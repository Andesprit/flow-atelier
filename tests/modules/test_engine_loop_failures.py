"""A loop with ``until`` / ``while`` counts a failed try and goes on.

The rule under test: when an iteration of a loop that has a condition fails
after its retries, it is one more try that did not meet the condition. The
next try sees why it failed through ``{{loop.previous}}``. A plain ``repeat``
still stops on the first failure, and a human's answer, a usage limit or a
body that cannot start still fails the task.
"""
from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any

import pytest

from flow_atelier.modules.engine import Engine
from flow_atelier.schemas.conduit import Conduit
from flow_atelier.schemas.log import ExecutionResult, LogEntry, UsageLimit
from flow_atelier.schemas.progress import FlowStatus, Progress, TaskStatus
from flow_atelier.services.executor.base import ExecutorBase
from flow_atelier.services.executor.conduit import ConduitExecutor
from flow_atelier.services.store.filesystem import FilesystemStore


@pytest.fixture
def store(tmp_path):
    """Provide a FilesystemStore rooted under the pytest temp path.

    :param tmp_path: pytest temp directory fixture.
    """
    return FilesystemStore(tmp_path / ".atelier")


@pytest.fixture
def no_margin(monkeypatch):
    """Retry a refused turn the moment its stated reset passes.

    :param monkeypatch: pytest monkeypatch fixture.
    """
    import flow_atelier.modules.engine as engine_module

    monkeypatch.setattr(engine_module, "USAGE_LIMIT_MARGIN_SECONDS", 0)


def _conduit(name: str, tasks: list[dict[str, Any]], **kw: Any) -> Conduit:
    """Build a Conduit from task dicts that each carry a ``name``.

    :param name: the conduit name.
    :param tasks: task dicts each containing a ``name`` and task fields.
    :param kw: extra top-level conduit fields (inputs, ...).
    """
    return Conduit.model_validate(
        {
            "name": name,
            "description": "d",
            "tasks": [
                {t["name"]: {"description": "d", **{k: v for k, v in t.items() if k != "name"}}}
                for t in tasks
            ],
            **kw,
        }
    )


def _shell(exit_code: int, stdout: str = "", stderr: str = "") -> ExecutionResult:
    """Build a result shaped like the bash executor's.

    :param exit_code: the exit code.
    :param stdout: what the command printed, also its output.
    :param stderr: what the command printed on stderr.
    """
    return ExecutionResult(
        exit_code=exit_code, stdout=stdout, stderr=stderr, output=stdout
    )


def _limited() -> ExecutionResult:
    """Build an agent turn refused for usage with no known reset."""
    return ExecutionResult(
        exit_code=1,
        stderr="RequestError: Internal error",
        usage_limit=UsageLimit(resets_at=None, detail="limit hit"),
    )


class Scripted(ExecutorBase):
    """Return scripted results per task name, recording each resolved command."""

    def __init__(self, **scripts: list[ExecutionResult]):
        """Initialize with one result list per task name.

        :param scripts: task name to the results its calls return, in order.
        """
        self.scripts = {name: list(results) for name, results in scripts.items()}
        self.commands: dict[str, list[str]] = {}

    async def execute(self, task, resolved_command, context):
        """Record the command and pop the task's next scripted result.

        :param task: task definition being executed.
        :param resolved_command: command string after template resolution.
        :param context: flow context provided by the engine.
        """
        self.commands.setdefault(task.name, []).append(resolved_command)
        return self.scripts[task.name].pop(0)


FAILED_1 = _shell(1, "test_login FAILED", "AssertionError: expected 200, got 500")
FAILED_2 = _shell(1, "test_login FAILED", "AssertionError: expected 200, got 404")
TEXT_1 = (
    "[this try failed with exit code 1]\n"
    "test_login FAILED\n"
    "AssertionError: expected 200, got 500"
)
TEXT_2 = TEXT_1.replace("500", "404")


