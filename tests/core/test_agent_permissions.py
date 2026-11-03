"""Where an agent's permission level is set, validated, and announced.

The harness side (which mode is chosen, which requests are rejected) is in
``tests/services/executor/test_harness_permissions.py``. These tests cover the
setting, the task field, the context the engine hands each agent task, and the
once-per-run notice, with stub agents so no process is spawned.
"""
from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from flow_atelier.core.atelier import Atelier
from flow_atelier.core.settings import AtelierSettings
from flow_atelier.modules.engine import PERMISSIONS_NOTICE
from flow_atelier.schemas.conduit import Conduit, TaskDefinition
from flow_atelier.schemas.log import ExecutionResult
from flow_atelier.services.executor.base import ExecutorBase


class RecordingAgent(ExecutorBase):
    """An agent that records the permission inputs of every task it runs."""

    def __init__(self) -> None:
        self.seen: list[tuple[str, str | None, str | None]] = []

    async def execute(self, task, resolved_command, context) -> ExecutionResult:
        """Record ``(task, task permissions, global permissions)`` and succeed.

        :param task: the task being run.
        :param resolved_command: the resolved prompt (unused).
        :param context: the flow context.
        :returns: a successful result.
        """
        self.seen.append((task.name, task.permissions, context.agent_permissions))
        return ExecutionResult(exit_code=0, output="ok")


def _agent_task(name: str, **fields: Any) -> dict[str, Any]:
    """Build one agent task definition.

    :param name: the task name.
    :param fields: extra task fields such as ``permissions``.
    """
    return {"name": name, "description": "d", "task": "x", "tool": "harness:alpha", **fields}


def _atelier(tmp_path, **settings: Any) -> tuple[Atelier, RecordingAgent]:
    """Build an Atelier whose ``harness:alpha`` is a recording stub.

    :param tmp_path: pytest temp directory fixture.
    :param settings: extra :class:`AtelierSettings` fields.
    """
    atelier = Atelier(settings=AtelierSettings(
        _env_file=None, atelier_dir=tmp_path / ".atelier", **settings,
    ))
    agent = RecordingAgent()
    atelier.executors["harness:alpha"] = agent
    return atelier, agent


@pytest.fixture(autouse=True)
def _no_permission_env(monkeypatch):
    """Keep a developer's own ATELIER_AGENT_PERMISSIONS out of these tests.

    :param monkeypatch: pytest monkeypatch fixture.
    """
    monkeypatch.delenv("ATELIER_AGENT_PERMISSIONS", raising=False)


# ------------------------------------------------------------------- setting


def test_setting_is_unset_by_default_and_read_from_the_env(monkeypatch):
    """Unset means nobody chose; the env var sets it."""
    assert AtelierSettings(_env_file=None).agent_permissions is None
    monkeypatch.setenv("ATELIER_AGENT_PERMISSIONS", "read-only")
    assert AtelierSettings(_env_file=None).agent_permissions == "read-only"


def test_an_unknown_setting_value_names_the_valid_ones(monkeypatch):
    """A typo fails loudly with the three accepted values."""
    monkeypatch.setenv("ATELIER_AGENT_PERMISSIONS", "readonly")
    with pytest.raises(ValidationError) as exc:
        AtelierSettings(_env_file=None)
    assert "agent_permissions" in str(exc.value)
    assert "'full', 'edits' or 'read-only'" in str(exc.value)


# ---------------------------------------------------------------- task field


def test_task_permissions_accepts_the_three_levels():
    """Each level parses on an agent task, and the field is optional."""
    for level in ("full", "edits", "read-only"):
        assert TaskDefinition.model_validate(_agent_task("a", permissions=level)).permissions == level
    assert TaskDefinition.model_validate(_agent_task("a")).permissions is None


def test_task_permissions_rejects_an_unknown_level():
    """A wrong value names the valid ones."""
    with pytest.raises(ValidationError) as exc:
        TaskDefinition.model_validate(_agent_task("a", permissions="none"))
    assert "'full', 'edits' or 'read-only'" in str(exc.value)


def test_task_permissions_requires_an_agent():
    """A shell step has no agent to limit, so the field is refused there."""
    with pytest.raises(ValidationError) as exc:
        TaskDefinition.model_validate(
            {**_agent_task("a", permissions="edits"), "tool": "tool:bash"}
        )
    assert "permissions requires a harness:<name> tool" in str(exc.value)


# -------------------------------------------------------------------- notice


def _conduit(tasks: list[dict[str, Any]], **fields: Any) -> Conduit:
    """Build the ``perm`` conduit from task definitions.

    :param tasks: the task definitions.
    :param fields: extra conduit fields such as ``interaction``.
    """
    return Conduit.model_validate({"name": "perm", "description": "d", "tasks": tasks, **fields})


