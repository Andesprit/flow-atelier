"""Tests for the YAML-backed ScheduleStore."""
from __future__ import annotations

import pytest
import yaml

from flow_atelier.schemas.api import CreateScheduleInput
from flow_atelier.services.scheduler.store import ScheduleStore

# ----------------------------------------------------------- fixtures / helpers


@pytest.fixture
def store(tmp_path) -> ScheduleStore:
    """Provide a ScheduleStore rooted at a temp .atelier directory.

    :param tmp_path: pytest temp directory fixture.
    """
    return ScheduleStore(tmp_path / ".atelier")


def _recurring_payload(**overrides):
    """Build a recurring CreateScheduleInput with optional overrides.

    :param overrides: keyword overrides applied to the base payload.
    """
    base = {
        "conduit_name": "report",
        "inputs": {"foo": "bar"},
        "run_path": "/tmp/x",
        "schedule": {
            "mode": "recurring",
            "name": "weekday mornings",
            "days": [1, 2, 3, 4, 5],
            "times": ["06:00", "12:00"],
        },
    }
    base.update(overrides)
    return CreateScheduleInput.model_validate(base)


def _once_payload(**overrides):
    """Build a once-mode CreateScheduleInput with optional overrides.

    :param overrides: keyword overrides applied to the base payload.
    """
    base = {
        "conduit_name": "backfill",
        "inputs": {},
        "run_path": "/tmp/x",
        "schedule": {
            "mode": "once",
            "name": "tomorrow",
            "run_at": "2099-05-01T09:00:00Z",
        },
    }
    base.update(overrides)
    return CreateScheduleInput.model_validate(base)


def _interval_payload(**overrides):
    """Build an interval-mode CreateScheduleInput with optional overrides.

    :param overrides: keyword overrides applied to the base payload.
    """
    base = {
        "conduit_name": "poll",
        "inputs": {},
        "run_path": "/tmp/x",
        "schedule": {
            "mode": "interval",
            "name": "half hourly",
            "every_minutes": 30,
        },
    }
    base.update(overrides)
    return CreateScheduleInput.model_validate(base)


# ----------------------------------------------------------- list / create


def test_list_empty(store):
    """Verify an empty store returns no schedules.

    :param store: ScheduleStore fixture.
    """
    assert store.list() == []
    assert store.fired_at("missing") is None
    assert not store.atelier_dir.exists()
    store.mark_fired("first")
    assert store.fired_at("first") is not None
    assert not store.schedules_dir.exists()


def test_create_assigns_id_created_at_counters(store):
    """Verify create() assigns id, created_at, and counters.

    :param store: ScheduleStore fixture.
    """
    job = store.create(_recurring_payload())
    assert job.id.startswith("SCH-")
    assert job.runs_completed == 0
    assert job.created_at > 0
    assert job.conduit_name == "report"


def test_create_pins_local_timezone_when_omitted(store):
    """create() stamps the host's current zone so a later tz change can't shift fires.

    :param store: ScheduleStore fixture.
    """
    from flow_atelier.services.scheduler.triggers import default_local_zone

    job = store.create(_recurring_payload())
    assert job.schedule.timezone == default_local_zone().key


def test_create_preserves_explicit_timezone(store):
    """An explicit schedule timezone is kept verbatim, not overwritten.

    :param store: ScheduleStore fixture.
    """
    job = store.create(
        _recurring_payload(
            schedule={
                "mode": "recurring",
                "name": "ny mornings",
                "days": [1],
                "times": ["09:00"],
                "timezone": "America/New_York",
            }
        )
    )
    assert job.schedule.timezone == "America/New_York"


def test_create_persists_to_yaml_file(store):
    """Verify create() persists the schedule into a per-name YAML file.

    :param store: ScheduleStore fixture.
    """
    job = store.create(_recurring_payload())
    (yaml_path,) = list(store.schedules_dir.glob("weekday-mornings-*.yaml"))
    assert yaml_path.exists()
    raw = yaml.safe_load(yaml_path.read_text())
    assert raw["id"] == job.id
    assert raw["conduit_name"] == "report"
    assert raw["schedule"]["mode"] == "recurring"
    assert raw["schedule"]["name"] == "weekday mornings"


