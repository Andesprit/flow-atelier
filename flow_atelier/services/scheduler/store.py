"""YAML-backed schedule store.

Layout under ``<atelier>/``:

- ``schedules/<slug(name)>-<hash>.yaml``: one file per :class:`ScheduledJob`.
  Deletes remove the file. The original ``schedule.name`` is preserved
  inside the YAML; the filename is a filesystem-safe slug of that name plus a
  short hash of the full name so distinct names that slug alike don't collide.
- ``scheduler_state.json``: fired-once markers keyed by schedule ``id``.

Atomic writes via ``os.replace``.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import time
import uuid
from datetime import UTC
from pathlib import Path
from typing import Any

import yaml

from flow_atelier.schemas.api import (
    CreateScheduleInput,
    ScheduledJob,
    ScheduleRunRecord,
)
from flow_atelier.services.scheduler.triggers import default_local_zone

logger = logging.getLogger(__name__)

_SLUG_RE = re.compile(r"[^a-z0-9._-]+")


def _atomic_replace(src: Path, dst: Path) -> None:
    """``os.replace(src, dst)`` with retries for transient Windows contention.

    On Windows, replacing a file while another thread or process holds it open
    (a concurrent reader, or another replace) raises ``PermissionError``
    (ERROR_ACCESS_DENIED / ERROR_SHARING_VIOLATION). The competing handle is
    short-lived, so a brief backoff-and-retry lets the replace land. POSIX
    renames over an open file and never raises this, so the loop succeeds on
    the first attempt there.
    """
    delay = 0.005
    for _ in range(10):
        try:
            os.replace(src, dst)
            return
        except PermissionError:
            time.sleep(delay)
            delay = min(delay * 2, 0.1)
    os.replace(src, dst)  # final attempt; surface the error if it still fails

# Cap the per-schedule run history so the flagship perpetual day/night loop
# can't grow scheduler_state.json without bound.
_MAX_HISTORY = 50


def _slug(name: str) -> str:
    """Reduce ``name`` to a filesystem-safe lowercase slug.

    :param name: human-readable schedule name
    :raises ValueError: when the slug would be empty
    """
    slug = _SLUG_RE.sub("-", name.strip().lower()).strip("-")
    if not slug:
        raise ValueError(f"schedule name has no filesystem-safe characters: {name!r}")
    return slug


def _filename_for_name(name: str) -> str:
    """Return an injective ``*.yaml`` filename for ``name``.

    The readable slug is kept as a prefix; a short hash of the *full* original
    name disambiguates distinct names that slug identically (``"My Job"`` vs
    ``"my-job"``), so they can no longer collide on one file.

    :param name: human-readable schedule name
    :raises ValueError: when the slug would be empty
    """
    digest = hashlib.sha1(name.encode("utf-8")).hexdigest()[:8]
    return f"{_slug(name)}-{digest}.yaml"


class ScheduleStore:
    """YAML-backed CRUD for :class:`ScheduledJob` records.

    :param atelier_dir: store directory, created on first write
    """

    def __init__(self, atelier_dir: Path | str) -> None:
        """Initialise the store rooted at ``atelier_dir``.

        :param atelier_dir: store directory, created on first write
        """
        self.atelier_dir = Path(atelier_dir)
        self.schedules_dir = self.atelier_dir / "schedules"
        self.state_path = self.atelier_dir / "scheduler_state.json"

    # --------------------------------------------------------------- internals

    def _path_for_name(self, name: str) -> Path:
        """Return the YAML path for ``name``.

        :param name: schedule name
        """
        return self.schedules_dir / _filename_for_name(name)

    def _find_job_file(self, schedule_id: str) -> tuple[Path, ScheduledJob] | None:
        """Locate the on-disk file backing ``schedule_id``.

        Resolves by reading each file's ``id`` rather than recomputing the
        filename, so pre-existing files written under an older slug scheme are
        still found.

        :param schedule_id: schedule identifier
        :returns: ``(path, job)`` or ``None`` if no file matches
        """
        for path in self._job_files():
            job = self._load_file(path)
            if job is not None and job.id == schedule_id:
                return path, job
        return None

    def _load_file(self, path: Path) -> ScheduledJob | None:
        """Load and validate one schedule YAML; return None on failure.

        :param path: path to a ``*.yaml`` schedule file
        """
        try:
            raw = yaml.safe_load(path.read_text())
        except (yaml.YAMLError, OSError) as e:
            logger.warning("skipping unreadable schedule %s: %s", path.name, e)
            return None
        if not isinstance(raw, dict):
            logger.warning("skipping malformed schedule %s: not a mapping", path.name)
            return None
        try:
            return ScheduledJob.model_validate(raw)
        except Exception as e:  # noqa: BLE001 — skip malformed rows
            logger.warning("skipping invalid schedule %s: %s", path.name, e)
            return None

    def _save_job(self, job: ScheduledJob, path: Path | None = None) -> None:
        """Atomically write ``job`` to its YAML file.

        :param job: schedule to persist
        :param path: explicit destination; defaults to the name-derived path.
            Pass the file a job was loaded from to update it in place.
        """
        if path is None:
            path = self._path_for_name(job.schedule.name)
            path.parent.mkdir(parents=True, exist_ok=True)
        payload = job.model_dump(mode="json", exclude_none=True)
        tmp = path.with_suffix(f".yaml.tmp.{uuid.uuid4().hex}")
        tmp.write_text(yaml.safe_dump(payload, sort_keys=False))
        _atomic_replace(tmp, path)

    def _job_files(self) -> list[Path]:
        """Enumerate YAML files, treating only a missing directory as empty."""
        try:
            return [p for p in self.schedules_dir.iterdir() if p.match("*.yaml")]
        except FileNotFoundError:
            return []

    def _iter_jobs(self) -> list[ScheduledJob]:
        """Return every persisted schedule, sorted by ``created_at`` ascending."""
        jobs: list[ScheduledJob] = []
        for path in self._job_files():
            job = self._load_file(path)
            if job is not None:
                jobs.append(job)
        jobs.sort(key=lambda j: j.created_at)
        return jobs

    # --------------------------------------------------------------- list / get

    def list(self) -> list[ScheduledJob]:
        """Return every persisted schedule, sorted by ``created_at``."""
        return self._iter_jobs()

    def get(self, schedule_id: str) -> ScheduledJob | None:
        """Return the schedule matching ``schedule_id`` or ``None``.

        :param schedule_id: schedule identifier
        """
        for job in self._iter_jobs():
            if job.id == schedule_id:
                return job
        return None

    def get_by_name(self, name: str) -> ScheduledJob | None:
        """Find the first schedule whose ``schedule.name`` matches.

        :param name: schedule name to look up
        """
        for job in self._iter_jobs():
            if job.schedule.name == name:
                return job
        return None

    # --------------------------------------------------------------- create / delete

    def create(self, payload: CreateScheduleInput) -> ScheduledJob:
        """Persist a new :class:`ScheduledJob` derived from ``payload``.

        :param payload: validated input describing the new schedule
        :raises ValueError: if ``payload.schedule.name`` is empty
        :raises FileExistsError: if a schedule with the same slug already exists
        """
        name = payload.schedule.name
        if not name.strip():
            raise ValueError("schedule.name must be non-empty")
        path = self._path_for_name(name)
        if path.exists():
            raise FileExistsError(
                f"schedule already exists for name {name!r} ({path.name})"
            )
        # Pin the host's current zone when the caller named none, so a later
        # host-timezone change can't silently shift this schedule's fire times.
        schedule = payload.schedule
        if schedule.timezone is None:
            schedule = schedule.model_copy(
                update={"timezone": default_local_zone().key}
            )
        job = ScheduledJob(
            id=f"SCH-{uuid.uuid4().hex[:12]}",
            conduit_name=payload.conduit_name,
            inputs=dict(payload.inputs),
            run_path=payload.run_path,
            schedule=schedule,
            created_at=int(time.time() * 1000),
            runs_completed=0,
        )
        self._save_job(job)
        return job

    def delete(self, schedule_id: str) -> ScheduledJob:
        """Remove the schedule's YAML file and clear its fired marker.

        :param schedule_id: schedule identifier
        :raises KeyError: if no schedule exists for ``schedule_id``
        :returns: the job as it was on disk just before removal
        """
        hit = self._find_job_file(schedule_id)
        if hit is None:
            raise KeyError(f"schedule not found: {schedule_id}")
        path, job = hit
        path.unlink(missing_ok=True)
        self.clear_fired(schedule_id)
        return job

    def increment_runs(self, schedule_id: str) -> None:
        """Bump ``runs_completed`` for ``schedule_id`` if it still exists.

        Re-checks that the YAML file still exists right before writing so a
        concurrent delete cannot resurrect the schedule by losing the race
        with the executor finishing.

        :param schedule_id: schedule identifier
        """
        hit = self._find_job_file(schedule_id)
        if hit is None:
            return
        path, job = hit
        self._save_job(
            job.model_copy(update={"runs_completed": job.runs_completed + 1}),
            path=path,
        )

    # --------------------------------------------------------------- fired state

    def _read_state(self) -> dict[str, Any]:
        """Load the ``scheduler_state.json`` payload, returning a safe default."""
        if not self.state_path.exists():
            return {"schedules": {}}
        # Retry transient OSErrors: on Windows a concurrent replace briefly
        # locks the file (ERROR_SHARING_VIOLATION). Returning the empty default
        # on such a blip would make the caller's read-modify-write clobber live
        # markers, so wait for the writer's short-lived handle to clear first.
        text: str | None = None
        delay = 0.005
        for _ in range(10):
            try:
                text = self.state_path.read_text()
                break
            except OSError:
                time.sleep(delay)
                delay = min(delay * 2, 0.1)
        if text is None:
            return {"schedules": {}}
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            return {"schedules": {}}
        if not isinstance(data, dict) or "schedules" not in data:
            return {"schedules": {}}
        return data

    def _write_state(self, data: dict[str, Any]) -> None:
        """Atomically replace ``scheduler_state.json`` with ``data``.

        :param data: full state payload to persist
        """
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.state_path.with_suffix(f".json.tmp.{uuid.uuid4().hex}")
        tmp.write_text(json.dumps(data, indent=2, sort_keys=True))
        _atomic_replace(tmp, self.state_path)

    def fired_at(self, schedule_id: str) -> str | None:
        """Return the ISO timestamp recorded for the last fire, or ``None``.

        :param schedule_id: schedule identifier
        """
        data = self._read_state()
        entry = data["schedules"].get(schedule_id)
        if not isinstance(entry, dict):
            return None
        value = entry.get("fired_at_iso")
        return value if isinstance(value, str) else None

    def mark_fired(self, schedule_id: str, scheduled_at_iso: str | None = None) -> None:
        """Record that ``schedule_id`` has fired.

        :param schedule_id: schedule identifier
        :param scheduled_at_iso: optional ISO timestamp; defaults to now
        """
        if scheduled_at_iso is None:
            from datetime import datetime

            scheduled_at_iso = (
                datetime.now(UTC).isoformat().replace("+00:00", "Z")
            )
        data = self._read_state()
        entry = data["schedules"].get(schedule_id)
        if not isinstance(entry, dict):
            entry = {}
        # Merge rather than overwrite so an existing ``runs`` history survives.
        entry["fired_at_iso"] = scheduled_at_iso
        data["schedules"][schedule_id] = entry
        self._write_state(data)

    def clear_fired(self, schedule_id: str) -> None:
        """Drop the fired-once marker for ``schedule_id`` if present.

        Deleting a schedule drops its entire state entry — including run
        history — which is the intended behaviour.

        :param schedule_id: schedule identifier
        """
        data = self._read_state()
        if schedule_id in data["schedules"]:
            del data["schedules"][schedule_id]
            self._write_state(data)

    # --------------------------------------------------------------- run history

    def append_run_record(
        self, schedule_id: str, status: str, flow_id: str | None
    ) -> None:
        """Append one fire outcome to ``schedule_id``'s bounded run history.

        History lives under the schedule's per-id state entry as a ``runs``
        list (newest-last), trimmed to the last ``_MAX_HISTORY`` records.

        :param schedule_id: schedule identifier
        :param status: ``"succeeded"`` or ``"failed"``
        :param flow_id: flow id produced by the fire, or ``None`` if it failed
            before the run started
        """
        from datetime import datetime

        ran_at_iso = datetime.now(UTC).isoformat().replace("+00:00", "Z")
        data = self._read_state()
        entry = data["schedules"].get(schedule_id)
        if not isinstance(entry, dict):
            entry = {}
        runs = entry.get("runs")
        if not isinstance(runs, list):
            runs = []
        runs.append(
            {"ran_at_iso": ran_at_iso, "status": status, "flow_id": flow_id}
        )
        entry["runs"] = runs[-_MAX_HISTORY:]
        data["schedules"][schedule_id] = entry
        self._write_state(data)

    def run_history(self, schedule_id: str) -> list[ScheduleRunRecord]:
        """Return ``schedule_id``'s recorded fires, oldest-first.

        Malformed entries are skipped, mirroring the defensive ``_read_state``
        style.

        :param schedule_id: schedule identifier
        """
        data = self._read_state()
        entry = data["schedules"].get(schedule_id)
        if not isinstance(entry, dict):
            return []
        raw = entry.get("runs")
        if not isinstance(raw, list):
            return []
        records: list[ScheduleRunRecord] = []
        for item in raw:
            try:
                records.append(ScheduleRunRecord.model_validate(item))
            except Exception:  # noqa: BLE001 — skip malformed rows
                continue
        return records

    def last_run(self, schedule_id: str) -> ScheduleRunRecord | None:
        """Return the most recent recorded fire, or ``None`` if no history.

        :param schedule_id: schedule identifier
        """
        history = self.run_history(schedule_id)
        return history[-1] if history else None
