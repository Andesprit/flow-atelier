"""Tests for engine resume-from-failed-flow functionality."""
from __future__ import annotations

from typing import Any

import pytest

from flow_atelier.modules.engine import Engine
from flow_atelier.schemas.conduit import Conduit
from flow_atelier.schemas.log import ExecutionResult
from flow_atelier.schemas.progress import FlowStatus, Progress, TaskStatus
from flow_atelier.services.executor.base import ExecutorBase
from flow_atelier.services.store.filesystem import FilesystemStore


class FakeExecutor(ExecutorBase):
    def __init__(
        self,
        outputs: dict[str, str] | None = None,
        fail: set[str] | None = None,
    ):
        """Initialize the fake executor.

        :param outputs: optional task-name to stdout mapping.
        :param fail: optional set of task names that should fail.
        """
        self.outputs = outputs or {}
        self.fail = fail or set()
        self.calls: list[str] = []

    async def execute(self, task, resolved_command, context):
        """Record the call and return a scripted ExecutionResult.

        :param task: task definition being executed.
        :param resolved_command: command string after template resolution.
        :param context: flow context provided by the engine.
        """
        self.calls.append(task.name)
        if task.name in self.fail:
            return ExecutionResult(exit_code=1, stderr="boom", output="")
        out = self.outputs.get(task.name, f"out-{task.name}")
        return ExecutionResult(exit_code=0, output=out, stdout=out)


class ScriptedExecutor(ExecutorBase):
    """Returns one scripted ExecutionResult per call, recording commands."""

    def __init__(self, script: list[ExecutionResult]):
        """Initialize with the per-call result script.

        :param script: results returned in order, one per execute call.
        """
        self.script = list(script)
        self.commands: list[str] = []

    async def execute(self, task, resolved_command, context):
        """Record the resolved command and pop the next scripted result.

        :param task: task definition being executed.
        :param resolved_command: command string after template resolution.
        :param context: flow context provided by the engine.
        """
        self.commands.append(resolved_command)
        return self.script.pop(0)


def _ok(out: str, last_turn: str | None = None) -> ExecutionResult:
    """Build a successful ExecutionResult.

    :param out: aggregate output value.
    :param last_turn: optional last-turn output value.
    """
    return ExecutionResult(
        exit_code=0, output=out, stdout=out, last_turn_output=last_turn
    )


_FAIL = ExecutionResult(exit_code=1, stderr="boom", output="")


@pytest.fixture
def store(tmp_path):
    """Provide a FilesystemStore rooted under the pytest temp path.

    :param tmp_path: pytest temp directory fixture.
    """
    return FilesystemStore(tmp_path / ".atelier")


def _conduit(tasks: list[dict[str, Any]], **kw: Any) -> Conduit:
    """Build a Conduit model from a list of task dicts plus optional fields.

    :param tasks: task dicts each containing a ``name`` and task fields.
    :param kw: extra top-level conduit fields.
    """
    body = {
        "name": "test",
        "description": "d",
        "tasks": [{t["name"]: {k: v for k, v in t.items() if k != "name"}} for t in tasks],
        **kw,
    }
    return Conduit.model_validate(body)


async def _seed_failed_flow(
    store: FilesystemStore,
    conduit: Conduit,
    fail: set[str],
    outputs: dict[str, str] | None = None,
) -> str:
    """Run a conduit to failure and return its flow_id.

    :param store: FilesystemStore fixture.
    :param conduit: conduit to run.
    :param fail: task names that should fail.
    :param outputs: optional task-name to output mapping for non-failing tasks.
    :returns: the flow_id of the failed run.
    """
    fake = FakeExecutor(outputs=outputs, fail=fail)
    engine = Engine({"tool:bash": fake}, store)
    with pytest.raises(RuntimeError):
        await engine.run(conduit, {})
    return store.list_flows()[0]


# ---------------------------------------------------------------- resume happy path


