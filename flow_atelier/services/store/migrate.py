"""Stage, replay and publish a legacy run tree before retiring its source."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
from collections.abc import Iterator
from contextlib import ExitStack
from pathlib import Path
from tempfile import TemporaryDirectory

import yaml

from flow_atelier.modules.liveness import is_crashed
from flow_atelier.schemas.flow import parse_flow_id
from flow_atelier.schemas.progress import FlowStatus
from flow_atelier.services.store.events import RunJournal, file_lock, project_folder
from flow_atelier.services.store.filesystem import FilesystemStore, _load_flow_yaml

_LOCKS = (".writer.lock", ".ask.lock")


def _leases(directory: Path, locks: ExitStack, dry_run: bool) -> None:
    for name in _LOCKS:
        path = directory / name
        if path.is_symlink() or (path.exists() and not path.is_file()):
            raise ValueError(f"invalid lock file {path}")
        if (not dry_run or path.exists()) and not locks.enter_context(file_lock(path, wait=False)):
            raise ValueError(f"active writer: {directory}")


def _check_record(directory: Path, old: FilesystemStore) -> None:
    """The display reader tolerates damaged records. Migration must preserve them."""
    for path in directory.iterdir():
        if path.is_symlink():
            raise ValueError(f"unexpected symlink {path}")
        if path.name == "flows" and path.is_dir():
            continue
        if not path.is_file():
            raise ValueError(f"unexpected entry {path}")
        if path.name in (*_LOCKS, "progress.json"):
            continue
        if path.name in ("input.yaml", "outputs.yaml", "sessions.yaml"):
            if not isinstance(_load_flow_yaml(path.read_text(encoding="utf-8")), dict):
                raise ValueError(f"expected a mapping in {path}")
        elif path.name == "logs.json":
            rows = json.loads(path.read_bytes())
            if not isinstance(rows, list):
                raise ValueError(f"expected log entries in {path}")
        elif path.name == "logs.jsonl":
            with path.open("rb") as stream:
                count = sum(1 for line in stream if line.strip())
            if len(old.read_logs(directory.name)) != count:
                raise ValueError(f"unreadable log entry in {path}")
        elif path.name == "steps.jsonl":
            records, consumed = old.read_steps(directory.name)
            with path.open("rb") as stream:
                if consumed != path.stat().st_size or len(records) != sum(1 for _ in stream):
                    raise ValueError(f"unreadable tool step in {path}")
        else:
            raise ValueError(f"unrecognized file {path}")


def _stage_tree(
    directory: Path, old: FilesystemStore, stage: Path, locks: ExitStack,
    dry_run: bool, project: Path, parent: str | None = None,
) -> Iterator[RunJournal]:
    run = directory.name
    try:
        if directory.is_symlink() or not directory.is_dir() or old._flow_dir(run) != directory:
            raise ValueError(f"invalid or ambiguous legacy directory {directory}")
        _leases(directory, locks, dry_run)
        _check_record(directory, old)
        progress = old.read_progress(run)
        if progress.status == FlowStatus.running and not (
            is_crashed(progress) or (progress.pending_ask and progress.runner_pid is None)
        ):
            raise ValueError("cannot rule out a live legacy writer")
        if progress.run_path:
            project = Path(progress.run_path)
            if not project.is_absolute():
                raise ValueError(f"relative working directory {project}")
        project = project.resolve()
        journal = RunJournal(stage / project_folder(project) / f"{run}.jsonl")
        inputs = old.read_input(run)
        workspace_root = old.workspace_dir(run)
        if workspace_root.resolve() != workspace_root:
            raise ValueError(f"unexpected workspace symlink {workspace_root}")
        header = {
            "flow_id": run, "conduit": parse_flow_id(run)[0], "parent_flow_id": parent,
            "project": str(project), "store": str(old.base_dir), "host": progress.runner_host,
            "pid": progress.runner_pid, "steps": "steps.jsonl", "inputs": inputs,
            "progress": progress.model_dump(mode="json"),
            "legacy_workspace_root": str(workspace_root),
        }
        journal.create(header)
        sessions = old.read_sessions(run)
        outputs = old.read_outputs(run)
        logs = [log.model_dump(mode="json", exclude_unset=True) for log in old.read_logs(run)]
        journal.append("sessions", sessions)
        journal.append("outputs", outputs)
        for log in logs:
            journal.append("log", log)
        steps = directory / "steps.jsonl"
        if steps.exists():
            shutil.copyfile(steps, journal.side / "steps.jsonl")
        if (
            journal.header() != header or journal.inputs() != inputs or journal.outputs() != outputs
            or journal.latest("sessions") != sessions or list(journal.logs()) != logs
            or (steps.exists() and _digest(steps) != _digest(journal.side / "steps.jsonl"))
        ):
            raise ValueError("staged journal replay differs from source")
    except (ValueError, OSError, TypeError, yaml.YAMLError) as exc:
        raise ValueError(f"{run}: {exc}") from exc
    yield journal
    children = directory / "flows"
    if children.exists():
        for child in sorted(children.iterdir()):
            yield from _stage_tree(child, old, stage, locks, dry_run, project, run)


def _digest(path: Path) -> bytes:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").digest()


def _side_bytes(directory: Path) -> dict[Path, bytes]:
    files = {}

    def visit(parent: Path) -> None:
        for path in parent.iterdir():
            if path.is_symlink():
                raise ValueError(f"unexpected symlink {path}")
            if path.is_dir():
                visit(path)
            elif not path.is_file():
                raise ValueError(f"unexpected file {path}")
            elif parent != directory or path.name not in (*_LOCKS, ".append.lock"):
                files[path.relative_to(directory)] = _digest(path)

    visit(directory)
    return files


def _staging_area(home: Path, locks: ExitStack, cleanup: ExitStack, dry_run: bool) -> Path:
    if dry_run:
        return Path(cleanup.enter_context(TemporaryDirectory()))
    control = home / ".migrate-runs"
    control.mkdir(mode=0o700, parents=True, exist_ok=True)
    lock = control / "lock"
    stage = control / "pending"
    if (
        control.is_symlink() or lock.is_symlink()
        or ((lock.exists() or stage.exists()) and not lock.is_file())
    ):
        raise ValueError(f"unexpected migration staging directory {control}")
    # Different stores can contain the same ID. Serialize their collision checks
    # and reclaim our private staging area if the previous process was killed.
    if not locks.enter_context(file_lock(lock, wait=False)):
        raise ValueError(f"migration already active at {control}")
    if stage.exists():
        shutil.rmtree(stage)
    stage.mkdir(mode=0o700)
    cleanup.callback(shutil.rmtree, stage)
    return stage


def migrate_tree(old: FilesystemStore, home: Path, root: str, *, dry_run: bool) -> list[str]:
    """Migrate one tree. An interrupted publish may reuse byte-identical staged records."""
    source = old.base_dir / "flows" / root
    retired = old.base_dir / "migrated-runs" / root
    if source.resolve() != source or retired.resolve() != retired or retired.exists():
        raise ValueError(f"source or archive conflict: {source}, {retired}")
    with ExitStack() as locks, ExitStack() as cleanup:
        stage = _staging_area(home, locks, cleanup, dry_run)
        source_locks = locks.enter_context(ExitStack())
        staged = list(_stage_tree(source, old, stage, source_locks, dry_run, old.base_dir.parent))
        projects = home / "projects"
        records = []
        for journal in staged:
            dest = RunJournal(projects / journal.path.relative_to(stage))
            if dest.path.resolve() != dest.path or dest.side.resolve() != dest.side:
                raise ValueError(f"symlink conflict at {dest.path}")
            for folder in projects.iterdir() if projects.exists() else ():
                if folder == dest.path.parent or not folder.is_dir():
                    continue
                try:
                    (folder / dest.path.name).lstat()
                except FileNotFoundError:
                    continue
                raise ValueError(f"another project already has run {dest.path.stem}")
            payload = _side_bytes(journal.side)
            if dest.side.exists():
                _leases(dest.side, locks, dry_run)
                if _side_bytes(dest.side) != payload:
                    raise ValueError(f"conflicting side files at {dest.side}")
            if dest.path.exists() and (
                not dest.path.is_file() or not dest.side.is_dir()
                or _digest(dest.path) != _digest(journal.path)
            ):
                raise ValueError(f"conflicting journal at {dest.path}")
            records.append((journal, dest, payload))

        if not dry_run:
            for journal, dest, payload in records:
                dest.path.parent.mkdir(parents=True, exist_ok=True)
                if not dest.side.exists():
                    journal.side.rename(dest.side)
                    _leases(dest.side, locks, False)
                if not dest.path.exists():
                    os.link(journal.path, dest.path)
                if _digest(dest.path) != _digest(journal.path) or _side_bytes(dest.side) != payload:
                    raise ValueError(f"published bytes differ at {dest.path}")
            cleanup.close()
            retired.parent.mkdir(exist_ok=True)
            if os.name == "nt":
                # Windows cannot rename the source with its lease handles open.
                # Published IDs already reject local writers as ambiguous.
                source_locks.close()
            source.rename(retired)
        return [j.path.stem for j in staged]
