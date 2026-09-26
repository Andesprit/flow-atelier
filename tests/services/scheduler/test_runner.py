"""SchedulerDaemon: sync, fire, and one-shot fired-state tests (JSON store)."""
from __future__ import annotations

import asyncio
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from flow_atelier.schemas.api import CreateScheduleInput, ScheduledJob
from flow_atelier.services.scheduler.runner import (
    _RELOAD_JOB_ID,
    SchedulerDaemon,
    _default_executor,
    _resolve_run_path,
    compute_planned_view,
)
from flow_atelier.services.scheduler.store import ScheduleStore

UTC = ZoneInfo("UTC")


def test_default_executor_threads_working_dir(tmp_path, monkeypatch):
    """The default fire action must run the conduit under the resolved dir.

    Without this, scheduled tasks execute in the daemon's launch dir, ignoring
    the schedule's ``run_path``.
    """
    captured: dict = {}

    class _FakeAtelier:
        def __init__(self, base_dir):
            captured["base_dir"] = base_dir

        async def run_conduit(self, name, inputs, **kwargs):
            captured["name"] = name
            captured["working_dir"] = kwargs.get("working_dir")
            return "flow-1"

    monkeypatch.setattr("flow_atelier.core.atelier.Atelier", _FakeAtelier)
    working_dir = tmp_path.resolve()
    store = ScheduleStore(tmp_path / ".atelier")
    store.create(_recurring(conduit="report"))
    job = store.list()[0]

    asyncio.run(_default_executor(job, working_dir, lambda _fid: None))

    assert captured["working_dir"] == working_dir
    assert captured["base_dir"] == working_dir / ".atelier"


@pytest.mark.parametrize(
    "run_path",
    ["", "relative/dir", "/abs/dir"],
)
def test_resolve_run_path_matches_between_daemon_and_view(run_path, tmp_path):
    """The daemon's fire-path resolver and the planned-view preview must agree.

    :param run_path: run_path variant (empty / relative / absolute).
    :param tmp_path: pytest temp directory fixture.
    """
    base = tmp_path.resolve()
    store = ScheduleStore(tmp_path / ".atelier")
    store.create(_recurring(conduit="report", run_path=run_path))
    daemon = SchedulerDaemon(store, default_working_dir=base)
    job = store.list()[0]
    view = compute_planned_view(store, default_zone=UTC, default_working_dir=base)
    assert daemon._resolve_working_dir(job) == view[0].working_dir
    assert daemon._resolve_working_dir(job) == _resolve_run_path(run_path, base)


def _recurring(conduit: str = "report", run_path: str = "/tmp/run") -> CreateScheduleInput:
    """Build a recurring CreateScheduleInput for the given conduit.

    :param conduit: conduit name to embed in the schedule payload.
    :param run_path: working directory for the scheduled run.
    """
    return CreateScheduleInput.model_validate(
        {
            "conduit_name": conduit,
            "inputs": {},
            "run_path": run_path,
            "schedule": {
                "mode": "recurring",
                "name": conduit,
                "days": [1],  # Monday
                "times": ["09:00"],
            },
        }
    )


def _once(at_iso: str = "2099-05-01T09:00:00Z") -> CreateScheduleInput:
    """Build a once-mode CreateScheduleInput firing at the given timestamp.

    :param at_iso: ISO-8601 timestamp for the one-shot run.
    """
    return CreateScheduleInput.model_validate(
        {
            "conduit_name": "backfill",
            "inputs": {},
            "run_path": "/tmp/run",
            "schedule": {
                "mode": "once",
                "name": "backfill_once",
                "run_at": at_iso,
            },
        }
    )


@pytest.fixture
def store(tmp_path) -> ScheduleStore:
    """Provide a ScheduleStore rooted at a temp .atelier directory.

    :param tmp_path: pytest temp directory fixture.
    """
    return ScheduleStore(tmp_path / ".atelier")


class _RecordingExecutor:
    def __init__(self) -> None:
        """Initialize a recording executor with empty call history."""
        self.calls: list[tuple[ScheduledJob, Path]] = []
        self.raise_on_next = False
        self.report_flow_id = "FLOW-test-id"

    async def __call__(
        self, job: ScheduledJob, working_dir: Path, report
    ) -> None:
        """Record an execution, report a flow id, optionally raise to fail.

        Reports the flow id before any failure, mirroring how a real run
        fires ``on_flow_started`` before the first task runs.

        :param job: scheduled job being fired.
        :param working_dir: working directory for the run.
        :param report: flow-id reporter callback supplied by the daemon.
        """
        self.calls.append((job, working_dir))
        if self.report_flow_id is not None:
            report(self.report_flow_id)
        if self.raise_on_next:
            self.raise_on_next = False
            raise RuntimeError("forced failure for test")