async def test_resume_skips_completed_tasks(store):
    """Verify resume_from skips already-completed tasks and only runs the failed one.

    :param store: FilesystemStore fixture.
    """
    conduit = _conduit(
        [
            {"name": "a", "description": "d", "task": "x", "tool": "tool:bash", "depends_on": []},
            {"name": "b", "description": "d", "task": "x", "tool": "tool:bash", "depends_on": ["a"]},
        ]
    )
    # Seed: task a completes, task b fails
    flow_id = await _seed_failed_flow(store, conduit, fail={"b"})

    # Resume: only task b should run
    fake = FakeExecutor(outputs={"b": "recovered"})
    engine = Engine({"tool:bash": fake}, store)
    result_id = await engine.run(conduit, {}, resume_from=flow_id)
    assert result_id == flow_id
    assert fake.calls == ["b"]

    p = store.read_progress(flow_id)
    assert p.status == FlowStatus.completed
    assert p.tasks["a"].status == TaskStatus.completed
    assert p.tasks["b"].status == TaskStatus.completed


async def test_resume_reuses_prior_outputs(store):
    """Verify resumed tasks can access outputs from completed tasks in the prior run.

    :param store: FilesystemStore fixture.
    """
    conduit = _conduit(
        [
            {"name": "a", "description": "d", "task": "x", "tool": "tool:bash", "depends_on": []},
            {"name": "b", "description": "d", "task": "echo {{a.output}}", "tool": "tool:bash",
             "depends_on": ["a"]},
        ]
    )
    # Seed: a outputs "alpha", b fails
    flow_id = await _seed_failed_flow(store, conduit, fail={"b"}, outputs={"a": "alpha"})

    captured = {}

    class Capturing(FakeExecutor):
        async def execute(self, task, resolved_command, context):
            """Capture the resolved command.

            :param task: task definition being executed.
            :param resolved_command: command string after template resolution.
            :param context: flow context provided by the engine.
            """
            captured["cmd"] = resolved_command
            return ExecutionResult(exit_code=0, output="recovered")

    engine = Engine({"tool:bash": Capturing()}, store)
    await engine.run(conduit, {}, resume_from=flow_id)
    assert captured["cmd"] == "echo alpha"


async def test_resume_preserves_flow_id(store):
    """Verify the resumed flow reuses the same flow_id.

    :param store: FilesystemStore fixture.
    """
    conduit = _conduit(
        [
            {"name": "a", "description": "d", "task": "x", "tool": "tool:bash", "depends_on": []},
        ]
    )
    flow_id = await _seed_failed_flow(store, conduit, fail={"a"})

    fake = FakeExecutor()
    engine = Engine({"tool:bash": fake}, store)
    result_id = await engine.run(conduit, {}, resume_from=flow_id)
    assert result_id == flow_id


# ---------------------------------------------------------------- resume edge cases


async def test_resume_three_task_pipeline_skips_first(store):
    """Verify resume in a 3-task pipeline skips completed tasks and runs from the failed point.

    :param store: FilesystemStore fixture.
    """
    conduit = _conduit(
        [
            {"name": "a", "description": "d", "task": "x", "tool": "tool:bash", "depends_on": []},
            {"name": "b", "description": "d", "task": "x", "tool": "tool:bash", "depends_on": ["a"]},
            {"name": "c", "description": "d", "task": "x", "tool": "tool:bash", "depends_on": ["b"]},
        ]
    )
    # a completes, b fails, c never starts
    flow_id = await _seed_failed_flow(store, conduit, fail={"b"})

    fake = FakeExecutor(outputs={"b": "ok-b", "c": "ok-c"})
    engine = Engine({"tool:bash": fake}, store)
    await engine.run(conduit, {}, resume_from=flow_id)
    assert fake.calls == ["b", "c"]


async def test_resume_fires_on_task_event_only_for_rerun_tasks(store):
    """Verify on_task_event only fires for tasks actually re-executed, not skipped ones.

    :param store: FilesystemStore fixture.
    """
    conduit = _conduit(
        [
            {"name": "a", "description": "d", "task": "x", "tool": "tool:bash", "depends_on": []},
            {"name": "b", "description": "d", "task": "x", "tool": "tool:bash", "depends_on": ["a"]},
        ]
    )
    flow_id = await _seed_failed_flow(store, conduit, fail={"b"})

    fake = FakeExecutor(outputs={"b": "ok"})
    engine = Engine({"tool:bash": fake}, store)
    events = []
    await engine.run(conduit, {}, resume_from=flow_id, on_task_event=events.append)
    assert [e.task for e in events] == ["b"]


