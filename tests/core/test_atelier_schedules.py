"""Atelier facade: schedule CRUD."""
from __future__ import annotations

import pytest

from flow_atelier.core.atelier import Atelier
from flow_atelier.schemas.api import CreateScheduleInput, ScheduledJob


@pytest.fixture
def atelier(tmp_path, monkeypatch):
    """Construct an Atelier instance rooted under tmp_path.

    :param tmp_path: pytest temp directory fixture.
    :param monkeypatch: pytest monkeypatch fixture.
    """
    monkeypatch.delenv("ATELIER_GLOBAL_ATELIER_DIR", raising=False)
    atelier = Atelier(base_dir=tmp_path / ".atelier")
    # create_schedule validates conduit_name against the store and rejects
    # inputs the conduit cannot use, so the conduit the fixtures schedule
    # must exist on disk and reference the `foo` input the payloads send.
    conduit_dir = tmp_path / ".atelier" / "conduits" / "report"
    conduit_dir.mkdir(parents=True)
    (conduit_dir / "conduit.yaml").write_text(
        "name: report\n"
        "description: test conduit\n"
        "tasks:\n"
        "  - greet:\n"
        "      description: say hi\n"
        '      task: "echo {{inputs.foo}}"\n'
        "      tool: tool:bash\n"
        "      depends_on: []\n"
    )
    return atelier


def _payload(**overrides) -> CreateScheduleInput:
    """Build a CreateScheduleInput payload with overrides applied.

    :param overrides: keyword overrides merged into the base payload.
    """
    base = {
        "conduit_name": "report",
        "inputs": {"foo": "bar"},
        "run_path": "/tmp/x",
        "schedule": {
            "mode": "recurring",
            "name": "weekday mornings",
            "days": [1, 2, 3, 4, 5],
            "times": ["06:00"],
        },
    }
    base.update(overrides)
    return CreateScheduleInput.model_validate(base)


def test_list_schedules_starts_empty(atelier):
    """Verify list_schedules returns [] for a fresh Atelier.

    :param atelier: Atelier facade fixture.
    """
    assert atelier.list_schedules() == []


def test_create_schedule_returns_scheduled_job(atelier):
    """Verify create_schedule returns a ScheduledJob with a SCH- id.

    :param atelier: Atelier facade fixture.
    """
    job = atelier.create_schedule(_payload())
    assert isinstance(job, ScheduledJob)
    assert job.conduit_name == "report"
    assert job.id.startswith("SCH-")


def test_create_schedule_rejects_unknown_conduit(atelier):
    """Verify create_schedule rejects a conduit_name with no conduit on disk.

    :param atelier: Atelier facade fixture.
    """
    with pytest.raises(ValueError, match="unknown conduit"):
        atelier.create_schedule(_payload(conduit_name="does-not-exist"))
    assert atelier.list_schedules() == []


def test_create_schedule_rejects_missing_required_input(atelier, tmp_path):
    """Verify create_schedule rejects a schedule omitting a required input.

    A conduit input with no ``default`` is required; a schedule that fails to
    supply it would fail on every fire (swallowed into daemon logs), so it must
    be rejected loudly at create time.

    :param atelier: Atelier facade fixture.
    :param tmp_path: pytest temp directory fixture.
    """
    conduit_dir = tmp_path / ".atelier" / "conduits" / "needs_input"
    conduit_dir.mkdir(parents=True)
    (conduit_dir / "conduit.yaml").write_text(
        "name: needs_input\n"
        "description: requires an input\n"
        "inputs:\n"
        "  target:\n"
        "    description: who to greet\n"
        "tasks:\n"
        "  - greet:\n"
        "      description: say hi\n"
        '      task: "echo {{inputs.target}}"\n'
        "      tool: tool:bash\n"
        "      depends_on: []\n"
    )
    with pytest.raises(ValueError, match="missing required inputs"):
        atelier.create_schedule(_payload(conduit_name="needs_input", inputs={}))
    assert atelier.list_schedules() == []