@pytest.fixture
def executor() -> _RecordingExecutor:
    """Provide a fresh recording executor."""
    return _RecordingExecutor()


@pytest.fixture
async def daemon(tmp_path, store, executor):
    """Provide a started/stopped SchedulerDaemon wired to the test store and executor.

    :param tmp_path: pytest temp directory fixture.
    :param store: ScheduleStore fixture.
    :param executor: recording executor fixture.
    """
    d = SchedulerDaemon(
        store,
        executor=executor,
        default_zone=UTC,
        default_working_dir=tmp_path,
        reload_interval_seconds=3600,
    )
    yield d
    await d.stop()


# -------------------------------------------------------------- start / stop


async def test_start_registers_existing_schedules(daemon, store):
    """Verify start() registers all pre-existing schedules.

    :param daemon: SchedulerDaemon fixture.
    :param store: ScheduleStore fixture.
    """
    a = store.create(_recurring("a"))
    b = store.create(_once())
    await daemon.start()
    planned = {p.id for p in daemon.list_planned()}
    assert planned == {a.id, b.id}


async def test_start_is_idempotent(daemon, store):
    """Verify calling start() twice does not duplicate jobs.

    :param daemon: SchedulerDaemon fixture.
    :param store: ScheduleStore fixture.
    """
    job = store.create(_recurring())
    await daemon.start()
    await daemon.start()
    assert {p.id for p in daemon.list_planned()} == {job.id}


async def test_start_with_no_schedules(daemon):
    """Verify start() handles an empty store gracefully.

    :param daemon: SchedulerDaemon fixture.
    """
    await daemon.start()
    assert daemon.list_planned() == []


# -------------------------------------------------------------- sync / hot reload


async def test_sync_picks_up_added_schedules(daemon, store):
    """Verify sync() picks up schedules added after start.

    :param daemon: SchedulerDaemon fixture.
    :param store: ScheduleStore fixture.
    """
    await daemon.start()
    assert daemon.list_planned() == []
    job = store.create(_recurring())
    await daemon.sync()
    assert [p.id for p in daemon.list_planned()] == [job.id]


async def test_sync_drops_deleted_schedules(daemon, store):
    """Verify sync() drops schedules deleted from the store.

    :param daemon: SchedulerDaemon fixture.
    :param store: ScheduleStore fixture.
    """
    job = store.create(_recurring())
    await daemon.start()
    store.delete(job.id)
    await daemon.sync()
    assert daemon.list_planned() == []


async def test_sync_reload_job_is_preserved(daemon, store):
    """Verify the internal reload job remains scheduled after sync.

    :param daemon: SchedulerDaemon fixture.
    :param store: ScheduleStore fixture.
    """
    await daemon.start()
    store.create(_recurring())
    await daemon.sync()
    job_ids = {j.id for j in daemon._scheduler.get_jobs()}
    assert _RELOAD_JOB_ID in job_ids


async def test_sync_skips_unchanged_schedules(daemon, store, monkeypatch):
    """Verify a sync tick does not re-add APScheduler jobs whose config is identical.

    :param daemon: SchedulerDaemon fixture.
    :param store: ScheduleStore fixture.
    :param monkeypatch: pytest monkeypatch fixture.
    """
    store.create(_recurring())
    await daemon.start()
    # Count add_job calls during a no-op sync; only the reload job belongs.
    add_calls: list[str] = []
    original_add = daemon._scheduler.add_job

    def _spy(*args, **kwargs):
        add_calls.append(kwargs.get("id", "?"))
        return original_add(*args, **kwargs)

    monkeypatch.setattr(daemon._scheduler, "add_job", _spy)
    await daemon.sync()
    # No add_job call should have happened — schedule config did not change.
    assert add_calls == []


async def test_sync_replaces_when_config_changes(daemon, store):
    """A schedule whose config changes must be re-registered on the next sync.

    :param daemon: SchedulerDaemon fixture.
    :param store: ScheduleStore fixture.
    """
    import yaml

    job = store.create(_recurring())
    await daemon.start()
    initial_hash = daemon._known[job.id]
    # Mutate the on-disk YAML through a round-trip so the new value stays
    # a string (PyYAML auto-quotes ambiguous scalars on safe_dump).
    (yaml_path,) = list(store.schedules_dir.glob("report-*.yaml"))
    payload = yaml.safe_load(yaml_path.read_text())
    payload["schedule"]["times"] = ["10:30"]
    yaml_path.write_text(yaml.safe_dump(payload, sort_keys=False))
    await daemon.sync()
    assert daemon._known[job.id] != initial_hash


# -------------------------------------------------------------- fire