async def test_resume_continues_repeat_task_at_next_iteration(store):
    """Verify a mid-loop failure resumes at the next iteration with seeded history.

    :param store: FilesystemStore fixture.
    """
    conduit = _conduit(
        [
            {"name": "loop", "description": "d", "task": "p=[{{loop.previous}}]",
             "tool": "tool:bash", "depends_on": [], "repeat": 5},
        ]
    )
    # Seed: iterations 1-2 succeed (iteration 2 has a distinct last-turn
    # output), iteration 3 fails.
    seed = ScriptedExecutor([_ok("o1"), _ok("o2-full", last_turn="o2-last"), _FAIL])
    engine = Engine({"tool:bash": seed}, store)
    with pytest.raises(RuntimeError):
        await engine.run(conduit, {})
    flow_id = store.list_flows()[0]
    assert seed.commands == ["p=[]", "p=[o1]", "p=[o2-last]"]

    # Resume: must run only iterations 3-5, seeing the prior last-turn output.
    resumed = ScriptedExecutor([_ok("o3"), _ok("o4"), _ok("o5")])
    engine = Engine({"tool:bash": resumed}, store)
    await engine.run(conduit, {}, resume_from=flow_id)
    assert resumed.commands == ["p=[o2-last]", "p=[o3]", "p=[o4]"]

    p = store.read_progress(flow_id)
    assert p.status == FlowStatus.completed
    assert p.tasks["loop"].status == TaskStatus.completed
    assert p.tasks["loop"].iteration == 5
    iterations = [e.iteration for e in store.read_logs(flow_id)]
    assert iterations == [1, 2, 3, 3, 4, 5]


async def test_resume_twice_keeps_history_consistent(store):
    """Verify a second resume still seeds history correctly from cumulative logs.

    :param store: FilesystemStore fixture.
    """
    conduit = _conduit(
        [
            {"name": "loop", "description": "d", "task": "p=[{{loop.previous}}]",
             "tool": "tool:bash", "depends_on": [], "repeat": 4},
        ]
    )
    engine = Engine({"tool:bash": ScriptedExecutor([_ok("o1"), _FAIL])}, store)
    with pytest.raises(RuntimeError):
        await engine.run(conduit, {})
    flow_id = store.list_flows()[0]

    # First resume: iteration 2 succeeds, iteration 3 fails again.
    engine = Engine({"tool:bash": ScriptedExecutor([_ok("o2"), _FAIL])}, store)
    with pytest.raises(RuntimeError):
        await engine.run(conduit, {}, resume_from=flow_id)

    # Second resume: starts at iteration 3 with history [o1, o2].
    final = ScriptedExecutor([_ok("o3"), _ok("o4")])
    engine = Engine({"tool:bash": final}, store)
    await engine.run(conduit, {}, resume_from=flow_id)
    assert final.commands == ["p=[o2]", "p=[o3]"]
    p = store.read_progress(flow_id)
    assert p.status == FlowStatus.completed
    assert p.tasks["loop"].iteration == 4


async def test_until_predicate_matches_last_turn_not_echoed_history(store):
    """Verify the loop predicate ignores text echoed from prior iterations.

    :param store: FilesystemStore fixture.
    """
    conduit = _conduit(
        [
            {"name": "loop", "description": "d", "task": "{{loop.history}}",
             "tool": "tool:bash", "depends_on": [], "repeat": 3,
             "until": "output.match(DONE)"},
        ]
    )
    # Iteration 1's full output contains DONE only in transcript noise; its
    # last turn does not. Without last-turn scoping, iteration 2 would never
    # run because iteration 2's full output echoes iteration 1 via history.
    fake = ScriptedExecutor(
        [
            _ok("working... DONE was mentioned upstream", last_turn="working"),
            _ok("echo of history then DONE", last_turn="DONE"),
        ]
    )
    engine = Engine({"tool:bash": fake}, store)
    flow_id = await engine.run(conduit, {})
    p = store.read_progress(flow_id)
    assert p.tasks["loop"].status == TaskStatus.completed
    assert p.tasks["loop"].iteration == 2
    assert len(fake.commands) == 2


