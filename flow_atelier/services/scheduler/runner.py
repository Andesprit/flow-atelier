"""Async scheduler daemon driven by the JSON-backed ScheduleStore."""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import signal
import sys
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger

from flow_atelier.schemas.api import ScheduledJob, ScheduleRunRecord
from flow_atelier.services.scheduler.store import ScheduleStore
from flow_atelier.services.scheduler.triggers import default_local_zone, to_trigger

logger = logging.getLogger(__name__)


# Job ID reserved for the periodic reload job.
_RELOAD_JOB_ID = "__atelier_sync__"


# An executor receives the schedule, its working dir, and a flow-id reporter it
# must invoke (via ``on_flow_started``) so the daemon can record which run a
# fire produced — even when the fire later fails.
ScheduleExecutor = Callable[
    [ScheduledJob, Path, Callable[[str], None]], Awaitable[None]
]


def _resolve_run_path(run_path: str, base: Path) -> Path:
    """Resolve a schedule's ``run_path`` against ``base``.

    Empty → ``base``; absolute → used verbatim; relative → resolved under
    ``base``. Shared by the daemon's fire path and the planned-view preview so
    "where it will fire" can never disagree with where it actually fires.

    :param run_path: the schedule's ``run_path`` (possibly empty)
    :param base: default working directory (already resolved)
    """
    if not run_path:
        return base
    wd = Path(run_path)
    return wd if wd.is_absolute() else (base / wd).resolve()


async def _default_executor(
    job: ScheduledJob, working_dir: Path, report: Callable[[str], None]
) -> None:
    """Default fire action: instantiate ``Atelier(base_dir=working_dir/.atelier)``
    and ``await run_conduit(...)``. Imported lazily to avoid a circular import.

    :param job: schedule being fired
    :param working_dir: directory under which ``.atelier`` is resolved
    :param report: flow-id reporter invoked once the run starts, forwarded as
        ``on_flow_started`` so the id is captured before any failure
    """
    from flow_atelier.core.atelier import Atelier

    atelier = Atelier(base_dir=working_dir / ".atelier")
    await atelier.run_conduit(
        job.conduit_name,
        dict(job.inputs),
        on_flow_started=report,
        working_dir=working_dir,
    )


@dataclass(frozen=True)
class PlannedJob:
    """A schedule currently registered with the daemon."""

    id: str
    name: str
    conduit_name: str
    next_fire_time: datetime | None
    working_dir: Path
    schedule_kind: str  # "once" | "recurring"
    last_run: ScheduleRunRecord | None = None