async def test_notice_prints_once_per_run_when_nobody_chose(tmp_path, capsys):
    """Two agent tasks and a nested one still print a single line."""
    atelier, agent = _atelier(tmp_path)
    atelier.store.write_conduit(Conduit.model_validate({
        "name": "child", "description": "d", "tasks": [_agent_task("inner")],
    }))
    atelier.store.write_conduit(_conduit([
        _agent_task("one"),
        _agent_task("two", depends_on=["one"]),
        {"name": "nested", "description": "d", "task": "child", "tool": "tool:conduit",
         "depends_on": ["two"]},
    ]))
    await atelier.run_conduit("perm", {})
    assert capsys.readouterr().err.count(PERMISSIONS_NOTICE) == 1
    assert [name for name, _, _ in agent.seen] == ["one", "two", "inner"]

    # A second run is a new run, so it says it again.
    await atelier.run_conduit("perm", {})
    assert capsys.readouterr().err.count(PERMISSIONS_NOTICE) == 1


def test_notice_names_the_fix():
    """The line says what agents can do and both ways to limit them."""
    assert "full permissions" in PERMISSIONS_NOTICE
    assert "permissions: edits or read-only" in PERMISSIONS_NOTICE
    # `atelier ask` has no task to set permissions on; the variable works there.
    assert "ATELIER_AGENT_PERMISSIONS" in PERMISSIONS_NOTICE
    # A binary install has no docs folder: the link must work from anywhere.
    assert "https://github.com/Andesprit/flow-atelier/blob/main/docs/guide.md#security" in (
        PERMISSIONS_NOTICE
    )


async def test_notice_is_kept_in_the_run_for_the_run_page(tmp_path):
    """The web UI never sees stderr, so the line is also one of the task's steps."""
    atelier, _ = _atelier(tmp_path)
    atelier.store.write_conduit(_conduit([_agent_task("one"), _agent_task("two")]))
    await atelier.run_conduit("perm", {})
    flow_id = atelier.store.list_flows()[0]
    records, _ = atelier.store.read_steps(flow_id)
    texts = [(r.task, r.step.text) for r in records]
    assert texts.count(("one", PERMISSIONS_NOTICE)) == 1
    assert ("two", PERMISSIONS_NOTICE) not in texts
    # What GET /flows/<id>/tasks/<task>/log serves to the run page.
    for task, expected in (("one", 1), ("two", 0)):
        log = atelier.get_task_log(flow_id, task)
        lines = [line.text for r in log.rounds for line in r.lines]
        assert sum("full permissions" in text for text in lines) == expected


async def test_choosing_full_in_the_env_silences_the_notice(tmp_path, capsys, monkeypatch):
    """Setting the env var, even to full, is a choice; it reaches every task."""
    monkeypatch.setenv("ATELIER_AGENT_PERMISSIONS", "full")
    atelier, agent = _atelier(tmp_path)
    atelier.store.write_conduit(_conduit([_agent_task("one")]))
    await atelier.run_conduit("perm", {})
    assert PERMISSIONS_NOTICE not in capsys.readouterr().err
    assert agent.seen == [("one", None, "full")]


async def test_a_task_that_chose_is_silent(tmp_path, capsys):
    """Only a task with unchosen full permissions triggers the line."""
    atelier, agent = _atelier(tmp_path)
    atelier.store.write_conduit(_conduit([
        _agent_task("one", permissions="full"),
        _agent_task("two", permissions="read-only"),
    ]))
    await atelier.run_conduit("perm", {})
    assert PERMISSIONS_NOTICE not in capsys.readouterr().err
    assert sorted(agent.seen) == [("one", "full", None), ("two", "read-only", None)]


async def test_a_run_whose_requests_a_person_answers_is_silent(tmp_path, capsys):
    """``interaction.permissions: human`` shows every request before it is allowed."""
    atelier, _ = _atelier(tmp_path)
    atelier.store.write_conduit(_conduit(
        [_agent_task("one")], interaction={"permissions": "human"},
    ))
    await atelier.run_conduit("perm", {})
    assert PERMISSIONS_NOTICE not in capsys.readouterr().err


async def test_a_run_without_agents_is_silent(tmp_path, capsys):
    """Shell steps are not agents."""
    atelier, _ = _atelier(tmp_path)
    atelier.store.write_conduit(_conduit([
        {"name": "sh", "description": "d", "task": "echo hi", "tool": "tool:bash"},
    ]))
    await atelier.run_conduit("perm", {})
    assert PERMISSIONS_NOTICE not in capsys.readouterr().err