async def test_until_exhaustion_completes_with_reason_by_default(store):
    """Verify a never-matching until loop completes but records exhaustion.

    :param store: FilesystemStore fixture.
    """
    conduit = _conduit(
        [
            {"name": "loop", "description": "d", "task": "x", "tool": "tool:bash",
             "depends_on": [], "repeat": 2, "until": "output.match(NEVER)"},
        ]
    )
    fake = ScriptedExecutor([_ok("a"), _ok("b")])
    engine = Engine({"tool:bash": fake}, store)
    flow_id = await engine.run(conduit, {})
    p = store.read_progress(flow_id)
    assert p.tasks["loop"].status == TaskStatus.completed
    assert p.tasks["loop"].reason == "loop exhausted without predicate match"


async def test_until_exhaustion_fails_when_on_exhaust_fail(store):
    """Verify on_exhaust: fail turns predicate exhaustion into a task failure.

    :param store: FilesystemStore fixture.
    """
    conduit = _conduit(
        [
            {"name": "loop", "description": "d", "task": "x", "tool": "tool:bash",
             "depends_on": [], "repeat": 2, "until": "output.match(NEVER)",
             "on_exhaust": "fail"},
        ]
    )
    fake = ScriptedExecutor([_ok("a"), _ok("b")])
    engine = Engine({"tool:bash": fake}, store)
    with pytest.raises(RuntimeError, match="exhausted 2 iterations"):
        await engine.run(conduit, {})
    flow_id = store.list_flows()[0]
    p = store.read_progress(flow_id)
    assert p.status == FlowStatus.failed
    assert p.tasks["loop"].status == TaskStatus.failed
    assert "exhausted" in (p.tasks["loop"].reason or "")


async def test_until_match_leaves_no_exhaustion_reason(store):
    """Verify a matched predicate completes without an exhaustion note.

    :param store: FilesystemStore fixture.
    """
    conduit = _conduit(
        [
            {"name": "loop", "description": "d", "task": "x", "tool": "tool:bash",
             "depends_on": [], "repeat": 3, "until": "output.match(HIT)"},
        ]
    )
    fake = ScriptedExecutor([_ok("miss"), _ok("HIT")])
    engine = Engine({"tool:bash": fake}, store)
    flow_id = await engine.run(conduit, {})
    p = store.read_progress(flow_id)
    assert p.tasks["loop"].status == TaskStatus.completed
    assert p.tasks["loop"].reason is None


async def test_stagnation_limit_fails_on_identical_outputs(store):
    """Verify stagnation_limit fails the task after N identical outputs.

    :param store: FilesystemStore fixture.
    """
    conduit = _conduit(
        [
            {"name": "loop", "description": "d", "task": "x", "tool": "tool:bash",
             "depends_on": [], "repeat": 5, "stagnation_limit": 2},
        ]
    )
    fake = ScriptedExecutor([_ok("same"), _ok("same"), _ok("never-reached")])
    engine = Engine({"tool:bash": fake}, store)
    with pytest.raises(RuntimeError, match="stagnated: 2 identical"):
        await engine.run(conduit, {})
    assert len(fake.commands) == 2
    flow_id = store.list_flows()[0]
    p = store.read_progress(flow_id)
    assert p.tasks["loop"].status == TaskStatus.failed
    assert "stagnated" in (p.tasks["loop"].reason or "")


