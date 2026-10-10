"""Named agent sessions: which earlier conversation the engine offers a task."""
import asyncio
import json
import sys
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from flow_atelier.modules.engine import (
    ConduitValidationError,
    Engine,
    accepted_input_keys,
    validate_conduit,
)
from flow_atelier.schemas.conduit import Conduit
from flow_atelier.schemas.log import ExecutionResult
from flow_atelier.services.executor.base import ExecutorBase
from flow_atelier.services.executor.conduit import ConduitExecutor
from flow_atelier.services.executor.harness import DEFAULT_DONE_MARKER, AcpHarnessExecutor
from flow_atelier.services.store.filesystem import FilesystemStore

FAKE_AGENT = Path(__file__).resolve().parents[1] / "fixtures" / "fake_acp_agent.py"


class SessionAgent(ExecutorBase):
    """An agent double that opens ``<prefix><n>`` sessions unless told to continue one."""

    def __init__(self, prefix: str = "s", fail_first: bool = False, sleep: float = 0.0):
        """Initialize the double.

        :param prefix: prefix of the session ids this agent opens.
        :param fail_first: time out the first call after opening its session.
        :param sleep: delay (seconds) inside each call, to expose overlap.
        """
        self.prefix = prefix
        self.fail_first = fail_first
        self.sleep = sleep
        self.offered: list[tuple[str, str | None]] = []
        self.opened = 0
        self.running = 0
        self.most_running = 0

    def with_model(self, model: str, effort: str | None = None) -> "SessionAgent":
        """Stay one agent whatever model a task pins, as a real harness does.

        :param model: model id (unused).
        :param effort: reasoning-effort id (unused).
        """
        return self

    async def execute(self, task, resolved_command, context):
        """Record the session the engine offered and answer in it.

        :param task: task definition being executed.
        :param resolved_command: command string after template resolution.
        :param context: flow context provided by the engine.
        """
        self.offered.append((task.name, context.resume_session))
        self.running += 1
        self.most_running = max(self.most_running, self.running)
        await asyncio.sleep(self.sleep)
        self.running -= 1
        session = context.resume_session
        if session is None:
            self.opened += 1
            session = f"{self.prefix}{self.opened}"
        return ExecutionResult(
            exit_code=124 if self.fail_first and len(self.offered) == 1 else 0,
            output=session,
            agent_session_id=session,
            session_continued=context.resume_session is not None,
        )


@pytest.fixture
def store(tmp_path):
    """Provide a FilesystemStore rooted under the pytest temp path.

    :param tmp_path: pytest temp directory fixture.
    """
    return FilesystemStore(tmp_path / ".atelier")


def _conduit(tasks: list[dict[str, Any]], name: str = "test") -> Conduit:
    """Build a Conduit of agent tasks, each defaulting to ``harness:fake``.

    :param tasks: task dicts each containing a ``name`` and task fields.
    :param name: conduit name.
    """
    return Conduit.model_validate({
        "name": name,
        "description": "d",
        "tasks": [
            {t["name"]: {"description": "d", "task": "x", "tool": "harness:fake",
                         **{k: v for k, v in t.items() if k != "name"}}}
            for t in tasks
        ],
    })


async def test_tasks_naming_one_session_continue_it(store):
    """The second task is offered the session the first opened, and the log says so.

    :param store: FilesystemStore fixture.
    """
    agent = SessionAgent()
    flow_id = await Engine({"harness:fake": agent}, store).run(
        _conduit([
            {"name": "build", "session": "worker"},
            {"name": "plain", "depends_on": ["build"]},
            {"name": "repair", "session": "worker", "depends_on": ["plain"]},
        ]),
        {},
    )
    assert agent.offered == [("build", None), ("plain", None), ("repair", "s1")]
    assert store.read_sessions(flow_id) == {"worker": {"id": "s1", "harness": "fake"}}
    extras = {entry.task: entry.extra for entry in store.read_logs(flow_id)}
    assert extras == {
        "build": {"session": "worker", "session_continued": False},
        "plain": {},
        "repair": {"session": "worker", "session_continued": True},
    }


async def test_a_session_another_agent_opened_is_not_offered(store):
    """One agent cannot reload another's conversation, so it opens its own.

    :param store: FilesystemStore fixture.
    """
    one, two = SessionAgent("one-"), SessionAgent("two-")
    flow_id = await Engine({"harness:one": one, "harness:two": two}, store).run(
        _conduit([
            {"name": "a", "session": "shared", "tool": "harness:one"},
            {"name": "b", "session": "shared", "tool": "harness:two:big-model",
             "depends_on": ["a"]},
            {"name": "c", "session": "shared", "tool": "harness:two", "depends_on": ["b"]},
        ]),
        {},
    )
    assert two.offered == [("b", None), ("c", "two-1")]
    assert store.read_sessions(flow_id) == {"shared": {"id": "two-1", "harness": "two"}}


async def test_a_name_built_from_an_input_starts_afresh_when_it_changes(store):
    """A templated name is how a recipe decides where one conversation ends.

    :param store: FilesystemStore fixture.
    """
    conduit = _conduit([
        {"name": "a", "session": "fix-{{inputs.ticket}}"},
        {"name": "b", "session": "fix-{{inputs.ticket}}", "depends_on": ["a"]},
        {"name": "c", "session": "fix-{{b.output}}", "depends_on": ["b"]},
        {"name": "d", "session": " {{inputs.blank}} ", "depends_on": ["c"]},
    ])
    assert accepted_input_keys(conduit) == {"ticket", "blank"}
    agent = SessionAgent()
    flow_id = await Engine({"harness:fake": agent}, store).run(
        conduit, {"ticket": "7", "blank": ""}
    )
    # A name that resolves to nothing is no session at all.
    assert agent.offered == [("a", None), ("b", "s1"), ("c", None), ("d", None)]
    assert set(store.read_sessions(flow_id)) == {"fix-7", "fix-s1"}


