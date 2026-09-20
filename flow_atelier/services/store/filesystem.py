"""Filesystem-backed implementation of StoreBase.

Manages the on-disk layout:

    <base>/
    ├── conduits/<name>/conduit.yaml
    └── flows/<flow_id>/
        ├── input.yaml
        ├── logs.jsonl
        ├── progress.json
        └── flows/<child_flow_id>/...

Nested (child) flows live under `<parent>/flows/<child_id>/`. The store tracks
parent->child relationships via an in-memory map keyed on `parent_flow_id`.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

from flow_atelier.schemas.conduit import CONDUIT_NAME_RE, Conduit
from flow_atelier.schemas.flow import new_flow_id, parse_flow_id
from flow_atelier.schemas.log import LogEntry, StepRecord
from flow_atelier.schemas.progress import Progress
from flow_atelier.services.store.base import ConduitSource, StoreBase

logger = logging.getLogger(__name__)

if os.name == "nt":
    def _atomic_replace(src: str | Path, dst: str | Path) -> None:
        for attempt in range(5):
            try:
                os.replace(src, dst)
                return
            except PermissionError:
                if attempt == 4:
                    raise
                time.sleep(0.05 * (attempt + 1))
else:
    def _atomic_replace(src: str | Path, dst: str | Path) -> None:
        os.replace(src, dst)


class FilesystemStore(StoreBase):
    def __init__(
        self,
        base_dir: Path | str,
        global_dir: Path | str | None = None,
    ):
        """Initialise the filesystem store with project and optional global dirs.

        :param base_dir: project-level ``.atelier/`` directory
        :param global_dir: optional user-level directory for shared conduits
        """
        self.base_dir = Path(base_dir)
        (self.base_dir / "conduits").mkdir(parents=True, exist_ok=True)
        (self.base_dir / "flows").mkdir(parents=True, exist_ok=True)
        self.global_dir: Path | None = Path(global_dir) if global_dir else None
        if self.global_dir is not None:
            try:
                (self.global_dir / "conduits").mkdir(parents=True, exist_ok=True)
            except OSError:
                # Read-only HOME / sandbox — degrade to project-only mode.
                self.global_dir = None
        # Both caches grow with distinct flow ids and are never pruned;
        # entries are tiny and bounded by process lifetime, so that's fine.
        self._log_locks: dict[str, asyncio.Lock] = {}
        self._flow_paths: dict[str, Path] = {}

    # ------------------------------------------------------------------ paths

    @staticmethod
    def _safe_conduit_name(name: str) -> str:
        """Reject conduit names that aren't a single safe path component.

        Defense-in-depth behind :class:`Conduit`'s name validator: guards any
        caller that builds a conduit path without going through the schema.

        :param name: conduit name to check.
        :returns: the name unchanged when safe.
        :raises ValueError: if the name contains a path separator or is ``.``/``..``.
        """
        if name in ("", ".", "..") or "/" in name or "\\" in name or os.sep in name:
            raise ValueError(f"unsafe conduit name: {name!r}")
        return name

    @staticmethod
    def _safe_flow_id(flow_id: str) -> bool:
        """Report whether ``flow_id`` is a single safe path component.

        The same guard :meth:`_safe_conduit_name` gives conduits. A flow id
        reaches this store straight from a URL path segment
        (``GET /flows/<id>/logs``) and is joined onto ``flows/`` unchecked,
        so without this ``%2E%2E`` walks out of the flow store and reads
        (or, via :meth:`delete_flow`, removes) whatever sits one level up.

        Both halves are load-bearing: the id grammar pins the
        ``<date>_<uuid8>_`` prefix, which alone rules out ``.``/``..``, and
        the conduit-name check closes the trailing ``.+`` capture, which
        would otherwise happily match ``../../etc``.

        :param flow_id: candidate flow identifier.
        :returns: True when the id is well-formed and separator-free.
        """
        try:
            conduit, _, _ = parse_flow_id(flow_id)
        except ValueError:
            return False
        return bool(CONDUIT_NAME_RE.match(conduit))

    def _conduit_dir(self, name: str) -> Path:
        """Return the project-level directory for conduit ``name``.

        :param name: conduit name
        """
        return self.base_dir / "conduits" / self._safe_conduit_name(name)

    def _conduit_yaml(self, name: str) -> Path:
        """Return the project-level ``conduit.yaml`` path for ``name``.

        :param name: conduit name
        """
        return self._conduit_dir(name) / "conduit.yaml"

    def _global_conduit_dir(self, name: str) -> Path | None:
        """Return the global directory for conduit ``name`` or ``None``.

        :param name: conduit name
        """
        if self.global_dir is None:
            return None
        return self.global_dir / "conduits" / self._safe_conduit_name(name)

    def _global_conduit_yaml(self, name: str) -> Path | None:
        """Return the global ``conduit.yaml`` path for ``name`` or ``None``.

        :param name: conduit name
        """
        conduit_dir = self._global_conduit_dir(name)
        return conduit_dir / "conduit.yaml" if conduit_dir is not None else None

    def _flow_dir(self, flow_id: str) -> Path:
        """Resolve and cache the directory for ``flow_id``.

        :param flow_id: flow identifier
        :raises FileNotFoundError: if the id is malformed or no such flow exists
        """
        if flow_id in self._flow_paths:
            return self._flow_paths[flow_id]
        # Rejected as "not found" rather than as a distinct error: to every
        # caller a malformed id names no flow, and the routes already map
        # FileNotFoundError to a 404. This also keeps the rglob below from
        # ever receiving a glob pattern (`*`) to walk the store with.
        if not self._safe_flow_id(flow_id):
            raise FileNotFoundError(f"flow not found: {flow_id}")
        # Top-level flows live at a deterministic path; only nested child
        # flows (under <parent>/flows/<id>) need the recursive search.
        top_level = self.base_dir / "flows" / flow_id
        if top_level.is_dir():
            self._flow_paths[flow_id] = top_level
            return top_level
        # fall-back: search (for nested flows after restart)
        for candidate in self.base_dir.rglob(flow_id):
            if candidate.is_dir() and candidate.name == flow_id:
                self._flow_paths[flow_id] = candidate
                return candidate
        raise FileNotFoundError(f"flow not found: {flow_id}")

    # ------------------------------------------------------------------ conduits

    def read_conduit(self, name: str) -> Conduit:
        """Load conduit ``name`` from the project store, falling back to global.

        :param name: conduit name
        """
        project_path = self._conduit_yaml(name)
        global_path = self._global_conduit_yaml(name)
        if project_path.exists():
            path = project_path
        elif global_path is not None and global_path.exists():
            path = global_path
        else:
            raise FileNotFoundError(f"conduit not found: {name} ({project_path})")
        try:
            # Explicit utf-8: the locale default is cp1252 on Windows, which
            # mangles accented conduits and silently accepts binary files that
            # every other platform rejects as unreadable.
            data = yaml.safe_load(path.read_text(encoding="utf-8"))
        except yaml.YAMLError as e:
            raise ValueError(f"{name}: invalid YAML — {e}") from e
        conduit = Conduit.model_validate(data)
        if conduit.name != name:
            raise ValueError(
                f"conduit.yaml name {conduit.name!r} != folder name {name!r}"
            )
        return conduit

    def _scan_conduits_dir(self, root: Path) -> list[str]:
        """List conduit names under ``root`` that have a ``conduit.yaml``.

        :param root: directory expected to contain ``<name>/conduit.yaml``
        """
        if not root.exists():
            return []
        return sorted(
            p.name
            for p in root.iterdir()
            if p.is_dir() and (p / "conduit.yaml").exists()
        )

    def list_conduits(self) -> list[str]:
        """Return all visible conduit names from project and global stores."""
        return [name for name, _ in self.list_conduits_with_source()]

    def list_conduits_with_source(self) -> list[tuple[str, ConduitSource]]:
        """Return ``(name, source)`` pairs; project shadows global on collision."""
        project_names = set(self._scan_conduits_dir(self.base_dir / "conduits"))
        global_names: set[str] = set()
        if self.global_dir is not None:
            global_names = set(
                self._scan_conduits_dir(self.global_dir / "conduits")
            )
        entries: list[tuple[str, ConduitSource]] = []
        for name in sorted(project_names | global_names):
            source: ConduitSource = "project" if name in project_names else "global"
            entries.append((name, source))
        return entries

    def write_conduit(self, conduit: Conduit) -> None:
        """Persist ``conduit`` under ``conduits/<name>/conduit.yaml``.

        :param conduit: validated conduit to persist (overwrites if present)
        """
        conduit_dir = self._conduit_dir(conduit.name)
        conduit_dir.mkdir(parents=True, exist_ok=True)
        payload = conduit.model_dump(mode="json", by_alias=True, exclude_none=True)
        path = self._conduit_yaml(conduit.name)
        tmp = path.with_suffix(f".yaml.tmp.{uuid.uuid4().hex}")
        tmp.write_text(yaml.safe_dump(payload, sort_keys=False))
        _atomic_replace(tmp, path)

    def write_conduit_global(self, conduit: Conduit) -> None:
        """Persist ``conduit`` under the global ``~/.atelier/conduits/<name>/``.

        :param conduit: validated conduit to persist.
        :raises RuntimeError: if no global directory is available.
        """
        conduit_dir = self._global_conduit_dir(conduit.name)
        if conduit_dir is None:
            raise RuntimeError(
                "global conduit directory is not available "
                "(home directory may be read-only)"
            )
        conduit_dir.mkdir(parents=True, exist_ok=True)
        path = conduit_dir / "conduit.yaml"
        payload = conduit.model_dump(mode="json", by_alias=True, exclude_none=True)
        tmp = path.with_suffix(f".yaml.tmp.{uuid.uuid4().hex}")
        tmp.write_text(yaml.safe_dump(payload, sort_keys=False))
        _atomic_replace(tmp, path)

    def conduit_dir(self, name: str) -> Path:
        """Return the directory conduit ``name`` loads from (project then global).

        :param name: conduit name
        :raises FileNotFoundError: if not found in either store
        """
        project_dir = self._conduit_dir(name)
        if self._conduit_yaml(name).exists():
            return project_dir
        global_dir = self._global_conduit_dir(name)
        global_yaml = self._global_conduit_yaml(name)
        if global_dir is not None and global_yaml is not None and global_yaml.exists():
            return global_dir
        raise FileNotFoundError(f"conduit not found: {name}")

    def conduit_source(self, name: str) -> ConduitSource:
        """Return whether ``name`` lives in the project or global store.

        :param name: conduit name
        :raises FileNotFoundError: if not found in either store
        """
        if self._conduit_yaml(name).exists():
            return "project"
        global_yaml = self._global_conduit_yaml(name)
        if global_yaml is not None and global_yaml.exists():
            return "global"
        raise FileNotFoundError(f"conduit not found: {name}")

    def delete_conduit_global(self, name: str) -> bool:
        """Remove a global conduit by name.

        :param name: conduit name
        :returns: True if deleted, False if it didn't exist
        """
        conduit_dir = self._global_conduit_dir(name)
        if conduit_dir is None or not conduit_dir.exists():
            return False
        shutil.rmtree(conduit_dir)
        return True

    def delete_conduit(self, name: str) -> bool:
        """Remove ``conduits/<name>/`` if present (project store only).

        :param name: conduit name
        :returns: True if deleted, False if it didn't exist
        """
        conduit_dir = self._conduit_dir(name)
        if not conduit_dir.exists():
            return False
        shutil.rmtree(conduit_dir)
        return True

    # ------------------------------------------------------------------ flows

    def create_flow(
        self,
        conduit_name: str,
        inputs: dict[str, Any],
        parent_flow_id: str | None = None,
        *,
        flow_id: str | None = None,
    ) -> str:
        """Create a new flow directory and seed its tracking files.

        :param conduit_name: conduit being run
        :param inputs: initial input map persisted to ``input.yaml``
        :param parent_flow_id: optional parent flow for nested runs
        :param flow_id: optional pre-generated flow id; when ``None`` the
            store generates one via :func:`new_flow_id`
        :returns: the flow id (generated or provided)
        """
        if flow_id is None:
            flow_id = new_flow_id(conduit_name)
        # Checked after generation, not before: the id embeds the conduit
        # name, so an unsafe name produces an unsafe id either way, and
        # mkdir(parents=True) below would create the escaped path.
        if not self._safe_flow_id(flow_id):
            raise ValueError(f"unsafe flow id: {flow_id!r}")
        if parent_flow_id is None:
            flow_dir = self.base_dir / "flows" / flow_id
        else:
            flow_dir = self._flow_dir(parent_flow_id) / "flows" / flow_id
        flow_dir.mkdir(parents=True, exist_ok=True)
        (flow_dir / "flows").mkdir(exist_ok=True)
        self._flow_paths[flow_id] = flow_dir
        # input.yaml
        (flow_dir / "input.yaml").write_text(yaml.safe_dump(inputs, sort_keys=False))
        # logs.jsonl (one JSON entry per line, appended as tasks run)
        (flow_dir / "logs.jsonl").write_text("")
        # progress.json (empty shell; engine writes real content immediately)
        (flow_dir / "progress.json").write_text(
            json.dumps(
                {
                    "status": "running",
                    "current_tasks": [],
                    "tasks": {},
                    "started_at": datetime.now(UTC)
                    .isoformat()
                    .replace("+00:00", "Z"),
                    "finished_at": None,
                },
                indent=2,
            )
        )
        return flow_id

    def list_flows(self, conduit_name: str | None = None) -> list[str]:
        """List top-level flow ids, optionally filtered by conduit name.

        :param conduit_name: when set, only flows whose parsed conduit equals
            ``conduit_name`` are returned.
        """
        root = self.base_dir / "flows"
        if not root.exists():
            return []
        ids: list[str] = []
        for p in root.iterdir():
            if not p.is_dir():
                continue
            if conduit_name is None:
                ids.append(p.name)
                continue
            try:
                flow_conduit, _, _ = parse_flow_id(p.name)
            except ValueError:
                continue
            if flow_conduit == conduit_name:
                ids.append(p.name)
        return sorted(ids)

    def list_child_flows(self, parent_flow_id: str) -> list[str]:
        """List flow ids under ``parent_flow_id/flows/``.

        :param parent_flow_id: parent flow identifier
        :returns: sorted list of child flow ids
        """
        children_dir = self._flow_dir(parent_flow_id) / "flows"
        if not children_dir.exists():
            return []
        return sorted(
            p.name for p in children_dir.iterdir() if p.is_dir()
        )

    def delete_flow(self, flow_id: str) -> bool:
        """Remove a flow directory and its nested child subtree.

        :param flow_id: flow identifier
        :returns: True if deleted, False if it didn't exist
        """
        try:
            flow_dir = self._flow_dir(flow_id)
        except FileNotFoundError:
            return False
        shutil.rmtree(flow_dir)
        # Evict the flow itself and any cached children that lived under it,
        # so no stale handles survive in the path/lock caches.
        self._flow_paths.pop(flow_id, None)
        self._log_locks.pop(flow_id, None)
        for cached_id, cached_path in list(self._flow_paths.items()):
            if cached_path.is_relative_to(flow_dir):
                self._flow_paths.pop(cached_id, None)
                self._log_locks.pop(cached_id, None)
        return True

    # ------------------------------------------------------------------ logs

    def _lock_for(self, flow_id: str) -> asyncio.Lock:
        """Return (lazily creating) the asyncio lock guarding ``flow_id`` logs.

        :param flow_id: flow identifier
        """
        lock = self._log_locks.get(flow_id)
        if lock is None:
            lock = asyncio.Lock()
            self._log_locks[flow_id] = lock
        return lock

    async def append_log(self, flow_id: str, entry: LogEntry) -> None:
        """Append ``entry`` to the flow's ``logs.jsonl`` under a per-flow lock.

        One JSON document per line — O(1) append instead of rewriting the
        whole log, and the blocking write runs off the event loop.

        :param flow_id: flow identifier
        :param entry: log entry to append
        """
        path = self._flow_dir(flow_id) / "logs.jsonl"
        line = json.dumps(entry.model_dump()) + "\n"

        def _write() -> None:
            with path.open("a", encoding="utf-8") as f:
                f.write(line)

        async with self._lock_for(flow_id):
            await asyncio.to_thread(_write)

    def read_logs(self, flow_id: str) -> list[LogEntry]:
        """Return all log entries for ``flow_id`` in append order.

        Reads ``logs.jsonl``; falls back to the legacy ``logs.json`` array
        for flow dirs created before the JSONL switch.

        Both are read as UTF-8 to match ``append_log``, which writes that
        way. Falling back to the locale default decodes as cp1252 on a stock
        Windows console and raises on the first non-ASCII character an agent
        or a task's output happens to contain.

        :param flow_id: flow identifier
        :returns: parsed list of :class:`LogEntry` — empty if missing/unreadable
        """
        flow_dir = self._flow_dir(flow_id)
        jsonl_path = flow_dir / "logs.jsonl"
        if jsonl_path.exists():
            entries: list[LogEntry] = []
            lines = jsonl_path.read_text(encoding="utf-8").splitlines()
            last_index = len(lines) - 1
            for index, line in enumerate(lines):
                if not line.strip():
                    continue
                try:
                    entries.append(LogEntry.model_validate(json.loads(line)))
                except (json.JSONDecodeError, ValueError):
                    # A truncated trailing line is the expected aftermath of a
                    # crash mid-write; a corrupt *interior* line means a real
                    # entry vanished, which must not happen silently.
                    if index != last_index:
                        logger.warning(
                            "read_logs: dropping unreadable line %d of %d in %s",
                            index + 1,
                            len(lines),
                            jsonl_path,
                        )
                    continue
            return entries
        legacy_path = flow_dir / "logs.json"
        try:
            raw = json.loads(legacy_path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError):
            return []
        return [LogEntry.model_validate(item) for item in raw]

    # -------------------------------------------------------------- live steps

    async def append_step(self, flow_id: str, record: StepRecord) -> None:
        """Append ``record`` to the flow's ``steps.jsonl``.

        Shares the per-flow log lock so a step write cannot interleave with
        a ``logs.jsonl`` write mid-line.

        :param flow_id: flow identifier
        :param record: step record to append
        """
        path = self._flow_dir(flow_id) / "steps.jsonl"
        line = json.dumps(record.model_dump()) + "\n"

        def _write() -> None:
            with path.open("a", encoding="utf-8") as f:
                f.write(line)

        async with self._lock_for(flow_id):
            await asyncio.to_thread(_write)

    def read_steps(
        self, flow_id: str, offset: int = 0
    ) -> tuple[list[StepRecord], int]:
        """Return live step records for ``flow_id`` from ``offset`` onward.

        ``offset`` is a byte position, and the matching end position comes
        back with the records. A poller feeds the returned value into its
        next call and reads only what was appended since, instead of pulling
        the whole file through memory every quarter second — ``steps.jsonl``
        has no rotation, so on a long loop conduit that file only grows.

        A truncated trailing line is expected: the file is appended to while
        the flow runs, which is exactly when it gets read. It is left
        unconsumed and picked up once complete. A *complete* line that will
        not parse is corruption rather than a race, so it is logged, skipped,
        and stepped over — stopping there would hide every later step for the
        rest of the flow's life.

        :param flow_id: flow identifier
        :param offset: byte offset to resume from; ``0`` reads from the start
        :returns: tuple of the parsed records and the byte offset just past
            the last complete line consumed
        """
        path = self._flow_dir(flow_id) / "steps.jsonl"
        if not path.exists():
            return [], offset
        with path.open("rb") as f:
            f.seek(offset)
            data = f.read()

        records: list[StepRecord] = []
        consumed = 0
        corrupt = 0
        first_corrupt_at = 0
        for raw in data.splitlines(keepends=True):
            if not raw.endswith(b"\n"):
                break  # partial trailing write — retried on the next read
            consumed += len(raw)
            try:
                records.append(
                    StepRecord.model_validate(json.loads(raw.decode("utf-8")))
                )
            except (json.JSONDecodeError, UnicodeDecodeError, ValueError):
                # Tallied, not logged per line: a truncated or garbled file can
                # be corrupt for thousands of consecutive lines, and one warning
                # per line would bury the run's real output.
                if not corrupt:
                    first_corrupt_at = offset + consumed - len(raw)
                corrupt += 1
        if corrupt:
            logger.warning(
                "skipped %d unreadable step record(s) in %s, first at byte %d",
                corrupt,
                path,
                first_corrupt_at,
            )
        return records, offset + consumed

    # ------------------------------------------------------------------ progress

    # write_progress / write_outputs / append_input are intentionally
    # synchronous: the files are tiny, writes are atomic-replace, and
    # crash-resume depends on state transitions hitting disk in order.

    def write_progress(self, flow_id: str, progress: Progress) -> None:
        """Atomically write ``progress.json`` for ``flow_id``.

        :param flow_id: flow identifier
        :param progress: progress snapshot to persist
        """
        path = self._flow_dir(flow_id) / "progress.json"
        tmp = path.with_suffix(f".json.tmp.{uuid.uuid4().hex}")
        tmp.write_text(progress.model_dump_json(indent=2))
        _atomic_replace(tmp, path)

    def read_progress(self, flow_id: str) -> Progress:
        """Load and return the ``Progress`` snapshot for ``flow_id``.

        :param flow_id: flow identifier
        """
        path = self._flow_dir(flow_id) / "progress.json"
        return Progress.model_validate_json(path.read_text())

    # ------------------------------------------------------------------ outputs.yaml

    def read_outputs(self, flow_id: str) -> dict[str, Any]:
        """Return the parsed ``outputs.yaml`` map (empty if missing).

        :param flow_id: flow identifier
        :returns: mapping of task name to output string
        """
        path = self._flow_dir(flow_id) / "outputs.yaml"
        if not path.exists():
            return {}
        loaded = yaml.safe_load(path.read_text())
        return loaded if isinstance(loaded, dict) else {}

    def write_outputs(self, flow_id: str, outputs: dict[str, Any]) -> None:
        """Atomically write ``outputs.yaml`` for ``flow_id``.

        :param flow_id: flow identifier
        :param outputs: mapping of task name to final output (``None`` for tasks
            that did not complete)
        """
        path = self._flow_dir(flow_id) / "outputs.yaml"
        tmp = path.with_suffix(f".yaml.tmp.{uuid.uuid4().hex}")
        tmp.write_text(yaml.safe_dump(outputs, sort_keys=False))
        _atomic_replace(tmp, path)

    # ------------------------------------------------------------------ input.yaml

    def read_input(self, flow_id: str) -> dict[str, Any]:
        """Return the parsed ``input.yaml`` map for ``flow_id`` (empty if missing).

        :param flow_id: flow identifier
        """
        path = self._flow_dir(flow_id) / "input.yaml"
        if not path.exists():
            return {}
        loaded = yaml.safe_load(path.read_text())
        return loaded if isinstance(loaded, dict) else {}

    def append_input(self, flow_id: str, key: str, value: Any) -> None:
        """Set ``key=value`` in the flow's ``input.yaml``.

        :param flow_id: flow identifier
        :param key: input key to add or overwrite
        :param value: value to store under ``key``
        """
        path = self._flow_dir(flow_id) / "input.yaml"
        existing: dict[str, Any] = {}
        if path.exists():
            loaded = yaml.safe_load(path.read_text())
            if isinstance(loaded, dict):
                existing = loaded
        existing[key] = value
        tmp = path.with_suffix(f".yaml.tmp.{uuid.uuid4().hex}")
        tmp.write_text(yaml.safe_dump(existing, sort_keys=False))
        _atomic_replace(tmp, path)