async def test_stagnation_limit_allows_varied_outputs(store):
    """Verify varied outputs never trip the stagnation guard.

    :param store: FilesystemStore fixture.
    """
    conduit = _conduit(
        [
            {"name": "loop", "description": "d", "task": "x", "tool": "tool:bash",
             "depends_on": [], "repeat": 3, "stagnation_limit": 2},
        ]
    )
    fake = ScriptedExecutor([_ok("a"), _ok("b"), _ok("a")])
    engine = Engine({"tool:bash": fake}, store)
    flow_id = await engine.run(conduit, {})
    p = store.read_progress(flow_id)
    assert p.tasks["loop"].status == TaskStatus.completed
    assert len(fake.commands) == 3


async def test_stagnation_streak_resets_on_change(store):
    """Verify the identical-output streak resets when output changes.

    :param store: FilesystemStore fixture.
    """
    conduit = _conduit(
        [
            {"name": "loop", "description": "d", "task": "x", "tool": "tool:bash",
             "depends_on": [], "repeat": 5, "stagnation_limit": 3},
        ]
    )
    # same, same (streak 2), change (reset), same (streak 2) — never hits 3.
    fake = ScriptedExecutor(
        [_ok("a"), _ok("a"), _ok("b"), _ok("b"), _ok("c")]
    )
    engine = Engine({"tool:bash": fake}, store)
    flow_id = await engine.run(conduit, {})
    p = store.read_progress(flow_id)
    assert p.tasks["loop"].status == TaskStatus.completed


async def test_resume_fires_on_task_starting_for_rerun_tasks(store):
    """Verify on_task_starting fires for re-executed tasks.

    :param store: FilesystemStore fixture.
    """
    conduit = _conduit(
        [
            {"name": "a", "description": "d", "task": "x", "tool": "tool:bash", "depends_on": []},
            {"name": "b", "description": "d", "task": "x", "tool": "tool:bash", "depends_on": ["a"]},
        ]
    )
    flow_id = await _seed_failed_flow(store, conduit, fail={"b"})

    fake = FakeExecutor(outputs={"b": "ok"})
    engine = Engine({"tool:bash": fake}, store)
    starting = []
    await engine.run(
        conduit, {}, resume_from=flow_id,
        on_task_starting=lambda name, tool: starting.append(name),
    )
    assert starting == ["b"]


# ------------------------------------------------------ run_path namespace (Fix 1)


async def test_run_path_recorded_on_progress_not_in_inputs(store, tmp_path):
    """The engine's run directory is bookkeeping on progress, never a user input.

    Mixing it into input.yaml would let ``{{inputs.run_path}}`` silently work on
    a resumed run but fail on a fresh one, and would clobber an author-declared
    input literally named ``run_path``.

    :param store: FilesystemStore fixture.
    :param tmp_path: pytest temp directory fixture.
    """
    conduit = _conduit(
        [{"name": "a", "description": "d", "task": "x", "tool": "tool:bash",
          "depends_on": []}]
    )
    engine = Engine({"tool:bash": FakeExecutor()}, store)
    flow_id = await engine.run(conduit, {}, working_dir=tmp_path)

    assert "run_path" not in store.read_input(flow_id)
    assert store.read_progress(flow_id).run_path == str(tmp_path)


async def test_resume_preserves_user_declared_run_path_input(store, tmp_path):
    """An author input named ``run_path`` survives a run + resume untouched.

    :param store: FilesystemStore fixture.
    :param tmp_path: pytest temp directory fixture.
    """
    conduit = _conduit(
        [
            {"name": "a", "description": "d", "task": "{{inputs.run_path}}",
             "tool": "tool:bash", "depends_on": []},
            {"name": "b", "description": "d", "task": "x", "tool": "tool:bash",
             "depends_on": ["a"]},
        ],
        inputs={"run_path": {"description": "a user input that shadows the name"}},
    )
    fake = FakeExecutor(fail={"b"})
    engine = Engine({"tool:bash": fake}, store)
    with pytest.raises(RuntimeError):
        await engine.run(conduit, {"run_path": "USER_VALUE"}, working_dir=tmp_path)
    flow_id = store.list_flows()[0]

    # The user's value lives in inputs; the engine's run dir lives on progress.
    assert store.read_input(flow_id)["run_path"] == "USER_VALUE"
    assert store.read_progress(flow_id).run_path == str(tmp_path)

    # Resume must not overwrite the user's input with the filesystem path.
    fake2 = FakeExecutor(outputs={"b": "ok"})
    engine2 = Engine({"tool:bash": fake2}, store)
    await engine2.run(conduit, {"run_path": "USER_VALUE"}, resume_from=flow_id)
    assert store.read_input(flow_id)["run_path"] == "USER_VALUE"


