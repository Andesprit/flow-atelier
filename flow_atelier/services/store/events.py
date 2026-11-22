"""Version 1 run journal. Only newline-terminated events are committed."""

from __future__ import annotations

import errno
import hashlib
import json
import os
import shutil
import tempfile
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any, BinaryIO

from flow_atelier.schemas.flow import parse_flow_id
from flow_atelier.services.store.permissions import private_dir, private_opener


class RunRecordError(ValueError):
    """A run has an unreadable or incompatible event record."""


class AmbiguousFlowId(RunRecordError):
    """Multiple records claim the same run ID."""


def project_folders(recorded: str) -> tuple[str, ...]:
    """Valid folder names for an already resolved project recorded in a journal."""
    encoded = "".join(c if c.isalnum() else "-" for c in recorded)
    if len(encoded.encode("utf-8")) <= 160:
        return (encoded,)
    digest = hashlib.sha256(recorded.encode("utf-8")).hexdigest()
    return (f"project-{digest}", encoded)


def project_folder(project: Path) -> str:
    return project_folders(str(project.resolve()))[0]


def _windows_lock(fd: int, *, wait: bool) -> None:
    import msvcrt

    while True:
        try:
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            return
        except OSError as exc:
            if exc.errno not in (errno.EACCES, errno.EAGAIN, errno.EDEADLK):
                raise
            if not wait:
                raise BlockingIOError(str(exc)) from exc
            time.sleep(0.05)


def publish_file(source: Path, destination: Path) -> None:
    """Publish once, including on filesystems without hard links."""
    try:
        os.link(source, destination)
    except OSError as exc:
        if exc.errno not in (
            errno.ENOTSUP,
            errno.EOPNOTSUPP,
            errno.ENOSYS,
            errno.EXDEV,
            errno.EPERM,
        ) and getattr(exc, "winerror", None) not in (1, 50):
            raise
        with source.open("rb") as src, open(destination, "xb", opener=private_opener) as dst:
            try:
                shutil.copyfileobj(src, dst)
            except BaseException:
                dst.close()
                destination.unlink()
                raise


@contextmanager
def file_lock(path: Path, *, wait: bool) -> Iterator[bool]:
    """The OS releases the lease on close, including process death."""
    with open(path, "a+b", opener=private_opener) as lock:
        try:
            if os.name == "nt":
                lock.seek(0)
                _windows_lock(lock.fileno(), wait=wait)
            else:
                import fcntl

                fcntl.flock(lock, fcntl.LOCK_EX | (0 if wait else fcntl.LOCK_NB))
        except (BlockingIOError, PermissionError):
            if wait:
                raise
            yield False
            return
        yield True


def _line(kind: str, data: dict[str, Any]) -> bytes:
    return (json.dumps({"version": 1, "type": kind, "data": data}) + "\n").encode()


def _repair_tail(stream: BinaryIO) -> int:
    """Find the last committed newline without rereading the whole journal."""
    end = stream.seek(0, os.SEEK_END)
    pos = end
    while pos:
        start = max(0, pos - 4096)
        stream.seek(start)
        last = stream.read(pos - start).rfind(b"\n")
        if last >= 0:
            pos = start + last + 1
            break
        pos = start
    if pos != end:
        stream.truncate(pos)
    stream.seek(pos)
    return pos