async def test_until_counts_failed_tries_and_passes_on_try_3(store):
    """A test step that fails twice and then passes ends the loop at try 3.

    :param store: FilesystemStore fixture.
    """
    conduit = _conduit("t", [
        {"name": "test", "task": "make test && echo 'TESTS PASSED'",
         "tool": "tool:bash", "repeat": 5,
         "until": "output.match(TESTS PASSED)", "on_exhaust": "fail"},
    ])
    bash = Scripted(test=[FAILED_1, FAILED_2, _shell(0, "3 passed\nTESTS PASSED")])
    events = []
    flow_id = await Engine({"tool:bash": bash}, store).run(
        conduit, {}, on_task_event=events.append
    )

    p = store.read_progress(flow_id)
    assert p.status == FlowStatus.completed
    assert p.tasks["test"].status == TaskStatus.completed
    assert p.tasks["test"].iteration == 3
    assert p.tasks["test"].loop_outcome.met is True
    assert store.read_outputs(flow_id)["test"] == "3 passed\nTESTS PASSED"
    assert [
        (e.iteration, e.exit_code, e.extra.get("failed_try"))
        for e in store.read_logs(flow_id)
    ] == [(1, 1, True), (2, 1, True), (3, 0, None)]
    assert [(e.iteration, e.status) for e in events] == [
        (1, TaskStatus.failed), (2, TaskStatus.failed), (3, TaskStatus.completed)
    ]
    assert events[-1].loop_condition_met is True


async def test_a_failed_try_shows_its_output_and_error_to_the_next_try(store):
    """``{{loop.previous}}`` and ``{{loop.history}}`` carry the failed try.

    :param store: FilesystemStore fixture.
    """
    conduit = _conduit("t", [
        {"name": "fix", "task": "{{loop.previous}}|{{loop.history}}",
         "tool": "tool:bash", "repeat": 3, "until": "output.match(PASS)"},
    ])
    bash = Scripted(fix=[FAILED_1, _shell(0, "PASS")])
    await Engine({"tool:bash": bash}, store).run(conduit, {})

    assert bash.commands["fix"] == ["|", f"{TEXT_1}|--- iteration 1 ---\n{TEXT_1}"]


async def test_a_failed_try_never_meets_until_even_when_its_output_matches(store):
    """The condition is not checked on a failed try, whatever it printed.

    :param store: FilesystemStore fixture.
    """
    conduit = _conduit("t", [
        {"name": "poll", "task": "x", "tool": "tool:bash", "repeat": 3,
         "until": "output.match(DONE)"},
    ])
    bash = Scripted(poll=[_shell(1, "DONE", "crashed"), _shell(0, "DONE")])
    flow_id = await Engine({"tool:bash": bash}, store).run(conduit, {})

    assert len(bash.commands["poll"]) == 2
    assert store.read_progress(flow_id).tasks["poll"].iteration == 2


async def test_retries_run_before_a_failed_try_counts(store):
    """Only the last attempt of an iteration is counted as the failed try.

    :param store: FilesystemStore fixture.
    """
    conduit = _conduit("t", [
        {"name": "test", "task": "x", "tool": "tool:bash", "repeat": 3,
         "retries": 1, "until": "output.match(PASS)"},
    ])
    bash = Scripted(test=[FAILED_1, FAILED_2, _shell(0, "PASS")])
    flow_id = await Engine({"tool:bash": bash}, store).run(conduit, {})

    assert [
        (e.iteration, e.extra.get("attempt"), e.extra.get("failed_try"))
        for e in store.read_logs(flow_id)
    ] == [(1, 1, None), (1, 2, True), (2, 1, None)]
    assert store.read_progress(flow_id).tasks["test"].iteration == 2


async def test_plain_repeat_without_a_condition_still_fails_fast(store):
    """With no ``until`` or ``while``, a failure ends the loop and the run.

    :param store: FilesystemStore fixture.
    """
    conduit = _conduit("t", [
        {"name": "a", "task": "x", "tool": "tool:bash", "repeat": 5, "retries": 1},
    ])
    bash = Scripted(a=[FAILED_1, FAILED_2])
    with pytest.raises(RuntimeError, match="exit=1"):
        await Engine({"tool:bash": bash}, store).run(conduit, {})

    flow_id = store.list_flows()[0]
    assert len(bash.commands["a"]) == 2
    assert store.read_progress(flow_id).tasks["a"].status == TaskStatus.failed
    assert store.read_progress(flow_id).tasks["a"].iteration == 1
    assert not any(e.extra.get("failed_try") for e in store.read_logs(flow_id))