def test_filename_is_slugified(store):
    """Verify the filename keeps a lowercase slug of ``schedule.name`` as a
    readable prefix (a disambiguating hash is appended).

    :param store: ScheduleStore fixture.
    """
    store.create(_recurring_payload(schedule={
        "mode": "recurring",
        "name": "Weekday Mornings!",
        "days": [1], "times": ["06:00"],
    }))
    files = list(store.schedules_dir.glob("weekday-mornings-*.yaml"))
    assert len(files) == 1


def test_distinct_names_same_slug_dont_collide(store):
    """Two names that slug identically map to two distinct files, each
    retrievable by its own name and id; an exact-duplicate name still raises.

    :param store: ScheduleStore fixture.
    """
    a = store.create(_recurring_payload(schedule={
        "mode": "recurring", "name": "My Job", "days": [1], "times": ["06:00"],
    }))
    b = store.create(_recurring_payload(schedule={
        "mode": "recurring", "name": "my-job", "days": [1], "times": ["06:00"],
    }))
    assert len(list(store.schedules_dir.glob("*.yaml"))) == 2
    assert a.id != b.id
    assert store.get_by_name("My Job").id == a.id
    assert store.get_by_name("my-job").id == b.id
    assert store.get(a.id).id == a.id
    assert store.get(b.id).id == b.id
    # Exact-duplicate name still collides on one file.
    with pytest.raises(FileExistsError):
        store.create(_recurring_payload(schedule={
            "mode": "recurring", "name": "My Job", "days": [1], "times": ["06:00"],
        }))


def test_create_rejects_empty_name(store):
    """Verify create() rejects an empty schedule.name.

    :param store: ScheduleStore fixture.
    """
    with pytest.raises(ValueError):
        store.create(_once_payload(schedule={
            "mode": "once", "name": "", "run_at": "2099-05-01T09:00:00Z",
        }))


def test_create_rejects_duplicate_slug(store):
    """Verify create() refuses to overwrite an existing schedule slug.

    :param store: ScheduleStore fixture.
    """
    store.create(_recurring_payload())
    with pytest.raises(FileExistsError):
        store.create(_recurring_payload())


def test_list_drops_deleted_schedules(store):
    """Verify list() no longer surfaces a schedule whose file was deleted.

    :param store: ScheduleStore fixture.
    """
    a = store.create(_recurring_payload())
    b = store.create(_once_payload())
    listed = store.list()
    ids = [j.id for j in listed]
    assert a.id in ids and b.id in ids
    store.delete(a.id)
    listed_after = store.list()
    assert a.id not in [j.id for j in listed_after]
    assert b.id in [j.id for j in listed_after]


def test_get_by_id(store):
    """Verify get() returns the schedule matching the id.

    :param store: ScheduleStore fixture.
    """
    job = store.create(_recurring_payload())
    again = store.get(job.id)
    assert again is not None
    assert again.id == job.id


def test_get_unknown_returns_none(store):
    """Verify get() returns None for unknown ids.

    :param store: ScheduleStore fixture.
    """
    assert store.get("SCH-nope") is None


# ----------------------------------------------------------- delete (hard)


def test_delete_removes_file(store):
    """Verify delete() removes the schedule's YAML file.

    :param store: ScheduleStore fixture.
    """
    job = store.create(_recurring_payload())
    (path,) = list(store.schedules_dir.glob("weekday-mornings-*.yaml"))
    assert path.exists()
    deleted = store.delete(job.id)
    assert deleted.id == job.id
    assert not path.exists()


def test_delete_unknown_raises(store):
    """Verify delete() raises KeyError for unknown ids.

    :param store: ScheduleStore fixture.
    """
    with pytest.raises(KeyError):
        store.delete("SCH-nope")


def test_delete_clears_fired_state(store):
    """Verify delete() clears any persisted fired-state marker.

    :param store: ScheduleStore fixture.
    """
    job = store.create(_once_payload())
    store.mark_fired(job.id)
    assert store.fired_at(job.id) is not None
    store.delete(job.id)
    assert store.fired_at(job.id) is None