async def test_fire_invokes_executor_with_run_path(daemon, store, executor, tmp_path):
    """Verify _fire invokes the executor with the schedule's run_path.

    :param daemon: SchedulerDaemon fixture.
    :param store: ScheduleStore fixture.
    :param executor: recording executor fixture.
    :param tmp_path: pytest temp directory fixture.
    """
    run_path = str(tmp_path / "run")
    job = store.create(_recurring(run_path=run_path))
    await daemon.start()
    await daemon._fire(job.id)
    assert len(executor.calls) == 1
    fired_job, working_dir = executor.calls[0]
    assert fired_job.id == job.id
    assert fired_job.conduit_name == "report"
    assert working_dir == Path(run_path)


async def test_fire_passes_inputs(daemon, store, executor):
    """Verify _fire passes the schedule's inputs to the executor.

    :param daemon: SchedulerDaemon fixture.
    :param store: ScheduleStore fixture.
    :param executor: recording executor fixture.
    """
    payload = CreateScheduleInput.model_validate(
        {
            "conduit_name": "report",
            "inputs": {"date": "today", "region": "us"},
            "run_path": "/tmp/run",
            "schedule": {
                "mode": "recurring",
                "name": "x",
                "days": [1],
                "times": ["09:00"],
            },
        }
    )
    job = store.create(payload)
    await daemon.start()
    await daemon._fire(job.id)
    fired_job, _ = executor.calls[0]
    assert fired_job.inputs == {"date": "today", "region": "us"}


async def test_fire_marks_one_shot_fired_state(daemon, store, executor):
    """Verify _fire marks fired-state for one-shot schedules.

    :param daemon: SchedulerDaemon fixture.
    :param store: ScheduleStore fixture.
    :param executor: recording executor fixture.
    """
    job = store.create(_once())
    await daemon.start()
    await daemon._fire(job.id)
    assert store.fired_at(job.id) is not None


async def test_fire_does_not_mark_state_for_recurring(daemon, store, executor):
    """Verify _fire does not mark fired-state for recurring schedules.

    :param daemon: SchedulerDaemon fixture.
    :param store: ScheduleStore fixture.
    :param executor: recording executor fixture.
    """
    job = store.create(_recurring())
    await daemon.start()
    await daemon._fire(job.id)
    assert store.fired_at(job.id) is None


async def test_fire_failure_still_marks_one_shot_fired(daemon, store, executor):
    """A failed one-shot fire MUST mark fired-state so it does not retry forever.

    :param daemon: SchedulerDaemon fixture.
    :param store: ScheduleStore fixture.
    :param executor: recording executor fixture.
    """
    job = store.create(_once())
    await daemon.start()
    executor.raise_on_next = True
    await daemon._fire(job.id)  # must NOT raise
    assert store.fired_at(job.id) is not None


async def test_fire_failure_does_not_increment_runs(daemon, store, executor):
    """A failed fire MUST NOT advance the runs_completed counter.

    :param daemon: SchedulerDaemon fixture.
    :param store: ScheduleStore fixture.
    :param executor: recording executor fixture.
    """
    job = store.create(_recurring())
    await daemon.start()
    executor.raise_on_next = True
    await daemon._fire(job.id)
    refreshed = store.get(job.id)
    assert refreshed is not None
    assert refreshed.runs_completed == 0


async def test_fire_skips_deleted_schedules(daemon, store, executor):
    """Verify _fire skips schedules that have been deleted.

    :param daemon: SchedulerDaemon fixture.
    :param store: ScheduleStore fixture.
    :param executor: recording executor fixture.
    """
    job = store.create(_recurring())
    await daemon.start()
    store.delete(job.id)
    await daemon._fire(job.id)
    assert executor.calls == []


# -------------------------------------------------------------- run history


async def test_fire_records_succeeded_with_flow_id(daemon, store, executor):
    """A successful fire records a 'succeeded' record carrying the flow id.

    :param daemon: SchedulerDaemon fixture.
    :param store: ScheduleStore fixture.
    :param executor: recording executor fixture.
    """
    job = store.create(_recurring())
    await daemon.start()
    executor.report_flow_id = "FLOW-ok"
    await daemon._fire(job.id)
    history = store.run_history(job.id)
    assert len(history) == 1
    assert history[-1].status == "succeeded"
    assert history[-1].flow_id == "FLOW-ok"


async def test_fire_records_failed_with_captured_flow_id(daemon, store, executor):
    """A failing fire records a 'failed' record still carrying the captured id.

    :param daemon: SchedulerDaemon fixture.
    :param store: ScheduleStore fixture.
    :param executor: recording executor fixture.
    """
    job = store.create(_recurring())
    await daemon.start()
    executor.report_flow_id = "FLOW-doomed"
    executor.raise_on_next = True
    await daemon._fire(job.id)  # must NOT raise
    history = store.run_history(job.id)
    assert len(history) == 1
    assert history[-1].status == "failed"
    assert history[-1].flow_id == "FLOW-doomed"