async def test_on_failed_try_fail_stops_the_loop_at_the_first_failed_try(store):
    """With ``on_failed_try: fail`` a try that fails after its retries ends the run.

    :param store: FilesystemStore fixture.
    """
    conduit = _conduit("t", [
        {"name": "stage", "task": "x", "tool": "tool:bash", "repeat": 5,
         "retries": 1, "until": "output.match(DONE)", "on_failed_try": "fail"},
        {"name": "after", "task": "y", "tool": "tool:bash", "depends_on": ["stage"]},
    ])
    bash = Scripted(stage=[FAILED_1, FAILED_2])
    with pytest.raises(RuntimeError, match="task 'stage' failed: exit=1"):
        await Engine({"tool:bash": bash}, store).run(conduit, {})

    flow_id = store.list_flows()[0]
    task = store.read_progress(flow_id).tasks["stage"]
    assert len(bash.commands["stage"]) == 2
    assert "after" not in bash.commands
    assert task.status == TaskStatus.failed
    assert task.iteration == 1
    assert task.reason == "exit=1 stderr=AssertionError: expected 200, got 404"
    assert not any(e.extra.get("failed_try") for e in store.read_logs(flow_id))


async def test_on_failed_try_fail_still_loops_while_tries_succeed(store):
    """The setting changes nothing for tries that exit 0 without meeting the condition.

    :param store: FilesystemStore fixture.
    """
    conduit = _conduit("t", [
        {"name": "stage", "task": "x", "tool": "tool:bash", "repeat": 5,
         "until": "output.match(DONE)", "on_failed_try": "fail"},
    ])
    bash = Scripted(stage=[_shell(0, "one"), _shell(0, "two"), _shell(0, "DONE")])
    flow_id = await Engine({"tool:bash": bash}, store).run(conduit, {})

    task = store.read_progress(flow_id).tasks["stage"]
    assert task.status == TaskStatus.completed
    assert task.iteration == 3
    assert task.loop_outcome.met is True


async def test_on_failed_try_fail_stops_a_conduit_loop_and_resume_continues_it(store):
    """A body that fails ends the loop; resume goes on in the same try's child.

    :param store: FilesystemStore fixture.
    """
    parent = _conduit("parent", [
        {"name": "loop", "task": "child", "tool": "tool:conduit", "repeat": 5,
         "until": "output.match(GOAL_DONE)", "on_exhaust": "fail",
         "on_failed_try": "fail"},
    ])
    child = _conduit("child", [
        {"name": "work", "task": "work", "tool": "harness:fake"},
        {"name": "route", "task": "x", "tool": "tool:bash", "depends_on": ["work"]},
    ])
    store.write_conduit(parent)
    store.write_conduit(child)
    bash = Scripted(route=[_shell(0, "CONTINUE")])
    agent = Scripted(work=[_shell(0, "stage one"), _shell(1, "", "transport closed")])
    engine = Engine(
        {"tool:conduit": ConduitExecutor(), "tool:bash": bash, "harness:fake": agent},
        store,
    )
    with pytest.raises(RuntimeError, match="task 'loop' failed"):
        await engine.run(parent, {})
    flow_id = store.list_flows()[0]
    first_try, second_try = store.list_child_flows(flow_id)
    task = store.read_progress(flow_id).tasks["loop"]
    assert task.status == TaskStatus.failed
    assert task.iteration == 2
    assert len(agent.commands["work"]) == 2

    bash = Scripted(route=[_shell(0, "GOAL_DONE")])
    agent = Scripted(work=[_shell(0, "stage two")])
    engine = Engine(
        {"tool:conduit": ConduitExecutor(), "tool:bash": bash, "harness:fake": agent},
        store,
    )
    await engine.run(parent, {}, resume_from=flow_id)

    task = store.read_progress(flow_id).tasks["loop"]
    assert task.status == TaskStatus.completed
    assert task.iteration == 2
    assert store.list_child_flows(flow_id) == [first_try, second_try]
    assert store.read_progress(second_try).status == FlowStatus.completed