# ----------------------------------------------------------- fired-state


def test_fired_state_round_trip(store):
    """Verify mark_fired and fired_at round-trip the timestamp.

    :param store: ScheduleStore fixture.
    """
    job = store.create(_once_payload())
    assert store.fired_at(job.id) is None
    store.mark_fired(job.id)
    assert store.fired_at(job.id) is not None


def test_clear_fired_removes_marker(store):
    """Verify clear_fired removes the previously-stored marker.

    :param store: ScheduleStore fixture.
    """
    job = store.create(_once_payload())
    store.mark_fired(job.id)
    store.clear_fired(job.id)
    assert store.fired_at(job.id) is None


def test_state_resilient_to_corrupt_file(store):
    """Verify state methods tolerate a corrupt state file.

    :param store: ScheduleStore fixture.
    """
    store.state_path.parent.mkdir(parents=True, exist_ok=True)
    store.state_path.write_text("{not json")
    assert store.fired_at("SCH-x") is None
    store.mark_fired("SCH-x")
    assert store.fired_at("SCH-x") is not None


# ----------------------------------------------------------- atomic write


def test_atomic_write_does_not_leave_tmp(store):
    """Verify atomic writes do not leave temp files behind.

    :param store: ScheduleStore fixture.
    """
    store.create(_recurring_payload())
    leftovers = list(store.schedules_dir.glob("*.yaml.tmp"))
    assert leftovers == []


def test_increment_runs_skips_when_file_gone(store):
    """The delete-then-increment race: once a job's YAML file is unlinked,
    ``increment_runs`` must NOT resurrect it.

    :param store: ScheduleStore fixture.
    """
    job = store.create(_recurring_payload())
    (path,) = list(store.schedules_dir.glob("*.yaml"))
    path.unlink()
    store.increment_runs(job.id)
    assert not path.exists()
    assert list(store.schedules_dir.glob("*.yaml")) == []


# ----------------------------------------------------------- run history


def test_append_and_read_run_history(store):
    """append_run_record persists records; run_history reads them oldest-first.

    :param store: ScheduleStore fixture.
    """
    job = store.create(_recurring_payload())
    store.append_run_record(job.id, "succeeded", "FLOW-1")
    store.append_run_record(job.id, "failed", "FLOW-2")
    history = store.run_history(job.id)
    assert [r.flow_id for r in history] == ["FLOW-1", "FLOW-2"]
    assert history[0].status == "succeeded"
    assert history[1].status == "failed"


def test_run_history_empty_when_none(store):
    """run_history returns [] for a schedule with no recorded fires.

    :param store: ScheduleStore fixture.
    """
    job = store.create(_recurring_payload())
    assert store.run_history(job.id) == []


def test_last_run_returns_newest(store):
    """last_run returns the most recently appended record.

    :param store: ScheduleStore fixture.
    """
    job = store.create(_recurring_payload())
    assert store.last_run(job.id) is None
    store.append_run_record(job.id, "succeeded", "FLOW-old")
    store.append_run_record(job.id, "failed", "FLOW-new")
    last = store.last_run(job.id)
    assert last is not None
    assert last.flow_id == "FLOW-new"
    assert last.status == "failed"


def test_run_history_bounded_at_max(store):
    """History is trimmed to the last _MAX_HISTORY records.

    :param store: ScheduleStore fixture.
    """
    from flow_atelier.services.scheduler.store import _MAX_HISTORY

    job = store.create(_recurring_payload())
    for i in range(_MAX_HISTORY + 10):
        store.append_run_record(job.id, "succeeded", f"FLOW-{i}")
    history = store.run_history(job.id)
    assert len(history) == _MAX_HISTORY
    # Oldest 10 dropped; newest preserved.
    assert history[-1].flow_id == f"FLOW-{_MAX_HISTORY + 9}"
    assert history[0].flow_id == "FLOW-10"