# ----------------------------------------- nested-child resume disambiguation (Fix 2)


async def test_find_child_to_resume_disambiguates_by_invoking_task(store):
    """Two parent steps invoking the same sub-conduit must resume independently.

    The matcher takes the most-recent non-completed child of a name, so without
    tracking which step spawned each child it would, for the step whose child
    failed, see a newer same-named child (the other step's, completed) and
    wrongly conclude there's nothing to resume.

    :param store: FilesystemStore fixture.
    """
    parent = store.create_flow("parent", {})
    # step1's child failed; step2's child (sorts last → "most recent") completed.
    c1 = store.create_flow(
        "build", {}, parent_flow_id=parent, flow_id="20260101_aaaaaaaa_build"
    )
    c2 = store.create_flow(
        "build", {}, parent_flow_id=parent, flow_id="20260101_bbbbbbbb_build"
    )
    store.write_progress(c1, Progress(status=FlowStatus.failed, invoking_task="step1"))
    store.write_progress(
        c2, Progress(status=FlowStatus.completed, invoking_task="step2")
    )

    engine = Engine({}, store)
    # step1 resumes its own failed child despite the newer completed one.
    assert engine._find_child_to_resume(parent, "build", "step1") == c1
    # step2's child already completed → nothing to resume.
    assert engine._find_child_to_resume(parent, "build", "step2") is None


async def test_find_child_to_resume_takes_the_latest_round_of_a_loop(store):
    """A looping step resumes the child it started last, not the one whose id sorts last.

    The id's random part says nothing about order, so an earlier completed
    round must not hide the failed one that came after it.

    :param store: FilesystemStore fixture.
    """
    parent = store.create_flow("parent", {})
    rounds = [
        ("20260101_ffffffff_build", FlowStatus.completed),
        ("20260101_88888888_build", FlowStatus.completed),
        ("20260101_00000000_build", FlowStatus.failed),
    ]
    for minute, (fid, status) in enumerate(rounds):
        store.create_flow("build", {}, parent_flow_id=parent, flow_id=fid)
        store.write_progress(
            fid,
            Progress(
                status=status,
                invoking_task="loop",
                started_at=f"2026-01-01T09:0{minute}:00Z",
            ),
        )

    engine = Engine({}, store)
    assert engine._find_child_to_resume(parent, "build", "loop") == "20260101_00000000_build"


async def test_find_child_to_resume_skips_live_running_child(store):
    """A `running` child whose runner is still alive must not be picked up.

    Resuming it would drive the same child directory from two engines at once.
    A `running` child is resumable only when its runner is provably dead.

    :param store: FilesystemStore fixture.
    """
    import os
    import socket
    import subprocess

    parent = store.create_flow("parent", {})
    child = store.create_flow(
        "build", {}, parent_flow_id=parent, flow_id="20260101_aaaaaaaa_build"
    )
    host = socket.gethostname()

    # Live runner (this very test process): the child must be left alone.
    store.write_progress(
        child,
        Progress(
            status=FlowStatus.running,
            invoking_task="step1",
            runner_pid=os.getpid(),
            runner_host=host,
        ),
    )
    engine = Engine({}, store)
    assert engine._find_child_to_resume(parent, "build", "step1") is None

    # Dead runner (a pid that has exited): the orphaned child is resumable.
    dead = subprocess.Popen(["true"])
    dead.wait()
    store.write_progress(
        child,
        Progress(
            status=FlowStatus.running,
            invoking_task="step1",
            runner_pid=dead.pid,
            runner_host=host,
        ),
    )
    assert engine._find_child_to_resume(parent, "build", "step1") == child