async def test_on_exhaust_fail_names_the_failed_last_try(store):
    """When every try failed, the reason says how the last one failed.

    :param store: FilesystemStore fixture.
    """
    conduit = _conduit("t", [
        {"name": "test", "task": "x", "tool": "tool:bash", "repeat": 2,
         "until": "output.match(PASS)", "on_exhaust": "fail"},
    ])
    bash = Scripted(test=[FAILED_1, FAILED_2])
    with pytest.raises(RuntimeError, match="the last try failed: exit=1"):
        await Engine({"tool:bash": bash}, store).run(conduit, {})

    task = store.read_progress(store.list_flows()[0]).tasks["test"]
    assert task.status == TaskStatus.failed
    assert task.iteration == 2
    assert task.loop_outcome.met is False
    assert task.reason == (
        "exhausted 2 iterations without matching its loop predicate; the last "
        "try failed: exit=1 stderr=AssertionError: expected 200, got 404"
    )


async def test_a_failed_last_try_fails_the_task_whatever_on_exhaust_says(store):
    """A loop whose last try still failed fails, so its dependents never run.

    :param store: FilesystemStore fixture.
    """
    conduit = _conduit("t", [
        {"name": "test", "task": "x", "tool": "tool:bash", "repeat": 2,
         "until": "output.match(PASS)"},
        {"name": "report", "task": "got=[{{test.output}}]", "tool": "tool:bash",
         "depends_on": ["test"]},
    ])
    bash = Scripted(test=[FAILED_1, FAILED_2], report=[_shell(0, "ok")])
    with pytest.raises(RuntimeError, match="the last try failed: exit=1"):
        await Engine({"tool:bash": bash}, store).run(conduit, {})

    p = store.read_progress(store.list_flows()[0])
    assert p.status == FlowStatus.failed
    assert p.tasks["test"].status == TaskStatus.failed
    assert p.tasks["test"].loop_outcome.met is False
    assert p.tasks["test"].reason == (
        "exhausted 2 iterations without matching its loop predicate; the last "
        "try failed: exit=1 stderr=AssertionError: expected 200, got 404"
    )
    assert "report" not in bash.commands
    assert p.tasks["report"].status != TaskStatus.completed


async def test_on_exhaust_complete_hands_a_clean_last_miss_on(store):
    """A last try that exited 0 without a match completes, as before tries counted.

    :param store: FilesystemStore fixture.
    """
    conduit = _conduit("t", [
        {"name": "test", "task": "x", "tool": "tool:bash", "repeat": 2,
         "until": "output.match(PASS)"},
        {"name": "report", "task": "got=[{{test.output}}]", "tool": "tool:bash",
         "depends_on": ["test"]},
    ])
    bash = Scripted(test=[FAILED_1, _shell(0, "still red")], report=[_shell(0, "ok")])
    flow_id = await Engine({"tool:bash": bash}, store).run(conduit, {})

    p = store.read_progress(flow_id)
    assert p.status == FlowStatus.completed
    assert p.tasks["test"].status == TaskStatus.completed
    assert p.tasks["test"].loop_outcome.met is False
    assert p.tasks["test"].reason == "loop exhausted without predicate match"
    assert store.read_outputs(flow_id)["test"] == "still red"
    assert bash.commands["report"] == ["got=[still red]"]


async def test_identical_failed_tries_trip_stagnation(store):
    """Two failures that read the same are stagnation, reported once each.

    :param store: FilesystemStore fixture.
    """
    conduit = _conduit("t", [
        {"name": "test", "task": "x", "tool": "tool:bash", "repeat": 5,
         "until": "output.match(PASS)", "stagnation_limit": 2},
    ])
    bash = Scripted(test=[FAILED_1, FAILED_1, _shell(0, "PASS")])
    events = []
    with pytest.raises(RuntimeError, match="stagnated: 2 identical"):
        await Engine({"tool:bash": bash}, store).run(
            conduit, {}, on_task_event=events.append
        )

    assert len(bash.commands["test"]) == 2
    assert [(e.iteration, e.status) for e in events] == [
        (1, TaskStatus.failed), (2, TaskStatus.failed)
    ]


async def test_hitl_in_a_loop_still_fails_on_the_first_refusal(store):
    """A human who does not answer is not a try to repeat.

    :param store: FilesystemStore fixture.
    """
    conduit = _conduit("t", [
        {"name": "ask", "task": "x", "tool": "tool:hitl", "repeat": 3,
         "until": "output.match(yes)"},
    ])
    hitl = Scripted(ask=[_shell(1, "", "interactive input unavailable: EOFError")])
    with pytest.raises(RuntimeError, match="EOFError"):
        await Engine({"tool:hitl": hitl}, store).run(conduit, {})

    flow_id = store.list_flows()[0]
    assert len(hitl.commands["ask"]) == 1
    assert store.read_progress(flow_id).tasks["ask"].status == TaskStatus.failed
    assert not any(e.extra.get("failed_try") for e in store.read_logs(flow_id))