class SchedulerDaemon:
    """JSON-driven async scheduler.

    Holds an :class:`AsyncIOScheduler`, syncs the live job set against the
    schedule store on a fixed reload interval, and dispatches each fire by
    calling the configured executor with a fresh :class:`Atelier`.

    :param store: backing :class:`ScheduleStore`
    :param executor: per-fire coroutine; defaults to running the conduit
    :param default_zone: default timezone for naive ``run_at`` values
    :param default_working_dir: fallback working dir if ``run_path`` is empty
    :param reload_interval_seconds: how often to re-scan the store
    """

    def __init__(
        self,
        store: ScheduleStore,
        *,
        executor: ScheduleExecutor | None = None,
        default_zone: ZoneInfo | None = None,
        default_working_dir: Path | None = None,
        reload_interval_seconds: float = 30.0,
    ) -> None:
        """Configure the daemon without starting it.

        :param store: backing :class:`ScheduleStore`
        :param executor: per-fire coroutine; defaults to running the conduit
        :param default_zone: default timezone for naive ``run_at`` values
        :param default_working_dir: fallback working dir if ``run_path`` is empty
        :param reload_interval_seconds: how often to re-scan the store
        """
        self.store = store
        self.executor: ScheduleExecutor = executor or _default_executor
        self.default_zone = default_zone or default_local_zone()
        self.default_working_dir = (default_working_dir or Path.cwd()).resolve()
        self.reload_interval_seconds = reload_interval_seconds
        self._scheduler: AsyncIOScheduler | None = None
        # Hash of the registered schedule config keyed by job id, so sync()
        # ticks can skip re-adding APScheduler jobs whose config is unchanged.
        self._known: dict[str, str] = {}

    # ------------------------------------------------------------------ lifecycle

    async def start(self) -> None:
        """Boot the APScheduler, install the reload job, and sync from disk."""
        if self._scheduler is not None:
            return
        # APScheduler logs every job execution at INFO, including our 30s
        # reload tick, which buries the actual schedule-fire logs. Quiet its
        # executor logger so only our own fire logs (and real errors) surface.
        logging.getLogger("apscheduler.executors.default").setLevel(logging.WARNING)
        self._scheduler = AsyncIOScheduler(timezone=self.default_zone)
        self._scheduler.add_job(
            self.sync,
            trigger=IntervalTrigger(seconds=self.reload_interval_seconds),
            id=_RELOAD_JOB_ID,
            replace_existing=True,
            max_instances=1,
            coalesce=True,
        )
        await self.sync()
        self._scheduler.start()
        logger.info(
            "scheduler started: %d schedule(s), tz=%s, reload=%.1fs",
            len(self._known),
            self.default_zone,
            self.reload_interval_seconds,
        )

    async def stop(self) -> None:
        """Shut down the APScheduler if it is currently running."""
        if self._scheduler is None:
            return
        self._scheduler.shutdown(wait=False)
        self._scheduler = None
        logger.info("scheduler stopped")

    async def run_forever(self) -> None:
        """Start the daemon and block until SIGINT/SIGTERM."""
        await self.start()
        stop_event = asyncio.Event()
        loop = asyncio.get_running_loop()
        installed: list[int] = []
        if sys.platform != "win32":
            for sig in (signal.SIGINT, signal.SIGTERM):
                try:
                    loop.add_signal_handler(sig, stop_event.set)
                    installed.append(sig)
                except (NotImplementedError, RuntimeError):
                    pass
        try:
            await stop_event.wait()
        finally:
            for sig in installed:
                try:
                    loop.remove_signal_handler(sig)
                except Exception:  # noqa: BLE001
                    pass
            await self.stop()

    # ------------------------------------------------------------------ sync

    async def sync(self) -> None:
        """Diff live jobs against persisted schedules; add/update/remove as needed."""
        if self._scheduler is None:
            return
        active = {j.id: j for j in self.store.list()}
        live_ids = {
            job.id
            for job in self._scheduler.get_jobs()
            if job.id != _RELOAD_JOB_ID
        }

        for stale in live_ids - active.keys():
            self._scheduler.remove_job(stale)
            self._known.pop(stale, None)
            logger.info("removed schedule %s (no longer in store)", stale)

        for sid, job in active.items():
            if job.schedule.mode == "once" and self.store.fired_at(sid):
                # Skip already-fired one-shots (regardless of whether the
                # daemon was previously running).
                if sid in live_ids:
                    self._scheduler.remove_job(sid)
                self._known.pop(sid, None)
                continue
            self._register(job)

    @staticmethod
    def _config_hash(job: ScheduledJob) -> str:
        """Hash the fields that decide what/when the daemon fires.

        :param job: schedule whose config should be hashed
        """
        payload = {
            "conduit_name": job.conduit_name,
            "run_path": job.run_path,
            "inputs": job.inputs,
            "schedule": job.schedule.model_dump(mode="json"),
        }
        blob = json.dumps(payload, sort_keys=True, default=str).encode()
        return hashlib.sha256(blob).hexdigest()

    def _register(self, job: ScheduledJob) -> None:
        """Register or replace the APScheduler job for ``job``.

        Skips re-registration when the schedule config is byte-for-byte
        identical to what's already known — saves the no-op churn from
        every reload tick re-adding every active job.

        :param job: schedule to (re-)register
        """
        assert self._scheduler is not None
        config_hash = self._config_hash(job)
        if self._known.get(job.id) == config_hash:
            return
        trigger = to_trigger(job, default_zone=self.default_zone)
        self._scheduler.add_job(
            self._fire,
            trigger=trigger,
            id=job.id,
            args=[job.id],
            replace_existing=True,
            max_instances=1,
            coalesce=True,
        )
        self._known[job.id] = config_hash
        now = datetime.now(tz=self.default_zone)
        logger.info(
            "registered schedule %s (%s): next fire %s",
            job.id,
            job.schedule.mode,
            trigger.get_next_fire_time(None, now),
        )

    def _resolve_working_dir(self, job: ScheduledJob) -> Path:
        """Resolve ``job.run_path`` against the configured default working dir.

        :param job: schedule whose working directory should be resolved
        """
        return _resolve_run_path(job.run_path, self.default_working_dir)

    # ------------------------------------------------------------------ fire

    async def _fire(self, schedule_id: str) -> None:
        """Job function: re-read the latest schedule and dispatch it.

        ``once`` schedules are marked fired regardless of executor outcome
        so a single failure does not turn into an infinite retry loop.
        ``runs_completed`` only advances when the executor returns cleanly.

        :param schedule_id: schedule identifier passed by APScheduler
        """
        job = self.store.get(schedule_id)
        if job is None:
            logger.warning(
                "schedule %s not present before firing; skipping", schedule_id
            )
            return
        working_dir = self._resolve_working_dir(job)
        logger.info(
            "RUNNING schedule %s (%s) → conduit '%s' in %s",
            schedule_id,
            job.schedule.name or schedule_id,
            job.conduit_name,
            working_dir,
        )
        succeeded = False
        captured: dict[str, str | None] = {"id": None}

        def on_started(flow_id: str) -> None:
            """Capture the flow id the moment the run begins.

            :param flow_id: flow id assigned when the run starts
            """
            # The engine also reports each nested tool:conduit run; keep the first.
            if captured["id"] is None:
                captured["id"] = flow_id

        try:
            await self.executor(job, working_dir, on_started)
            succeeded = True
            logger.info(
                "DONE schedule %s → conduit '%s'", schedule_id, job.conduit_name
            )
        except Exception:  # noqa: BLE001 — daemon must survive a single failed fire
            logger.exception(
                "FAILED schedule %s → conduit '%s'", schedule_id, job.conduit_name
            )
        finally:
            if job.schedule.mode == "once":
                self.store.mark_fired(schedule_id)
            self.store.append_run_record(
                schedule_id,
                "succeeded" if succeeded else "failed",
                captured["id"],
            )
        if succeeded:
            self.store.increment_runs(schedule_id)

    # ------------------------------------------------------------------ introspection

    def list_planned(self) -> list[PlannedJob]:
        """Return the schedules currently visible to the daemon.

        Delegates to :func:`compute_planned_view` so callers get a fresh
        next-fire time computed against ``datetime.now`` rather than a
        registration-time snapshot that may already be stale.
        """
        return compute_planned_view(
            self.store,
            default_zone=self.default_zone,
            default_working_dir=self.default_working_dir,
        )