async def test_a_looping_task_continues_its_own_session(store):
    """Every later iteration is a new turn in the first iteration's session.

    :param store: FilesystemStore fixture.
    """
    agent = SessionAgent()
    await Engine({"harness:fake": agent}, store).run(
        _conduit([{"name": "polish", "session": "worker", "repeat": 3}]), {}
    )
    assert [session for _, session in agent.offered] == [None, "s1", "s1"]


async def test_a_retry_continues_the_session_a_timed_out_attempt_opened(store):
    """A timeout keeps the conversation instead of losing what the agent learned.

    :param store: FilesystemStore fixture.
    """
    agent = SessionAgent(fail_first=True)
    await Engine({"harness:fake": agent}, store).run(
        _conduit([{"name": "build", "session": "worker", "retries": 1}]), {}
    )
    assert agent.offered == [("build", None), ("build", "s1")]


async def test_a_child_conduit_run_again_continues_the_session(store):
    """Sessions belong to the top-level run, so each child run can find them.

    :param store: FilesystemStore fixture.
    """
    store.write_conduit(_conduit([{"name": "build", "session": "worker"}], name="child"))
    parent = Conduit.model_validate({
        "name": "parent", "description": "d",
        "tasks": [{"again": {"description": "d", "task": "child",
                             "tool": "tool:conduit", "repeat": 2}}],
    })
    agent = SessionAgent()
    engine = Engine({"harness:fake": agent, "tool:conduit": ConduitExecutor()}, store)
    flow_id = await engine.run(parent, {})
    assert agent.offered == [("build", None), ("build", "s1")]
    assert store.read_sessions(flow_id) == {"worker": {"id": "s1", "harness": "fake"}}
    assert all(store.read_sessions(child) == {} for child in store.list_child_flows(flow_id))


async def test_tasks_sharing_a_session_take_turns(store):
    """Two prompts never reach one conversation at once, even from parallel tasks.

    :param store: FilesystemStore fixture.
    """
    agent = SessionAgent(sleep=0.05)
    await Engine({"harness:fake": agent}, store).run(
        _conduit([
            {"name": "a", "session": "worker"},
            {"name": "b", "session": "worker"},
            {"name": "c"},
        ]),
        {},
    )
    assert agent.most_running == 2  # c overlaps; a and b do not
    shared = [session for name, session in agent.offered if name in "ab"]
    assert shared == [None, "s1"]


def test_session_is_only_for_agent_tasks_and_follows_the_dependency_rule():
    """A shell task has no conversation, and a name cannot read an unordered task."""
    with pytest.raises(ValidationError, match="session requires a harness"):
        _conduit([{"name": "a", "session": "worker", "tool": "tool:bash"}])
    with pytest.raises(ValidationError, match="session must not be empty"):
        _conduit([{"name": "a", "session": "  "}])
    with pytest.raises(ConduitValidationError, match="not in its depends_on chain"):
        validate_conduit(_conduit([
            {"name": "a"},
            {"name": "b", "session": "fix-{{a.output}}"},
        ]))


async def test_a_real_agent_process_is_asked_to_resume_and_the_supervisor_is_not(
    store, tmp_path
):
    """End to end over ACP: the second task resumes; the supervisor never does.

    :param store: FilesystemStore fixture.
    :param tmp_path: pytest temp directory fixture.
    """
    records = {name: tmp_path / name for name in ("worker", "supervisor")}
    for directory in records.values():
        directory.mkdir()

    def agent(name: str, turns: list[dict[str, Any]]) -> AcpHarnessExecutor:
        script = {"reload": "resume", "record_path": str(records[name]), "turns": turns}
        return AcpHarnessExecutor(
            [sys.executable, str(FAKE_AGENT), "--script", json.dumps(script)]
        )

    def events(name: str) -> list[str]:
        return sorted(
            f"{event['event']}:{event.get('session_id', '')}"
            for log in records[name].glob("*.events")
            for event in map(json.loads, log.read_text().splitlines())
            if "session" in event["event"]
        )

    answer = json.dumps({"action": "answer", "reply": "Use the existing one"})
    conduit = Conduit.model_validate({
        "name": "test", "description": "d",
        "interaction": {"questions": "supervisor",
                        "supervisor": {"tool": "harness:supervisor"}},
        "tasks": [
            {"build": {"description": "d", "task": "x", "tool": "harness:worker",
                       "session": "worker"}},
            {"repair": {"description": "d", "task": "x", "tool": "harness:worker",
                        "session": "worker", "depends_on": ["build"]}},
        ],
    })
    engine = Engine(
        {
            "harness:worker": agent("worker", [
                {"chunks": ["Which convention?"]},
                {"chunks": ["Finished. " + DEFAULT_DONE_MARKER]},
            ]),
            "harness:supervisor": agent("supervisor", [{"chunks": [answer]}]),
        },
        store,
    )
    flow_id = await engine.run(conduit, {}, working_dir=tmp_path)
    assert events("worker") == ["new_session:", "resume_session:fake-session-1"]
    assert events("supervisor") == ["new_session:", "new_session:"]
    assert store.read_sessions(flow_id) == {
        "worker": {"id": "fake-session-1", "harness": "worker"}
    }