async def test_a_usage_limit_in_a_loop_still_fails_the_task(store):
    """A limit past the waiting budget fails, it is not spent as a try.

    :param store: FilesystemStore fixture.
    """
    conduit = _conduit("t", [
        {"name": "fix", "task": "x", "tool": "harness:fake", "repeat": 3,
         "until": "output.match(PASS)"},
    ])
    agent = Scripted(fix=[_limited()])
    engine = Engine({"harness:fake": agent}, store, usage_limit_max_wait=0)
    with pytest.raises(RuntimeError, match="usage limit reached"):
        await engine.run(conduit, {})

    assert len(agent.commands["fix"]) == 1
    flow_id = store.list_flows()[0]
    assert store.read_progress(flow_id).tasks["fix"].status == TaskStatus.failed


# ----------------------------------------------------------- tool:conduit body


HERO = {
    "name": "build_until_green", "tool": "tool:conduit", "repeat": 5,
    "until": "output.match(TESTS PASSED)", "on_exhaust": "fail",
    "inputs": {"goal": "{{inputs.goal}}", "feedback": "{{loop.previous}}"},
    "tasks": [
        {"build": {"description": "d", "tool": "tool:bash",
                   "task": "build {{inputs.goal}} from: {{inputs.feedback}}"}},
        {"test": {"description": "d", "tool": "tool:bash", "depends_on": ["build"],
                  "task": "make test && echo 'TESTS PASSED'"}},
    ],
}


async def test_conduit_body_whose_test_fails_twice_loops_until_green(store):
    """The README loop: each try is a fresh body that sees the last error.

    :param store: FilesystemStore fixture.
    """
    parent = _conduit("hero", [HERO], inputs={"goal": "what to build"})
    store.write_conduit(parent)
    bash = Scripted(
        build=[_shell(0, "built")] * 3,
        test=[FAILED_1, FAILED_2, _shell(0, "3 passed\nTESTS PASSED")],
    )
    engine = Engine({"tool:conduit": ConduitExecutor(), "tool:bash": bash}, store)
    flow_id = await engine.run(parent, {"goal": "login"})

    task = store.read_progress(flow_id).tasks["build_until_green"]
    assert task.status == TaskStatus.completed
    assert task.iteration == 3
    assert task.loop_outcome.met is True
    # A fresh child per try: a failed body is not resumed by the next try.
    children = store.list_child_flows(flow_id)
    assert len(children) == 3
    assert len(bash.commands["build"]) == 3
    feedback = [store.read_input(child)["feedback"] for child in children]
    assert feedback[0] == ""
    # The log and the failure panel name the loop iteration; the feedback
    # leaves it out, as the history numbers its tries.
    summary = "build_until_green -> test [tool:bash]"
    assert feedback[1] == (
        f"{TEXT_1}\n{summary}: AssertionError: expected 200, got 500"
    )
    assert "AssertionError: expected 200, got 404" in feedback[2]
    assert bash.commands["build"][1] == f"build login from: {feedback[1]}"
    logs = store.read_logs(flow_id)
    assert [(e.exit_code, e.extra.get("failed_try")) for e in logs] == [
        (1, True), (1, True), (0, None)
    ]
    assert [e.extra["child_flow_id"] for e in logs] == children
    assert logs[0].stderr.startswith(
        "build_until_green -> test [tool:bash], loop iteration 1/5: "
    )


async def test_identical_failed_conduit_bodies_trip_stagnation(store):
    """A body that fails the same way twice is stagnation, as a step's is.

    :param store: FilesystemStore fixture.
    """
    parent = _conduit("p", [
        {"name": "loop", "tool": "tool:conduit", "repeat": 4,
         "until": "output.match(TESTS PASSED)", "stagnation_limit": 2,
         "tasks": [{"test": {"description": "d", "tool": "tool:bash",
                             "task": "make test"}}]},
    ])
    store.write_conduit(parent)
    bash = Scripted(test=[FAILED_1, FAILED_1, _shell(0, "TESTS PASSED")])
    engine = Engine({"tool:conduit": ConduitExecutor(), "tool:bash": bash}, store)
    with pytest.raises(RuntimeError, match="stagnated: 2 identical"):
        await engine.run(parent, {})

    assert len(bash.commands["test"]) == 2