def test_mark_fired_preserves_existing_history(store):
    """mark_fired must merge, not clobber an existing runs list (regression).

    :param store: ScheduleStore fixture.
    """
    job = store.create(_once_payload())
    store.append_run_record(job.id, "succeeded", "FLOW-keep")
    store.mark_fired(job.id)
    assert store.fired_at(job.id) is not None
    history = store.run_history(job.id)
    assert len(history) == 1
    assert history[0].flow_id == "FLOW-keep"


def test_append_run_record_preserves_fired_marker(store):
    """Appending history must not drop an existing fired-once marker.

    :param store: ScheduleStore fixture.
    """
    job = store.create(_once_payload())
    store.mark_fired(job.id)
    store.append_run_record(job.id, "succeeded", "FLOW-x")
    assert store.fired_at(job.id) is not None
    assert len(store.run_history(job.id)) == 1


def test_delete_drops_run_history(store):
    """Deleting a schedule clears its recorded history.

    :param store: ScheduleStore fixture.
    """
    job = store.create(_recurring_payload())
    store.append_run_record(job.id, "succeeded", "FLOW-1")
    store.delete(job.id)
    assert store.run_history(job.id) == []


def test_run_history_skips_malformed_entries(store):
    """run_history tolerates malformed entries in the runs list.

    :param store: ScheduleStore fixture.
    """
    job = store.create(_recurring_payload())
    store.append_run_record(job.id, "succeeded", "FLOW-good")
    # Inject a malformed record directly into state.
    data = store._read_state()
    data["schedules"][job.id]["runs"].append({"bogus": True})
    store._write_state(data)
    history = store.run_history(job.id)
    assert len(history) == 1
    assert history[0].flow_id == "FLOW-good"


def test_write_state_uses_unique_tmp_and_survives_concurrency(store):
    """Concurrent state writers must not corrupt scheduler_state.json.

    Regression for the fixed-name temp collision: many threads firing and
    clearing markers at once each used the same ``scheduler_state.json.tmp``
    and could rename a half-written file into place, which the loader then
    read as "nothing ever fired".

    :param store: ScheduleStore fixture.
    """
    import threading

    survivor = store.create(_once_payload(schedule={
        "mode": "once",
        "name": "survivor",
        "run_at": "2099-05-01T09:00:00Z",
    }))
    store.mark_fired(survivor.id)

    errors: list[Exception] = []

    def churn(n: int) -> None:
        try:
            for i in range(20):
                store.mark_fired(f"SCH-churn-{n}-{i}")
                store.clear_fired(f"SCH-churn-{n}-{i}")
                # Re-assert the survivor each pass; a clobbered write would
                # drop it and the final assert below would catch the loss.
                store.mark_fired(survivor.id)
        except Exception as e:  # noqa: BLE001 — surface any thread failure
            errors.append(e)

    threads = [threading.Thread(target=churn, args=(n,)) for n in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == []
    # The state file still parses and retains the marker we kept re-asserting.
    assert store.fired_at(survivor.id) is not None
    # No half-written temp files were left behind.
    assert list(store.atelier_dir.glob("scheduler_state.json.tmp*")) == []


def test_recreate_after_load_round_trips(store, tmp_path):
    """Verify a fresh ScheduleStore reloads previously persisted data.

    :param store: ScheduleStore fixture.
    :param tmp_path: pytest temp directory fixture.
    """
    job = store.create(_recurring_payload())
    fresh = ScheduleStore(tmp_path / ".atelier")
    listed = fresh.list()
    assert len(listed) == 1
    assert listed[0].id == job.id
    assert listed[0].schedule.times == ["06:00", "12:00"]


def test_interval_schedule_round_trips(store, tmp_path):
    """An interval schedule persists every_minutes across a reload.

    :param store: ScheduleStore fixture.
    :param tmp_path: pytest temp directory fixture.
    """
    job = store.create(_interval_payload())
    fresh = ScheduleStore(tmp_path / ".atelier")
    (loaded,) = fresh.list()
    assert loaded.id == job.id
    assert loaded.schedule.mode == "interval"
    assert loaded.schedule.every_minutes == 30