def compute_planned_view(
    store: ScheduleStore,
    *,
    default_zone: ZoneInfo,
    default_working_dir: Path,
) -> list[PlannedJob]:
    """Compute next-fire-time for every active schedule on disk.

    Used by ``atelier schedule list`` and ``atelier scheduler status`` so they
    work whether or not a daemon is running. Already-fired one-shots are
    surfaced with ``next_fire_time=None``.

    :param store: schedule store to read from
    :param default_zone: timezone used to evaluate cron/date triggers
    :param default_working_dir: base directory used to resolve relative ``run_path``
    """
    now = datetime.now(tz=default_zone)
    base = default_working_dir.resolve()
    planned: list[PlannedJob] = []
    for job in store.list():
        working_dir = _resolve_run_path(job.run_path, base)
        last_run = store.last_run(job.id)
        if job.schedule.mode == "once" and store.fired_at(job.id):
            planned.append(
                PlannedJob(
                    id=job.id,
                    name=job.schedule.name or job.id,
                    conduit_name=job.conduit_name,
                    next_fire_time=None,
                    working_dir=working_dir,
                    schedule_kind="once",
                    last_run=last_run,
                )
            )
            continue
        trigger = to_trigger(job, default_zone=default_zone)
        next_fire = trigger.get_next_fire_time(None, now)
        planned.append(
            PlannedJob(
                id=job.id,
                name=job.schedule.name or job.id,
                conduit_name=job.conduit_name,
                next_fire_time=next_fire,
                working_dir=working_dir,
                schedule_kind=job.schedule.mode,
                last_run=last_run,
            )
        )
    planned.sort(key=lambda p: p.id)
    return planned