async def test_a_conduit_try_that_times_out_starts_a_fresh_body(store, monkeypatch):
    """The next try does not resume the timed-out child with its old inputs.

    :param store: FilesystemStore fixture.
    :param monkeypatch: pytest monkeypatch fixture.
    """
    import flow_atelier.modules.engine as engine_module

    monkeypatch.setattr(engine_module, "BACKSTOP_GRACE_SECONDS", 0)

    class Slow(Scripted):
        """Hang on the first call, so the engine's timeout stops the try."""

        async def execute(self, task, resolved_command, context):
            """Sleep past the timeout on the first call only.

            :param task: task definition being executed.
            :param resolved_command: command string after template resolution.
            :param context: flow context provided by the engine.
            """
            if not self.commands:
                self.commands[task.name] = [resolved_command]
                await asyncio.sleep(30)
            return await super().execute(task, resolved_command, context)

    parent = _conduit("p", [
        {"name": "loop", "tool": "tool:conduit", "repeat": 3, "timeout": 1,
         "until": "output.match(TESTS PASSED)",
         "inputs": {"feedback": "{{loop.previous}}"},
         "tasks": [{"work": {"description": "d", "tool": "tool:bash",
                             "task": "work from <{{inputs.feedback}}>"}}]},
    ])
    store.write_conduit(parent)
    bash = Slow(work=[_shell(0, "TESTS PASSED")])
    engine = Engine({"tool:conduit": ConduitExecutor(), "tool:bash": bash}, store)
    flow_id = await engine.run(parent, {})

    timeout = "[this try failed with exit code 124]\nengine timeout after 1s"
    assert bash.commands["work"] == ["work from <>", f"work from <{timeout}>"]
    assert len(store.list_child_flows(flow_id)) == 2
    assert store.read_progress(flow_id).tasks["loop"].iteration == 2


@pytest.mark.parametrize("deep", [False, True], ids=["in-body", "nested-in-body"])
async def test_a_hitl_step_in_a_conduit_body_still_fails_the_loop(store, deep):
    """A human who does not answer is never asked again, at any depth.

    With the default ``on_exhaust`` a counted try would let the steps after
    the loop run without the approval.

    :param store: FilesystemStore fixture.
    :param deep: put the gate in a conduit that the body calls.
    """
    approve = {"name": "approve", "task": "Approve?", "tool": "tool:hitl",
               "inputs": {"answer": "Type APPROVED"}}
    if deep:
        store.write_conduit(_conduit("approval", [approve]))
        approve = {"name": "gate", "task": "approval", "tool": "tool:conduit"}
    body = _conduit("body", [
        {"name": "change", "task": "x", "tool": "tool:bash"},
        {**approve, "depends_on": ["change"]},
    ])
    parent = _conduit("gated", [
        {"name": "review_loop", "task": "body", "tool": "tool:conduit",
         "repeat": 3, "until": "output.match(APPROVED)"},
        {"name": "deploy", "task": "deploy", "tool": "tool:bash",
         "depends_on": ["review_loop"]},
    ])
    store.write_conduit(body)
    store.write_conduit(parent)
    bash = Scripted(change=[_shell(0, "changed")] * 3, deploy=[_shell(0, "DEPLOYED")])
    hitl = Scripted(approve=[_shell(1, "", "interactive input unavailable: EOFError")] * 3)
    engine = Engine(
        {"tool:conduit": ConduitExecutor(), "tool:bash": bash, "tool:hitl": hitl},
        store,
    )
    with pytest.raises(RuntimeError, match="EOFError"):
        await engine.run(parent, {})

    flow_id = store.list_flows()[0]
    assert len(hitl.commands["approve"]) == 1
    assert "deploy" not in bash.commands
    task = store.read_progress(flow_id).tasks["review_loop"]
    assert (task.status, task.iteration) == (TaskStatus.failed, 1)
    assert not any(e.extra.get("failed_try") for e in store.read_logs(flow_id))