class RunJournal:
    def __init__(self, path: Path):
        self.path = path
        self.side = path.with_suffix("")

    def create(self, header: dict[str, Any]) -> None:
        """Publish a complete header without replacing an existing run."""
        private_dir(self.path.parent)
        with tempfile.NamedTemporaryFile(dir=self.path.parent, delete_on_close=False) as tmp:
            if os.name == "posix":
                os.fchmod(tmp.fileno(), 0o600)
            tmp.write(_line("created", header))
            tmp.close()
            publish_file(Path(tmp.name), self.path)

    def append(self, kind: str, data: dict[str, Any]) -> None:
        private_dir(self.side)
        with (
            file_lock(self.side / ".append.lock", wait=True),
            open(self.path, "r+b", opener=private_opener) as stream,
        ):
            sequence = _repair_tail(stream)
            if not sequence:
                raise RunRecordError(f"{self.path}: missing run header")
            if kind in {"outputs", "log"}:
                data = self._pack(kind, data, sequence)
            stream.write(_line(kind, data))

    def _pack(self, kind: str, data: dict[str, Any], sequence: int) -> dict[str, Any]:
        values = data.copy()
        files = {}
        for key, value in data.items():
            if not isinstance(value, str) or len(raw := value.encode("utf-8")) <= 100_000:
                continue
            task = data["task"] if kind == "log" else key
            if not task or not task.replace("_", "a").isalnum():
                raise ValueError(f"invalid output task: {task!r}")
            outputs = self.side / "outputs"
            private_dir(outputs)
            target = outputs / task
            private_dir(target)
            # Exclusive creation also skips orphaned blobs left by a killed writer.
            while True:
                path = target / str(sequence)
                sequence += 1
                try:
                    with open(path, "xb", opener=private_opener) as blob:
                        blob.write(raw)
                except FileExistsError:
                    continue
                break
            files[key] = path.relative_to(self.side).as_posix()
            del values[key]
        return {"values": values, "files": files}

    def append_step(self, line: bytes) -> None:
        private_dir(self.side)
        with (
            file_lock(self.side / ".append.lock", wait=True),
            open(self.side / "steps.jsonl", "a+b", opener=private_opener) as stream,
        ):
            _repair_tail(stream)
            stream.write(line)

    def _rows(self, stream: BinaryIO) -> Iterator[tuple[str, dict[str, Any]]]:
        for number, line in enumerate(stream, 1):
            if not line.endswith(b"\n"):
                return
            try:
                row = json.loads(line)
                if row["version"] != 1:
                    raise ValueError("unsupported event version")
                kind, data = row["type"], row["data"]
                if number == 1:
                    if (
                        kind != "created"
                        or data["flow_id"] != self.path.stem
                        or data["conduit"] != parse_flow_id(self.path.stem)[0]
                        or not Path(data["project"]).is_absolute()
                        or self.path.parent.name not in project_folders(data["project"])
                        or not (
                            data["parent_flow_id"] is None
                            or isinstance(data["parent_flow_id"], str)
                        )
                        or not Path(data["store"]).is_absolute()
                        or data["steps"] != "steps.jsonl"
                    ):
                        raise ValueError("invalid run header")
                elif kind not in {"progress", "outputs", "input", "log", "sessions", "heartbeat"}:
                    raise ValueError(f"unknown event type {kind!r}")
                if not isinstance(data, dict):
                    raise ValueError("expected event object")
            except (ValueError, TypeError, KeyError) as exc:
                raise RunRecordError(f"{self.path} line {number}: {exc}") from exc
            yield kind, data

    def rows(self) -> Iterator[tuple[str, dict[str, Any]]]:
        with self.path.open("rb") as stream:
            yield from self._rows(stream)

    def header(self) -> dict[str, Any]:
        with self.path.open("rb") as stream:
            first = next(self._rows(stream), None)
        if first is None:
            raise RunRecordError(f"{self.path}: missing run header")
        return first[1]

    def latest(self, kind: str) -> dict[str, Any]:
        value = {}
        for event, data in self.rows():
            if event == "created":
                value = data.get(kind, {})
            elif event == "heartbeat" and kind == "progress":
                value["heartbeat_at"] = data["at"]
            elif event == kind:
                value = data
        return value

    def inputs(self) -> dict[str, Any]:
        result = {}
        for kind, data in self.rows():
            if kind == "created":
                result.update(data["inputs"])
            elif kind == "input":
                result.update(data)
        return result

    def unpack(self, data: dict[str, Any]) -> dict[str, Any]:
        result = data["values"].copy()
        for key, relative in data["files"].items():
            path = Path(relative)
            if (
                len(path.parts) != 3
                or path.parts[0] != "outputs"
                or path.parts[1] == ".."
                or not path.name.isdecimal()
                or not (self.side / path).resolve().is_relative_to(self.side.resolve())
            ):
                raise RunRecordError(f"{self.path}: invalid output reference {relative!r}")
            result[key] = (self.side / path).read_bytes().decode("utf-8")
        return result

    def outputs(self) -> dict[str, Any]:
        saved = self.latest("outputs")
        return self.unpack(saved) if saved else {}

    def logs(self) -> Iterator[dict[str, Any]]:
        for kind, data in self.rows():
            if kind == "log":
                yield self.unpack(data)