async def test_fire_records_failure_without_flow_id(daemon, store, executor):
    """A fire that fails before reporting an id records a null flow id.

    :param daemon: SchedulerDaemon fixture.
    :param store: ScheduleStore fixture.
    :param executor: recording executor fixture.
    """
    job = store.create(_recurring())
    await daemon.start()
    executor.report_flow_id = None  # never reports → fails before on_started
    executor.raise_on_next = True
    await daemon._fire(job.id)
    history = store.run_history(job.id)
    assert len(history) == 1
    assert history[-1].status == "failed"
    assert history[-1].flow_id is None


async def test_fire_records_recurring_schedules(daemon, store, executor):
    """Recurring schedules accumulate history across repeated fires.

    :param daemon: SchedulerDaemon fixture.
    :param store: ScheduleStore fixture.
    :param executor: recording executor fixture.
    """
    job = store.create(_recurring())
    await daemon.start()
    await daemon._fire(job.id)
    await daemon._fire(job.id)
    assert len(store.run_history(job.id)) == 2


async def test_compute_planned_view_attaches_last_run(daemon, store, executor, tmp_path):
    """compute_planned_view surfaces the schedule's most recent run.

    :param daemon: SchedulerDaemon fixture.
    :param store: ScheduleStore fixture.
    :param executor: recording executor fixture.
    :param tmp_path: pytest temp directory fixture.
    """
    job = store.create(_recurring())
    await daemon.start()
    executor.report_flow_id = "FLOW-glance"
    await daemon._fire(job.id)
    view = compute_planned_view(
        store, default_zone=UTC, default_working_dir=tmp_path
    )
    assert view[0].last_run is not None
    assert view[0].last_run.flow_id == "FLOW-glance"
    assert view[0].last_run.status == "succeeded"


async def test_fire_increments_runs_completed(daemon, store, executor):
    """Verify a successful fire increments runs_completed.

    :param daemon: SchedulerDaemon fixture.
    :param store: ScheduleStore fixture.
    :param executor: recording executor fixture.
    """
    job = store.create(_recurring())
    await daemon.start()
    await daemon._fire(job.id)
    refreshed = store.get(job.id)
    assert refreshed is not None
    assert refreshed.runs_completed == 1


# -------------------------------------------------------------- one-shot fired skipping


async def test_already_fired_one_shot_is_not_registered(daemon, store, executor):
    """Verify an already-fired one-shot is not re-registered on start.

    :param daemon: SchedulerDaemon fixture.
    :param store: ScheduleStore fixture.
    :param executor: recording executor fixture.
    """
    job = store.create(_once())
    store.mark_fired(job.id)
    await daemon.start()
    assert daemon._scheduler.get_job(job.id) is None


# -------------------------------------------------------------- concurrency


async def test_fire_runs_concurrently_for_distinct_schedules(
    daemon, store, executor
):
    """Verify _fire can run concurrently for distinct schedules.

    :param daemon: SchedulerDaemon fixture.
    :param store: ScheduleStore fixture.
    :param executor: recording executor fixture.
    """
    a = store.create(_recurring("a"))
    b = store.create(_recurring("b"))
    await daemon.start()
    await asyncio.gather(daemon._fire(a.id), daemon._fire(b.id))
    ids = {c[0].id for c in executor.calls}
    assert ids == {a.id, b.id}


async def test_fire_records_the_top_flow_not_a_nested_one(tmp_path, monkeypatch, store):
    """A conduit that nests another records its own id, not its child's.

    :param tmp_path: pytest temp directory fixture.
    :param monkeypatch: pytest monkeypatch fixture.
    :param store: ScheduleStore fixture.
    """
    monkeypatch.setenv("ATELIER_GLOBAL_ATELIER_DIR", str(tmp_path / "global"))
    project = tmp_path / "project"
    conduits = project / ".atelier" / "conduits"
    for name, task, tool in (("child", "echo hi", "tool:bash"), ("parent", "child", "tool:conduit")):
        (conduits / name).mkdir(parents=True)
        (conduits / name / "conduit.yaml").write_text(
            f"name: {name}\ndescription: d\ntasks:\n  - t:\n      description: t\n"
            f"      task: {task}\n      tool: {tool}\n      depends_on: []\n"
        )
    job = store.create(_recurring(conduit="parent", run_path=str(project)))
    daemon = SchedulerDaemon(store, default_zone=UTC, default_working_dir=tmp_path)

    await daemon._fire(job.id)

    (record,) = store.run_history(job.id)
    assert record.status == "succeeded"
    assert record.flow_id.endswith("_parent")