async def test_a_body_that_cannot_start_still_fails_the_loop(store):
    """A missing body input is a recipe error, not a try to repeat.

    :param store: FilesystemStore fixture.
    """
    parent = _conduit("parent", [
        {"name": "loop", "task": "body", "tool": "tool:conduit", "repeat": 3,
         "until": "output.match(DONE)"},
    ])
    body = _conduit("body", [
        {"name": "work", "task": "x", "tool": "tool:bash"},
    ], inputs={"feedback": "required"})
    store.write_conduit(parent)
    store.write_conduit(body)
    bash = Scripted(work=[])
    engine = Engine({"tool:conduit": ConduitExecutor(), "tool:bash": bash}, store)
    with pytest.raises(RuntimeError, match="missing required inputs"):
        await engine.run(parent, {})

    flow_id = store.list_flows()[0]
    assert "work" not in bash.commands
    assert [e.extra.get("failed_try") for e in store.read_logs(flow_id)] == [None]


async def test_a_nested_usage_limit_in_a_loop_still_resumes_the_same_child(
    store, no_margin
):
    """The top-level run waits and its retry resumes the refused child.

    :param store: FilesystemStore fixture.
    :param no_margin: fixture dropping the post-reset margin.
    """
    parent = _conduit("parent", [
        {"name": "loop", "task": "child", "tool": "tool:conduit", "repeat": 3,
         "until": "output.match(fixed)"},
    ])
    child = _conduit("child", [
        {"name": "first", "task": "x", "tool": "tool:bash"},
        {"name": "fix", "task": "x", "tool": "harness:fake", "depends_on": ["first"]},
    ])
    store.write_conduit(parent)
    store.write_conduit(child)
    now = datetime.now(UTC).isoformat()
    refused = _limited().model_copy(
        update={"usage_limit": UsageLimit(resets_at=now, detail="limit hit")}
    )
    bash = Scripted(first=[_shell(0, "ok")])
    agent = Scripted(fix=[refused, _shell(0, "fixed")])
    engine = Engine(
        {"tool:conduit": ConduitExecutor(), "tool:bash": bash, "harness:fake": agent},
        store,
    )
    flow_id = await engine.run(parent, {})

    assert store.read_progress(flow_id).status == FlowStatus.completed
    assert len(agent.commands["fix"]) == 2
    assert len(bash.commands["first"]) == 1
    assert len(store.list_child_flows(flow_id)) == 1
    assert not any(e.extra.get("failed_try") for e in store.read_logs(flow_id))


# ------------------------------------------------------------------- resume


async def test_resume_after_counted_failures_keeps_them_in_history(store):
    """A resumed loop starts after its counted tries and still sees them.

    :param store: FilesystemStore fixture.
    """
    conduit = _conduit("t", [
        {"name": "fix", "task": "{{loop.history}}", "tool": "tool:bash",
         "repeat": 4, "until": "output.match(PASS)"},
    ])
    first = Scripted(fix=[FAILED_1, FAILED_2, _limited()])
    engine = Engine({"tool:bash": first}, store, usage_limit_max_wait=0)
    with pytest.raises(RuntimeError, match="usage limit reached"):
        await engine.run(conduit, {})
    flow_id = store.list_flows()[0]
    assert store.read_progress(flow_id).tasks["fix"].iteration == 3

    resumed = Scripted(fix=[_shell(0, "PASS")])
    await Engine({"tool:bash": resumed}, store).run(
        conduit, {}, resume_from=flow_id
    )
    assert resumed.commands["fix"] == [
        f"--- iteration 1 ---\n{TEXT_1}\n\n--- iteration 2 ---\n{TEXT_2}"
    ]
    p = store.read_progress(flow_id)
    assert p.status == FlowStatus.completed
    assert p.tasks["fix"].iteration == 3
    assert [e.iteration for e in store.read_logs(flow_id)] == [1, 2, 3, 3]