def test_create_schedule_rejects_unknown_input(atelier, tmp_path):
    """Verify create_schedule rejects an input key the conduit cannot use.

    A typo of a declared key would otherwise install cleanly and fire with
    the default forever. It is reported as the typo (with a suggestion), not
    as a missing required input, even though the real key is absent too.

    :param atelier: Atelier facade fixture.
    :param tmp_path: pytest temp directory fixture.
    """
    conduit_dir = tmp_path / ".atelier" / "conduits" / "needs_input"
    conduit_dir.mkdir(parents=True)
    (conduit_dir / "conduit.yaml").write_text(
        "name: needs_input\n"
        "description: requires an input\n"
        "inputs:\n"
        "  target:\n"
        "    description: who to greet\n"
        "tasks:\n"
        "  - greet:\n"
        "      description: say hi\n"
        '      task: "echo {{inputs.target}}"\n'
        "      tool: tool:bash\n"
        "      depends_on: []\n"
    )
    with pytest.raises(ValueError) as excinfo:
        atelier.create_schedule(
            _payload(conduit_name="needs_input", inputs={"targte": "world"})
        )
    message = str(excinfo.value)
    assert "unknown inputs: ['targte']" in message
    assert "did you mean 'target' for 'targte'?" in message
    assert "'needs_input' accepts inputs: ['target']" in message
    assert "missing required" not in message
    assert atelier.list_schedules() == []


def test_create_schedule_rejects_input_for_conduit_without_inputs(atelier, tmp_path):
    """Verify a conduit that uses no inputs says so instead of guessing.

    :param atelier: Atelier facade fixture.
    :param tmp_path: pytest temp directory fixture.
    """
    conduit_dir = tmp_path / ".atelier" / "conduits" / "plain"
    conduit_dir.mkdir(parents=True)
    (conduit_dir / "conduit.yaml").write_text(
        "name: plain\n"
        "description: no inputs\n"
        "tasks:\n"
        "  - greet:\n"
        "      description: say hi\n"
        '      task: "echo hi"\n'
        "      tool: tool:bash\n"
        "      depends_on: []\n"
    )
    with pytest.raises(ValueError) as excinfo:
        atelier.create_schedule(_payload(conduit_name="plain", inputs={"foo": "bar"}))
    message = str(excinfo.value)
    assert "unknown inputs: ['foo']" in message
    assert "did you mean" not in message
    assert "'plain' accepts no inputs" in message
    assert atelier.list_schedules() == []


def test_create_schedule_accepts_supplied_required_input(atelier, tmp_path):
    """Verify create_schedule succeeds when the required input is supplied.

    :param atelier: Atelier facade fixture.
    :param tmp_path: pytest temp directory fixture.
    """
    conduit_dir = tmp_path / ".atelier" / "conduits" / "needs_input"
    conduit_dir.mkdir(parents=True)
    (conduit_dir / "conduit.yaml").write_text(
        "name: needs_input\n"
        "description: requires an input\n"
        "inputs:\n"
        "  target:\n"
        "    description: who to greet\n"
        "tasks:\n"
        "  - greet:\n"
        "      description: say hi\n"
        '      task: "echo {{inputs.target}}"\n'
        "      tool: tool:bash\n"
        "      depends_on: []\n"
    )
    job = atelier.create_schedule(
        _payload(conduit_name="needs_input", inputs={"target": "world"})
    )
    assert job.conduit_name == "needs_input"


def test_list_schedules_includes_created(atelier):
    """Verify list_schedules includes a freshly created schedule.

    :param atelier: Atelier facade fixture.
    """
    job = atelier.create_schedule(_payload())
    listed = atelier.list_schedules()
    assert [j.id for j in listed] == [job.id]


def test_delete_schedule_removes_from_list(atelier):
    """Verify delete_schedule hard-deletes and drops the schedule from list.

    :param atelier: Atelier facade fixture.
    """
    job = atelier.create_schedule(_payload())
    deleted = atelier.delete_schedule(job.id)
    assert deleted.id == job.id
    assert atelier.list_schedules() == []


def test_delete_schedule_unknown_raises_keyerror(atelier):
    """Verify delete_schedule raises KeyError for an unknown id.

    :param atelier: Atelier facade fixture.
    """
    with pytest.raises(KeyError):
        atelier.delete_schedule("SCH-nope")