async def test_resume_of_a_conduit_loop_continues_the_try_it_stopped_in(store):
    """Resume picks the interrupted try's child, not an earlier counted one.

    :param store: FilesystemStore fixture.
    """
    parent = _conduit("parent", [
        {"name": "loop", "task": "child", "tool": "tool:conduit", "repeat": 3,
         "until": "output.match(TESTS PASSED)",
         "inputs": {"feedback": "{{loop.previous}}"}},
    ])
    child = _conduit("child", [
        {"name": "fix", "task": "fix from: {{inputs.feedback}}", "tool": "harness:fake"},
        {"name": "test", "task": "x", "tool": "tool:bash", "depends_on": ["fix"]},
    ], inputs={"feedback": "last test run"})
    store.write_conduit(parent)
    store.write_conduit(child)
    bash = Scripted(test=[FAILED_1])
    agent = Scripted(fix=[_shell(0, "edited"), _limited()])
    engine = Engine(
        {"tool:conduit": ConduitExecutor(), "tool:bash": bash, "harness:fake": agent},
        store, usage_limit_max_wait=0,
    )
    with pytest.raises(RuntimeError, match="usage limit reached"):
        await engine.run(parent, {})
    flow_id = store.list_flows()[0]
    first_try, second_try = store.list_child_flows(flow_id)

    bash = Scripted(test=[_shell(0, "TESTS PASSED")])
    agent = Scripted(fix=[_shell(0, "edited again")])
    engine = Engine(
        {"tool:conduit": ConduitExecutor(), "tool:bash": bash, "harness:fake": agent},
        store,
    )
    await engine.run(parent, {}, resume_from=flow_id)

    task = store.read_progress(flow_id).tasks["loop"]
    assert task.status == TaskStatus.completed
    assert task.iteration == 2
    assert store.list_child_flows(flow_id) == [first_try, second_try]
    assert store.read_progress(second_try).status == FlowStatus.completed
    assert store.read_progress(first_try).status == FlowStatus.failed
    assert TEXT_1 in agent.commands["fix"][0]


async def test_find_child_to_resume_skips_a_child_the_loop_counted(store):
    """A failed child that ran in a try the loop counted is never resumed.

    The counted entry need not name the child: after a timeout it cannot.

    :param store: FilesystemStore fixture.
    """
    parent = store.create_flow("parent", {})
    rounds = ["20260101_aaaaaaaa_build", "20260101_bbbbbbbb_build"]
    for minute, fid in enumerate(rounds):
        store.create_flow("build", {}, parent_flow_id=parent, flow_id=fid)
        store.write_progress(
            fid,
            Progress(
                status=FlowStatus.failed,
                invoking_task="loop",
                started_at=f"2026-01-01T09:0{minute}:00Z",
            ),
        )
    engine = Engine({}, store)

    async def count(minute: int) -> None:
        await store.append_log(parent, LogEntry(
            task="loop", tool="tool:conduit", exit_code=124,
            started_at=f"2026-01-01T09:0{minute}:00Z",
            finished_at=f"2026-01-01T09:0{minute}:30Z",
            extra={"failed_try": True},
        ))

    # The newest child started after the last counted try ended, in a try not
    # counted yet (a retry or a limit): resume it.
    await count(0)
    assert engine.find_child_to_resume(parent, "build", "loop") == rounds[1]
    # Once the loop counts the try it ran in, the next try starts fresh.
    await count(1)
    assert engine.find_child_to_resume(parent, "build", "loop") is None


@pytest.mark.parametrize("body", [
    'set -u; echo checking; echo "$NOT_DEFINED_VAR"',
    "no_such_test_runner_q 2>&1; echo TESTS FAILED",
])
async def test_the_same_bash_error_twice_trips_stagnation(store, tmp_path, body):
    """Errors bash prints itself read the same on every try, failed or not.

    :param store: FilesystemStore fixture.
    :param tmp_path: pytest temp directory fixture.
    :param body: a failing try, or a passing try that prints a bash error.
    """
    from flow_atelier.services.executor.bash import BashExecutor

    conduit = _conduit("t", [
        {"name": "fix", "task": body, "tool": "tool:bash", "repeat": 5,
         "until": "output.match(PASSED)", "stagnation_limit": 2},
    ])
    with pytest.raises(RuntimeError, match="stagnated: 2 identical"):
        await Engine({"tool:bash": BashExecutor()}, store).run(
            conduit, {}, working_dir=tmp_path
        )
    logs = store.read_logs(store.list_flows()[0])
    assert [e.iteration for e in logs] == [1, 2]
