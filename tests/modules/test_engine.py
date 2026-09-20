"""Engine unit tests using fake executors (no subprocesses)."""
import asyncio
from typing import Any

import pytest
import yaml

from flow_atelier.modules.engine import (
    ConduitValidationError,
    Engine,
    validate_conduit,
)
from flow_atelier.schemas.conduit import Conduit
from flow_atelier.schemas.log import ExecutionResult
from flow_atelier.schemas.progress import FlowStatus, TaskStatus
from flow_atelier.services.executor.base import ExecutorBase
from flow_atelier.services.executor.bash import to_bash_path
from flow_atelier.services.executor.conduit import ConduitExecutor
from flow_atelier.services.store.filesystem import FilesystemStore


class FakeExecutor(ExecutorBase):
    def __init__(
        self,
        outputs: dict[str, str] | None = None,
        fail: set[str] | None = None,
        sleep: float = 0.0,
    ):
        """Initialize the fake executor.

        :param outputs: optional task-name to stdout mapping.
        :param fail: optional set of task names that should fail.
        :param sleep: optional delay (seconds) before each execution returns.
        """
        self.outputs = outputs or {}
        self.fail = fail or set()
        self.sleep = sleep
        self.calls: list[str] = []

    async def execute(self, task, resolved_command, context):
        """Record the call and return a scripted ExecutionResult.

        :param task: task definition being executed.
        :param resolved_command: command string after template resolution.
        :param context: flow context provided by the engine.
        """
        self.calls.append(task.name)
        if self.sleep:
            await asyncio.sleep(self.sleep)
        if task.name in self.fail:
            return ExecutionResult(exit_code=1, stderr="boom", output="")
        out = self.outputs.get(task.name, f"out-{task.name}")
        return ExecutionResult(exit_code=0, output=out, stdout=out)


@pytest.fixture
def store(tmp_path):
    """Provide a FilesystemStore rooted under the pytest temp path.

    :param tmp_path: pytest temp directory fixture.
    """
    return FilesystemStore(tmp_path / ".atelier")


def _conduit(tasks: list[dict[str, Any]], **kw: Any) -> Conduit:
    """Build a Conduit model from a list of task dicts plus optional fields.

    :param tasks: task dicts each containing a ``name`` and task fields.
    :param kw: extra top-level conduit fields (inputs, max_concurrency, ...).
    """
    body = {
        "name": "test",
        "description": "d",
        "tasks": [{t["name"]: {k: v for k, v in t.items() if k != "name"}} for t in tasks],
        **kw,
    }
    return Conduit.model_validate(body)


async def test_linear_happy_path(store):
    """Verify a two-task linear DAG runs both tasks to completion in order.

    :param store: FilesystemStore fixture.
    """
    conduit = _conduit(
        [
            {"name": "a", "description": "d", "task": "x", "tool": "tool:bash", "depends_on": []},
            {"name": "b", "description": "d", "task": "x", "tool": "tool:bash", "depends_on": ["a"]},
        ]
    )
    fake = FakeExecutor()
    engine = Engine({"tool:bash": fake}, store)
    flow_id = await engine.run(conduit, {})
    p = store.read_progress(flow_id)
    assert p.status == FlowStatus.completed
    assert p.tasks["a"].status == TaskStatus.completed
    assert p.tasks["b"].status == TaskStatus.completed
    assert fake.calls == ["a", "b"]


async def test_parallel_fan_out(store):
    """Verify a fan-out/fan-in DAG completes all tasks.

    :param store: FilesystemStore fixture.
    """
    conduit = _conduit(
        [
            {"name": "root", "description": "d", "task": "x", "tool": "tool:bash", "depends_on": []},
            {"name": "a", "description": "d", "task": "x", "tool": "tool:bash", "depends_on": ["root"]},
            {"name": "b", "description": "d", "task": "x", "tool": "tool:bash", "depends_on": ["root"]},
            {"name": "c", "description": "d", "task": "x", "tool": "tool:bash", "depends_on": ["a", "b"]},
        ]
    )
    fake = FakeExecutor(sleep=0.1)
    engine = Engine({"tool:bash": fake}, store)
    flow_id = await engine.run(conduit, {})
    p = store.read_progress(flow_id)
    assert p.status == FlowStatus.completed
    for name in ("root", "a", "b", "c"):
        assert p.tasks[name].status == TaskStatus.completed


async def test_conditional_branch_match_and_not_match(store):
    """Verify match/not_match conditional dependencies pick the right branch.

    :param store: FilesystemStore fixture.
    """
    conduit = _conduit(
        [
            {"name": "review", "description": "d", "task": "x", "tool": "tool:bash", "depends_on": []},
            {"name": "deploy", "description": "d", "task": "x", "tool": "tool:bash",
             "depends_on": ["review.output.match(APPROVE)"]},
            {"name": "rollback", "description": "d", "task": "x", "tool": "tool:bash",
             "depends_on": ["review.output.not_match(APPROVE)"]},
        ]
    )
    fake = FakeExecutor(outputs={"review": "VERDICT: APPROVE"})
    engine = Engine({"tool:bash": fake}, store)
    flow_id = await engine.run(conduit, {})
    p = store.read_progress(flow_id)
    assert p.tasks["deploy"].status == TaskStatus.completed
    assert p.tasks["rollback"].status == TaskStatus.skipped
    assert "condition not met" in (p.tasks["rollback"].reason or "")


async def test_repeat_runs_n_times(store):
    """Verify a task with ``repeat: N`` is invoked N times.

    :param store: FilesystemStore fixture.
    """
    conduit = _conduit(
        [
            {"name": "a", "description": "d", "task": "x", "tool": "tool:bash",
             "depends_on": [], "repeat": 3},
        ]
    )
    fake = FakeExecutor()
    engine = Engine({"tool:bash": fake}, store)
    await engine.run(conduit, {})
    assert fake.calls == ["a", "a", "a"]


async def test_repeat_fails_aborts(store):
    """Verify a failing iteration aborts the rest of a repeat loop.

    :param store: FilesystemStore fixture.
    """
    conduit = _conduit(
        [
            {"name": "a", "description": "d", "task": "x", "tool": "tool:bash",
             "depends_on": [], "repeat": 5},
        ]
    )
    # Fail on the 2nd iteration by subclassing
    class FailSecond(FakeExecutor):
        def __init__(self):
            """Initialize the counter used to fail on the 2nd call."""
            super().__init__()
            self.count = 0

        async def execute(self, task, resolved_command, context):
            """Fail on the 2nd execution; succeed otherwise.

            :param task: task definition being executed.
            :param resolved_command: command string after template resolution.
            :param context: flow context provided by the engine.
            """
            self.count += 1
            self.calls.append(task.name)
            if self.count == 2:
                return ExecutionResult(exit_code=1, stderr="boom")
            return ExecutionResult(exit_code=0, output=f"i{self.count}")

    fake = FailSecond()
    engine = Engine({"tool:bash": fake}, store)
    with pytest.raises(RuntimeError):
        await engine.run(conduit, {})
    assert fake.count == 2  # stopped mid-repeat


async def test_fail_fast_cancels_siblings(store):
    """Verify a failing task cancels still-running sibling tasks.

    :param store: FilesystemStore fixture.
    """
    conduit = _conduit(
        [
            {"name": "fail", "description": "d", "task": "x", "tool": "tool:bash", "depends_on": []},
            {"name": "slow", "description": "d", "task": "x", "tool": "tool:bash", "depends_on": []},
            {"name": "after", "description": "d", "task": "x", "tool": "tool:bash", "depends_on": ["fail"]},
        ]
    )
    class Mixed(FakeExecutor):
        async def execute(self, task, resolved_command, context):
            """Fail the ``fail`` task immediately; stall everything else.

            :param task: task definition being executed.
            :param resolved_command: command string after template resolution.
            :param context: flow context provided by the engine.
            """
            self.calls.append(task.name)
            if task.name == "fail":
                return ExecutionResult(exit_code=1, stderr="boom")
            await asyncio.sleep(5)  # long — should be cancelled
            return ExecutionResult(exit_code=0, output="late")

    fake = Mixed()
    engine = Engine({"tool:bash": fake}, store)
    with pytest.raises(RuntimeError):
        await engine.run(conduit, {})
    p = store.read_progress(store.list_flows()[0])
    assert p.status == FlowStatus.failed
    assert p.tasks["fail"].status == TaskStatus.failed
    assert p.tasks["slow"].status in (TaskStatus.cancelled, TaskStatus.failed)
    # 'after' depended on 'fail', so it was never started
    assert p.tasks["after"].status in (TaskStatus.cancelled, TaskStatus.skipped, TaskStatus.pending)


async def test_skip_propagation_via_template(store):
    """Verify skipping a task propagates skip to template-dependent successors.

    :param store: FilesystemStore fixture.
    """
    conduit = _conduit(
        [
            {"name": "a", "description": "d", "task": "x", "tool": "tool:bash", "depends_on": []},
            {"name": "b", "description": "d", "task": "x", "tool": "tool:bash",
             "depends_on": ["a.output.match(YES)"]},
            # c depends on b via plain dep -> b skipped -> c skipped
            {"name": "c", "description": "d", "task": "echo {{b.output}}", "tool": "tool:bash",
             "depends_on": ["b"]},
        ]
    )
    fake = FakeExecutor(outputs={"a": "NO"})
    engine = Engine({"tool:bash": fake}, store)
    flow_id = await engine.run(conduit, {})
    p = store.read_progress(flow_id)
    assert p.tasks["b"].status == TaskStatus.skipped
    assert p.tasks["c"].status == TaskStatus.skipped


async def test_template_inputs_resolved(store):
    """Verify ``{{inputs.x}}`` is resolved into the command before execution.

    :param store: FilesystemStore fixture.
    """
    conduit = _conduit(
        [
            {"name": "a", "description": "d", "task": "echo {{inputs.msg}}",
             "tool": "tool:bash", "depends_on": []},
        ],
        inputs={"msg": "desc"},
    )
    captured = {}

    class Capturing(FakeExecutor):
        async def execute(self, task, resolved_command, context):
            """Capture the resolved command and return success.

            :param task: task definition being executed.
            :param resolved_command: command string after template resolution.
            :param context: flow context provided by the engine.
            """
            captured["cmd"] = resolved_command
            return ExecutionResult(exit_code=0, output="ok")

    fake = Capturing()
    engine = Engine({"tool:bash": fake}, store)
    await engine.run(conduit, {"msg": "hello"})
    assert captured["cmd"] == "echo hello"


async def test_missing_input_raises(store):
    """Verify a missing required input raises ValueError before execution.

    :param store: FilesystemStore fixture.
    """
    conduit = _conduit(
        [{"name": "a", "description": "d", "task": "x", "tool": "tool:bash", "depends_on": []}],
        inputs={"x": "required"},
    )
    engine = Engine({"tool:bash": FakeExecutor()}, store)
    with pytest.raises(ValueError, match="missing required inputs"):
        await engine.run(conduit, {})


class _Capturing(FakeExecutor):
    def __init__(self):
        """Capture the resolved command of the single task it runs."""
        super().__init__()
        self.cmd: str | None = None

    async def execute(self, task, resolved_command, context):
        """Record the resolved command and report success.

        :param task: task definition being executed.
        :param resolved_command: command string after template resolution.
        :param context: flow context provided by the engine.
        """
        self.cmd = resolved_command
        return ExecutionResult(exit_code=0, output="ok")


async def test_default_input_applied_when_missing(store):
    """Verify a declared default is used when the caller omits the input.

    :param store: FilesystemStore fixture.
    """
    conduit = _conduit(
        [{"name": "a", "description": "d", "task": "echo {{inputs.x}}",
          "tool": "tool:bash", "depends_on": []}],
        inputs={"x": {"description": "d", "default": "fallback"}},
    )
    fake = _Capturing()
    engine = Engine({"tool:bash": fake}, store)
    await engine.run(conduit, {})
    assert fake.cmd == "echo fallback"


async def test_supplied_input_overrides_default(store):
    """Verify a supplied value wins over the declared default.

    :param store: FilesystemStore fixture.
    """
    conduit = _conduit(
        [{"name": "a", "description": "d", "task": "echo {{inputs.x}}",
          "tool": "tool:bash", "depends_on": []}],
        inputs={"x": {"description": "d", "default": "fallback"}},
    )
    fake = _Capturing()
    engine = Engine({"tool:bash": fake}, store)
    await engine.run(conduit, {"x": "override"})
    assert fake.cmd == "echo override"


async def test_conduit_dir_in_task_template_resolves(store):
    """Verify {{conduit_dir}} in a task resolves to the conduit's install dir.

    :param store: FilesystemStore fixture.
    """
    conduit = _conduit(
        [{"name": "a", "description": "d", "task": "echo {{conduit_dir}}/x",
          "tool": "tool:bash", "depends_on": []}],
    )
    store.write_conduit(conduit)
    fake = _Capturing()
    engine = Engine({"tool:bash": fake}, store)
    await engine.run(conduit, {})
    # conduit_dir is translated into the bash executor's namespace for
    # tool:bash tasks (no-op on POSIX, /<drive>/... under git-bash on Windows).
    assert fake.cmd == f"echo {to_bash_path(store.conduit_dir('test'))}/x"


async def test_conduit_dir_default_resolves_at_apply_time(store):
    """Verify a default of {{conduit_dir}} resolves to the install dir.

    Defaults bypass resolve(), so the engine must substitute {{conduit_dir}}
    when applying the default value (D5).

    :param store: FilesystemStore fixture.
    """
    conduit = _conduit(
        [{"name": "a", "description": "d", "task": "echo {{inputs.dir}}",
          "tool": "tool:bash", "depends_on": []}],
        inputs={"dir": {"description": "d", "default": "{{conduit_dir}}"}},
    )
    store.write_conduit(conduit)
    fake = _Capturing()
    engine = Engine({"tool:bash": fake}, store)
    await engine.run(conduit, {})
    assert fake.cmd == f"echo {store.conduit_dir('test')}"


async def test_required_input_still_raises_when_others_defaulted(store):
    """Verify defaults don't excuse a sibling input that has no default.

    :param store: FilesystemStore fixture.
    """
    conduit = _conduit(
        [{"name": "a", "description": "d", "task": "x", "tool": "tool:bash", "depends_on": []}],
        inputs={
            "opt": {"description": "d", "default": "v"},
            "req": "required",
        },
    )
    engine = Engine({"tool:bash": FakeExecutor()}, store)
    with pytest.raises(ValueError, match="missing required inputs"):
        await engine.run(conduit, {})


async def test_cycle_detection(store):
    """Verify a cycle in the DAG raises ConduitValidationError.

    :param store: FilesystemStore fixture.
    """
    conduit = _conduit(
        [
            {"name": "a", "description": "d", "task": "x", "tool": "tool:bash", "depends_on": ["b"]},
            {"name": "b", "description": "d", "task": "x", "tool": "tool:bash", "depends_on": ["a"]},
        ]
    )
    engine = Engine({"tool:bash": FakeExecutor()}, store)
    with pytest.raises(ConduitValidationError, match="circular"):
        await engine.run(conduit, {})


async def test_unknown_dep_target(store):
    """Verify a dependency on an unknown task raises ConduitValidationError.

    :param store: FilesystemStore fixture.
    """
    conduit = _conduit(
        [
            {"name": "a", "description": "d", "task": "x", "tool": "tool:bash", "depends_on": ["ghost"]},
        ]
    )
    engine = Engine({"tool:bash": FakeExecutor()}, store)
    with pytest.raises(ConduitValidationError, match="unknown"):
        await engine.run(conduit, {})


async def test_unknown_template_ref_rejected(store):
    """Verify a {{ref.output}} to an unknown task fails validation at run start.

    :param store: FilesystemStore fixture.
    """
    conduit = _conduit(
        [
            {"name": "a", "description": "d", "task": "x", "tool": "tool:bash", "depends_on": []},
            {"name": "b", "description": "d", "task": "use {{ghost.output}}",
             "tool": "tool:bash", "depends_on": ["a"]},
        ]
    )
    engine = Engine({"tool:bash": FakeExecutor()}, store)
    with pytest.raises(ConduitValidationError, match="references unknown task 'ghost'"):
        await engine.run(conduit, {})


async def test_validate_rejects_ref_not_in_deps(store):
    """A {{ref.output}} to a task outside the depends_on chain must fail
    validation: whether it resolves at runtime is a scheduling race.

    :param store: FilesystemStore fixture.
    """
    conduit = _conduit(
        [
            {"name": "a", "description": "d", "task": "x", "tool": "tool:bash", "depends_on": []},
            {"name": "b", "description": "d", "task": "use {{a.output}}",
             "tool": "tool:bash", "depends_on": []},
        ]
    )
    engine = Engine({"tool:bash": FakeExecutor()}, store)
    with pytest.raises(ConduitValidationError, match="not in its depends_on chain"):
        await engine.run(conduit, {})


async def test_validate_accepts_transitive_ref(store):
    """A ref to a transitive dependency (a <- b <- c, c uses a) is valid.

    :param store: FilesystemStore fixture.
    """
    conduit = _conduit(
        [
            {"name": "a", "description": "d", "task": "x", "tool": "tool:bash", "depends_on": []},
            {"name": "b", "description": "d", "task": "x", "tool": "tool:bash",
             "depends_on": ["a"]},
            {"name": "c", "description": "d", "task": "use {{a.output}}",
             "tool": "tool:bash", "depends_on": ["b"]},
        ]
    )
    engine = Engine({"tool:bash": FakeExecutor()}, store)
    flow_id = await engine.run(conduit, {})
    assert store.read_progress(flow_id).status == FlowStatus.completed


async def test_validate_accepts_ref_via_conditional_dep(store):
    """A ref through a conditional dependency target counts as declared.

    :param store: FilesystemStore fixture.
    """
    conduit = _conduit(
        [
            {"name": "a", "description": "d", "task": "x", "tool": "tool:bash", "depends_on": []},
            {"name": "b", "description": "d", "task": "use {{a.output}}",
             "tool": "tool:bash", "depends_on": ["a.output.match(out-a)"]},
        ]
    )
    engine = Engine({"tool:bash": FakeExecutor()}, store)
    flow_id = await engine.run(conduit, {})
    assert store.read_progress(flow_id).status == FlowStatus.completed


def test_validate_rejects_unrecognized_expression():
    """A {{...}} matching none of the grammar forms is rejected at author time."""
    conduit = _conduit(
        [
            {"name": "a", "description": "d", "task": "use {{inputs}}",
             "tool": "tool:bash", "depends_on": []},
        ]
    )
    with pytest.raises(
        ConduitValidationError,
        match=r"task 'a' has an unrecognized template expression \{\{inputs\}\}",
    ):
        validate_conduit(conduit)


def test_validate_rejects_misspelled_output():
    """A misspelled `.output` ({{x.outpt}}) is unrecognized and rejected."""
    conduit = _conduit(
        [
            {"name": "a", "description": "d", "task": "use {{job.outpt}}",
             "tool": "tool:bash", "depends_on": []},
        ]
    )
    with pytest.raises(
        ConduitValidationError, match=r"unrecognized template expression"
    ):
        validate_conduit(conduit)


def test_validate_rejects_loop_ref_in_non_looping_task():
    """{{loop.previous}}/{{loop.history}} in a repeat==1 task is rejected."""
    for expr in ("loop.previous", "loop.history"):
        conduit = _conduit(
            [
                {"name": "a", "description": "d", "task": f"use {{{{{expr}}}}}",
                 "tool": "tool:bash", "depends_on": []},
            ]
        )
        with pytest.raises(
            ConduitValidationError, match=r"does not loop \(repeat is 1\)"
        ):
            validate_conduit(conduit)


def test_validate_accepts_loop_ref_in_looping_task():
    """{{loop.*}} is valid when the task actually loops (repeat > 1)."""
    conduit = _conduit(
        [
            {"name": "a", "description": "d", "task": "use {{loop.previous}}",
             "tool": "tool:bash", "depends_on": [], "repeat": 3},
        ]
    )
    validate_conduit(conduit)  # must not raise


def test_validate_scans_conduit_input_unknown_task():
    """A tool:conduit task's inputs value referencing an unknown task fails."""
    conduit = _conduit(
        [
            {"name": "caller", "description": "d", "task": "child",
             "tool": "tool:conduit", "depends_on": [],
             "inputs": {"p": "{{ghost.output}}"}},
        ]
    )
    with pytest.raises(
        ConduitValidationError, match="references unknown task 'ghost'"
    ):
        validate_conduit(conduit)


def test_validate_scans_conduit_input_out_of_deps():
    """A tool:conduit inputs ref to a task outside its deps chain fails."""
    conduit = _conduit(
        [
            {"name": "producer", "description": "d", "task": "x",
             "tool": "tool:bash", "depends_on": []},
            {"name": "caller", "description": "d", "task": "child",
             "tool": "tool:conduit", "depends_on": [],
             "inputs": {"p": "{{producer.output}}"}},
        ]
    )
    with pytest.raises(
        ConduitValidationError, match="not in its depends_on chain"
    ):
        validate_conduit(conduit)


def test_validate_accepts_conduit_input_ref_in_deps():
    """A tool:conduit inputs ref to an in-chain dependency is valid."""
    conduit = _conduit(
        [
            {"name": "producer", "description": "d", "task": "x",
             "tool": "tool:bash", "depends_on": []},
            {"name": "caller", "description": "d", "task": "child",
             "tool": "tool:conduit", "depends_on": ["producer"],
             "inputs": {"p": "{{producer.output}}"}},
        ]
    )
    validate_conduit(conduit)  # must not raise


def test_validate_does_not_require_inputs_declared():
    """Regression: {{inputs.x}} need not be declared (supplied at run time)."""
    conduit = _conduit(
        [
            {"name": "a", "description": "d", "task": "use {{inputs.undeclared}}",
             "tool": "tool:bash", "depends_on": []},
        ]
    )
    validate_conduit(conduit)  # must not raise


async def test_mid_loop_template_error_marks_task_failed(store):
    """Verify a TemplateError on iteration >= 2 fails the task (not stuck running).

    :param store: FilesystemStore fixture.
    """
    class InputEatingExecutor(FakeExecutor):
        async def execute(self, task, resolved_command, context):
            """Remove input 'x' so the next iteration's resolve fails.

            :param task: task definition being executed.
            :param resolved_command: command string after template resolution.
            :param context: flow context provided by the engine.
            """
            context.inputs.pop("x", None)
            return await super().execute(task, resolved_command, context)

    conduit = _conduit(
        [{"name": "a", "description": "d", "task": "{{inputs.x}}",
          "tool": "tool:bash", "depends_on": [], "repeat": 2}],
        inputs={"x": "desc"},
    )
    engine = Engine({"tool:bash": InputEatingExecutor()}, store)
    captured: list[str] = []
    with pytest.raises(ValueError, match="iteration 2"):
        await engine.run(conduit, {"x": "v"}, on_flow_started=captured.append)
    p = store.read_progress(captured[0])
    assert p.status == FlowStatus.failed
    assert p.tasks["a"].status == TaskStatus.failed
    assert "iteration" in (p.tasks["a"].reason or "")


async def test_failed_task_reason_persisted(store):
    """Verify a failing executor's reason is saved onto the task progress.

    :param store: FilesystemStore fixture.
    """
    conduit = _conduit(
        [{"name": "a", "description": "d", "task": "x", "tool": "tool:bash", "depends_on": []}]
    )
    engine = Engine({"tool:bash": FakeExecutor(fail={"a"})}, store)
    with pytest.raises(RuntimeError, match="task 'a' failed"):
        await engine.run(conduit, {})
    p = store.read_progress(store.list_flows()[0])
    assert p.tasks["a"].status == TaskStatus.failed
    reason = p.tasks["a"].reason or ""
    assert "exit=1" in reason
    assert "boom" in reason


async def test_first_failure_error_preserved(store):
    """Verify the first failing task's error wins when several fail together.

    :param store: FilesystemStore fixture.
    """
    conduit = _conduit(
        [
            {"name": "a", "description": "d", "task": "x", "tool": "tool:bash", "depends_on": []},
            {"name": "b", "description": "d", "task": "x", "tool": "tool:bash", "depends_on": []},
        ]
    )
    engine = Engine({"tool:bash": FakeExecutor(fail={"a", "b"})}, store)
    with pytest.raises(RuntimeError, match="task 'a' failed"):
        await engine.run(conduit, {})


async def test_invalid_regex(store):
    """Verify an invalid regex in a conditional dep raises ConduitValidationError.

    :param store: FilesystemStore fixture.
    """
    conduit = _conduit(
        [
            {"name": "a", "description": "d", "task": "x", "tool": "tool:bash", "depends_on": []},
            {"name": "b", "description": "d", "task": "x", "tool": "tool:bash",
             "depends_on": ["a.output.match([unclosed)"]},
        ]
    )
    engine = Engine({"tool:bash": FakeExecutor()}, store)
    with pytest.raises(ConduitValidationError):
        await engine.run(conduit, {})


async def test_concurrency_cap(store):
    """Verify ``max_concurrency`` caps the number of in-flight tasks.

    :param store: FilesystemStore fixture.
    """
    conduit = _conduit(
        [
            {"name": "a", "description": "d", "task": "x", "tool": "tool:bash", "depends_on": []},
            {"name": "b", "description": "d", "task": "x", "tool": "tool:bash", "depends_on": []},
            {"name": "c", "description": "d", "task": "x", "tool": "tool:bash", "depends_on": []},
            {"name": "d", "description": "d", "task": "x", "tool": "tool:bash", "depends_on": []},
        ],
        max_concurrency=2,
    )
    max_seen = 0
    active = 0
    lock = asyncio.Lock()

    class Cap(FakeExecutor):
        async def execute(self, task, resolved_command, context):
            """Track concurrent invocations and return success.

            :param task: task definition being executed.
            :param resolved_command: command string after template resolution.
            :param context: flow context provided by the engine.
            """
            nonlocal active, max_seen
            async with lock:
                active += 1
                if active > max_seen:
                    max_seen = active
            await asyncio.sleep(0.15)
            async with lock:
                active -= 1
            return ExecutionResult(exit_code=0, output="ok")

    fake = Cap()
    engine = Engine({"tool:bash": fake}, store)
    await engine.run(conduit, {})
    assert max_seen <= 2


async def test_on_task_event_fires_for_each_completed_task(store):
    """Verify on_task_event is called once per completed task with output.

    :param store: FilesystemStore fixture.
    """
    conduit = _conduit(
        [
            {"name": "a", "description": "d", "task": "x", "tool": "tool:bash", "depends_on": []},
            {"name": "b", "description": "d", "task": "x", "tool": "tool:bash", "depends_on": ["a"]},
        ]
    )
    fake = FakeExecutor(outputs={"a": "alpha", "b": "beta"})
    engine = Engine({"tool:bash": fake}, store)
    events = []
    await engine.run(conduit, {}, on_task_event=events.append)
    assert [e.task for e in events] == ["a", "b"]
    assert events[0].output == "alpha" and events[0].success is True
    assert events[1].output == "beta" and events[1].exit_code == 0
    assert all(e.tool == "tool:bash" for e in events)


async def test_on_task_event_fires_for_failed_task(store):
    """Verify on_task_event fires with failure metadata when a task fails.

    :param store: FilesystemStore fixture.
    """
    conduit = _conduit(
        [
            {"name": "boom", "description": "d", "task": "x", "tool": "tool:bash", "depends_on": []},
        ]
    )
    fake = FakeExecutor(fail={"boom"})
    engine = Engine({"tool:bash": fake}, store)
    events = []
    with pytest.raises(RuntimeError):
        await engine.run(conduit, {}, on_task_event=events.append)
    assert len(events) == 1
    assert events[0].task == "boom"
    assert events[0].success is False
    assert events[0].exit_code == 1
    assert events[0].stderr == "boom"


async def test_on_task_event_callback_error_does_not_break_flow(store, capsys):
    """Verify an error in on_task_event is swallowed and the flow still completes.

    :param store: FilesystemStore fixture.
    :param capsys: pytest captured-output fixture.
    """
    conduit = _conduit(
        [
            {"name": "a", "description": "d", "task": "x", "tool": "tool:bash", "depends_on": []},
        ]
    )
    fake = FakeExecutor()
    engine = Engine({"tool:bash": fake}, store)

    def bad_callback(event):
        """Always raise to simulate a broken task-event consumer.

        :param event: TaskEvent dispatched by the engine.
        """
        raise RuntimeError("renderer exploded")

    flow_id = await engine.run(conduit, {}, on_task_event=bad_callback)
    p = store.read_progress(flow_id)
    assert p.status == FlowStatus.completed
    assert p.tasks["a"].status == TaskStatus.completed
    captured = capsys.readouterr()
    assert "renderer exploded" in captured.err


async def test_on_task_event_fires_for_skipped_task(store):
    """A task skipped via a conditional dependency must still emit a
    TaskEvent so renderers can show it (previously it disappeared).

    :param store: FilesystemStore fixture.
    """
    conduit = _conduit(
        [
            {"name": "review", "description": "d", "task": "x",
             "tool": "tool:bash", "depends_on": []},
            {"name": "deploy", "description": "d", "task": "x",
             "tool": "tool:bash",
             "depends_on": ["review.output.match(APPROVE)"]},
        ]
    )
    fake = FakeExecutor(outputs={"review": "REJECT"})
    engine = Engine({"tool:bash": fake}, store)
    events = []
    await engine.run(conduit, {}, on_task_event=events.append)
    by_task = {e.task: e for e in events}
    assert "deploy" in by_task
    assert by_task["deploy"].status == TaskStatus.skipped
    assert by_task["deploy"].reason  # populated with skip reason


async def test_on_task_event_fires_for_cancelled_task(store):
    """When fail-fast cancels still-pending tasks, those tasks must
    emit a TaskEvent so the user sees they were cancelled rather than
    just silently missing from the live output.

    :param store: FilesystemStore fixture.
    """
    conduit = _conduit(
        [
            {"name": "fail", "description": "d", "task": "x",
             "tool": "tool:bash", "depends_on": []},
            {"name": "after", "description": "d", "task": "x",
             "tool": "tool:bash", "depends_on": ["fail"]},
        ]
    )
    fake = FakeExecutor(fail={"fail"})
    engine = Engine({"tool:bash": fake}, store)
    events = []
    with pytest.raises(RuntimeError):
        await engine.run(conduit, {}, on_task_event=events.append)
    by_task = {e.task: e for e in events}
    assert "after" in by_task
    assert by_task["after"].status == TaskStatus.cancelled


async def test_on_task_event_fires_per_repeat_iteration(store):
    """Verify on_task_event fires once per repeat iteration with iteration/of set.

    :param store: FilesystemStore fixture.
    """
    conduit = _conduit(
        [
            {"name": "a", "description": "d", "task": "x", "tool": "tool:bash",
             "depends_on": [], "repeat": 3},
        ]
    )
    fake = FakeExecutor()
    engine = Engine({"tool:bash": fake}, store)
    events = []
    await engine.run(conduit, {}, on_task_event=events.append)
    assert len(events) == 3
    assert [e.iteration for e in events] == [1, 2, 3]
    assert all(e.of == 3 for e in events)


# ---------------------------------------------------------------- until early-exit


class ScriptedExecutor(FakeExecutor):
    """Returns outputs from a scripted list, one per call."""

    def __init__(self, outputs_per_call: list[str], fail_on: int | None = None):
        """Initialize the scripted executor.

        :param outputs_per_call: outputs returned, one per call, in order.
        :param fail_on: optional 1-indexed call number that should fail.
        """
        super().__init__()
        self._scripted = outputs_per_call
        self._fail_on = fail_on  # 1-indexed iteration to fail on
        self._n = 0

    async def execute(self, task, resolved_command, context):
        """Return the next scripted output, failing on the configured call.

        :param task: task definition being executed.
        :param resolved_command: command string after template resolution.
        :param context: flow context provided by the engine.
        """
        self._n += 1
        self.calls.append(task.name)
        if self._fail_on is not None and self._n == self._fail_on:
            return ExecutionResult(
                exit_code=1, stderr="boom",
                output=self._scripted[self._n - 1],
            )
        out = self._scripted[self._n - 1]
        return ExecutionResult(exit_code=0, output=out, stdout=out)


async def test_until_match_breaks_loop_early(store):
    """Verify ``until: output.match(...)`` breaks the repeat loop early.

    :param store: FilesystemStore fixture.
    """
    conduit = _conduit(
        [
            {"name": "poll", "description": "d", "task": "x", "tool": "tool:bash",
             "depends_on": [], "repeat": 5,
             "until": "output.match(DONE)"},
        ]
    )
    fake = ScriptedExecutor(["wait", "DONE", "x", "x", "x"])
    engine = Engine({"tool:bash": fake}, store)
    flow_id = await engine.run(conduit, {})
    assert len(fake.calls) == 2
    p = store.read_progress(flow_id)
    assert p.tasks["poll"].status == TaskStatus.completed
    assert p.tasks["poll"].iteration == 2
    assert p.tasks["poll"].of == 5


async def test_until_match_never_fires_runs_full_repeat(store):
    """Verify the full repeat runs when an until pattern never matches.

    :param store: FilesystemStore fixture.
    """
    conduit = _conduit(
        [
            {"name": "poll", "description": "d", "task": "x", "tool": "tool:bash",
             "depends_on": [], "repeat": 5,
             "until": "output.match(NEVER)"},
        ]
    )
    fake = ScriptedExecutor(["a", "b", "c", "d", "e"])
    engine = Engine({"tool:bash": fake}, store)
    flow_id = await engine.run(conduit, {})
    assert len(fake.calls) == 5
    p = store.read_progress(flow_id)
    assert p.tasks["poll"].status == TaskStatus.completed
    assert p.tasks["poll"].iteration == 5
    assert p.tasks["poll"].of == 5


async def test_until_not_match_breaks_when_pattern_absent(store):
    """Verify ``until: output.not_match(...)`` breaks when pattern is absent.

    :param store: FilesystemStore fixture.
    """
    conduit = _conduit(
        [
            {"name": "poll", "description": "d", "task": "x", "tool": "tool:bash",
             "depends_on": [], "repeat": 5,
             "until": "output.not_match(RETRY)"},
        ]
    )
    fake = ScriptedExecutor(["RETRY", "RETRY", "done", "x", "x"])
    engine = Engine({"tool:bash": fake}, store)
    flow_id = await engine.run(conduit, {})
    assert len(fake.calls) == 3
    p = store.read_progress(flow_id)
    assert p.tasks["poll"].status == TaskStatus.completed
    assert p.tasks["poll"].iteration == 3
    assert p.tasks["poll"].of == 5


async def test_until_not_evaluated_on_failed_iteration(store):
    """Fail-fast wins over until — a failed iteration never triggers early-exit.

    :param store: FilesystemStore fixture.
    """
    conduit = _conduit(
        [
            {"name": "poll", "description": "d", "task": "x", "tool": "tool:bash",
             "depends_on": [], "repeat": 5,
             "until": "output.match(DONE)"},
        ]
    )
    # Iteration 2 fails; its output contains "DONE" — but fail-fast should win.
    fake = ScriptedExecutor(["wait", "DONE", "x", "x", "x"], fail_on=2)
    engine = Engine({"tool:bash": fake}, store)
    with pytest.raises(RuntimeError):
        await engine.run(conduit, {})
    flow_id = store.list_flows()[0]
    p = store.read_progress(flow_id)
    assert p.tasks["poll"].status == TaskStatus.failed


async def test_while_match_breaks_when_pattern_absent(store):
    """`while: output.match(retry)` keeps iterating while output contains
    "retry" and breaks on the first iteration that doesn't.

    :param store: FilesystemStore fixture.
    """
    conduit = _conduit(
        [
            {"name": "poll", "description": "d", "task": "x", "tool": "tool:bash",
             "depends_on": [], "repeat": 5,
             "while": "output.match(retry)"},
        ]
    )
    fake = ScriptedExecutor(["retry", "retry", "done", "x", "x"])
    engine = Engine({"tool:bash": fake}, store)
    flow_id = await engine.run(conduit, {})
    assert len(fake.calls) == 3
    p = store.read_progress(flow_id)
    assert p.tasks["poll"].status == TaskStatus.completed
    assert p.tasks["poll"].iteration == 3
    assert p.tasks["poll"].of == 5


async def test_while_not_match_breaks_when_pattern_present(store):
    """`while: output.not_match(ready)` keeps iterating while output is
    NOT ready and breaks on the first iteration that emits "ready".

    :param store: FilesystemStore fixture.
    """
    conduit = _conduit(
        [
            {"name": "poll", "description": "d", "task": "x", "tool": "tool:bash",
             "depends_on": [], "repeat": 5,
             "while": "output.not_match(ready)"},
        ]
    )
    fake = ScriptedExecutor(["pending", "pending", "ready now", "x", "x"])
    engine = Engine({"tool:bash": fake}, store)
    flow_id = await engine.run(conduit, {})
    assert len(fake.calls) == 3
    p = store.read_progress(flow_id)
    assert p.tasks["poll"].status == TaskStatus.completed
    assert p.tasks["poll"].iteration == 3
    assert p.tasks["poll"].of == 5


async def test_while_runs_full_repeat_when_predicate_holds(store):
    """If output keeps matching `while` regex, the loop never exits early.

    :param store: FilesystemStore fixture.
    """
    conduit = _conduit(
        [
            {"name": "poll", "description": "d", "task": "x", "tool": "tool:bash",
             "depends_on": [], "repeat": 4,
             "while": "output.match(retry)"},
        ]
    )
    fake = ScriptedExecutor(["retry", "retry", "retry", "retry"])
    engine = Engine({"tool:bash": fake}, store)
    flow_id = await engine.run(conduit, {})
    assert len(fake.calls) == 4
    p = store.read_progress(flow_id)
    assert p.tasks["poll"].iteration == 4
    assert p.tasks["poll"].of == 4


async def test_while_not_evaluated_on_failed_iteration(store):
    """Fail-fast wins over while too — failure stops the loop.

    :param store: FilesystemStore fixture.
    """
    conduit = _conduit(
        [
            {"name": "poll", "description": "d", "task": "x", "tool": "tool:bash",
             "depends_on": [], "repeat": 5,
             "while": "output.match(retry)"},
        ]
    )
    fake = ScriptedExecutor(["retry", "retry", "x", "x", "x"], fail_on=2)
    engine = Engine({"tool:bash": fake}, store)
    with pytest.raises(RuntimeError):
        await engine.run(conduit, {})
    flow_id = store.list_flows()[0]
    p = store.read_progress(flow_id)
    assert p.tasks["poll"].status == TaskStatus.failed


# ---------------------------------------------------------------- conduit-scope predicate


class ScriptedConduitExecutor(FakeExecutor):
    """Fakes a tool:conduit executor with fabricated sub-task outputs.

    Lets us prove the engine evaluates loop predicates against sub-task
    outputs (not just the aggregate ``result.output``) without the
    ceremony of standing up a real nested conduit.
    """

    def __init__(
        self,
        sub_outputs_per_iteration: list[list[str]],
        aggregate_output: str = "nested conduit completed",
    ):
        """Initialize the scripted conduit-style executor.

        :param sub_outputs_per_iteration: sub-task outputs per iteration.
        :param aggregate_output: aggregate ``output`` string returned each call.
        """
        super().__init__()
        self._scripted = sub_outputs_per_iteration
        self._aggregate = aggregate_output
        self._n = 0

    async def execute(self, task, resolved_command, context):
        """Return the next iteration's aggregate output plus sub-outputs.

        :param task: task definition being executed.
        :param resolved_command: command string after template resolution.
        :param context: flow context provided by the engine.
        """
        self._n += 1
        self.calls.append(task.name)
        sub = self._scripted[self._n - 1]
        return ExecutionResult(
            exit_code=0,
            output=self._aggregate,
            stdout=self._aggregate,
            sub_outputs=list(sub),
        )


async def test_conduit_until_breaks_on_any_sub_task_match(store):
    """Until predicate over a tool:conduit task matches against any
    nested sub-task output, even when the aggregate ``output`` does not.

    Vacuousness guard (per SPEC §5): this test was confirmed to FAIL
    against the engine *without* the conduit-scope wiring — it runs all
    5 iterations because ``"PASS"`` never appears in
    ``aggregate_output``. It only passes once the engine reads
    ``result.sub_outputs`` for ``tool:conduit`` tasks.

    :param store: FilesystemStore fixture.
    """
    conduit = _conduit(
        [
            {"name": "outer", "description": "d", "task": "child",
             "tool": "tool:conduit",
             "depends_on": [], "repeat": 5,
             "until": "output.match(PASS)"},
        ]
    )
    fake = ScriptedConduitExecutor(
        sub_outputs_per_iteration=[
            ["build ok", "tests FAIL"],
            ["build ok", "tests FAIL"],
            ["build ok", "tests PASS finally"],
            ["unused"],
            ["unused"],
        ],
        aggregate_output="nested conduit completed",
    )
    engine = Engine({"tool:conduit": fake}, store)
    flow_id = await engine.run(conduit, {})
    assert len(fake.calls) == 3
    p = store.read_progress(flow_id)
    assert p.tasks["outer"].status == TaskStatus.completed
    assert p.tasks["outer"].iteration == 3
    assert p.tasks["outer"].of == 5


async def test_conduit_while_continues_until_every_sub_task_matches(store):
    """`while: output.not_match(ready)` over a tool:conduit task keeps
    iterating while at least one sub-task is not ready, and breaks the
    iteration in which every sub-task output contains "ready" (the
    plan's M2 semantics for while + negated predicate).

    :param store: FilesystemStore fixture.
    """
    conduit = _conduit(
        [
            {"name": "outer", "description": "d", "task": "child",
             "tool": "tool:conduit",
             "depends_on": [], "repeat": 5,
             "while": "output.not_match(ready)"},
        ]
    )
    fake = ScriptedConduitExecutor(
        sub_outputs_per_iteration=[
            ["build pending", "service pending"],
            ["build ready", "service pending"],
            ["build ready", "service ready"],
            ["unused"],
            ["unused"],
        ],
    )
    engine = Engine({"tool:conduit": fake}, store)
    flow_id = await engine.run(conduit, {})
    assert len(fake.calls) == 3
    p = store.read_progress(flow_id)
    assert p.tasks["outer"].status == TaskStatus.completed
    assert p.tasks["outer"].iteration == 3
    assert p.tasks["outer"].of == 5


async def test_conduit_predicate_runs_full_repeat_when_no_sub_match(store):
    """If no sub-output ever matches the until regex, the loop runs to
    completion — no vacuous early exit.

    :param store: FilesystemStore fixture.
    """
    conduit = _conduit(
        [
            {"name": "outer", "description": "d", "task": "child",
             "tool": "tool:conduit",
             "depends_on": [], "repeat": 3,
             "until": "output.match(PASS)"},
        ]
    )
    fake = ScriptedConduitExecutor(
        sub_outputs_per_iteration=[
            ["build ok", "tests fail"],
            ["build ok", "tests fail"],
            ["build ok", "tests fail"],
        ],
        aggregate_output="nested conduit completed",
    )
    engine = Engine({"tool:conduit": fake}, store)
    flow_id = await engine.run(conduit, {})
    assert len(fake.calls) == 3
    p = store.read_progress(flow_id)
    assert p.tasks["outer"].iteration == 3


async def test_until_early_exit_publishes_output_to_downstream(store):
    """Output from the early-exit iteration must reach downstream conditional deps.

    :param store: FilesystemStore fixture.
    """
    conduit = _conduit(
        [
            {"name": "up", "description": "d", "task": "x", "tool": "tool:bash",
             "depends_on": [], "repeat": 5,
             "until": "output.match(DONE)"},
            {"name": "down", "description": "d", "task": "x", "tool": "tool:bash",
             "depends_on": ["up.output.match(DONE)"]},
        ]
    )

    class ByName(FakeExecutor):
        def __init__(self):
            """Initialize per-task call counters."""
            super().__init__()
            self._up_n = 0

        async def execute(self, task, resolved_command, context):
            """Return scripted output keyed off the task name.

            :param task: task definition being executed.
            :param resolved_command: command string after template resolution.
            :param context: flow context provided by the engine.
            """
            self.calls.append(task.name)
            if task.name == "up":
                self._up_n += 1
                out = "DONE" if self._up_n == 2 else "wait"
                return ExecutionResult(exit_code=0, output=out, stdout=out)
            return ExecutionResult(exit_code=0, output="down-ran", stdout="down-ran")

    fake = ByName()
    engine = Engine({"tool:bash": fake}, store)
    flow_id = await engine.run(conduit, {})
    assert fake.calls.count("up") == 2
    assert "down" in fake.calls
    p = store.read_progress(flow_id)
    assert p.tasks["down"].status == TaskStatus.completed


# ---------------------------------------------------------------- outputs.yaml


async def test_run_writes_outputs_yaml_with_completed_tasks(store):
    """Verify outputs.yaml is written next to logs.json with a task map."""
    conduit = _conduit(
        [
            {"name": "a", "description": "d", "task": "x", "tool": "tool:bash", "depends_on": []},
            {"name": "b", "description": "d", "task": "x", "tool": "tool:bash", "depends_on": ["a"]},
        ]
    )
    fake = FakeExecutor(outputs={"a": "alpha", "b": "beta"})
    engine = Engine({"tool:bash": fake}, store)
    flow_id = await engine.run(conduit, {})
    path = store._flow_dir(flow_id) / "outputs.yaml"
    assert path.exists()
    data = yaml.safe_load(path.read_text())
    assert data == {"a": "alpha", "b": "beta"}


async def test_run_outputs_yaml_has_null_for_skipped_tasks(store):
    """Skipped tasks must appear in outputs.yaml with value None."""
    conduit = _conduit(
        [
            {"name": "review", "description": "d", "task": "x", "tool": "tool:bash", "depends_on": []},
            {"name": "deploy", "description": "d", "task": "x", "tool": "tool:bash",
             "depends_on": ["review.output.match(APPROVE)"]},
        ]
    )
    fake = FakeExecutor(outputs={"review": "VERDICT: REJECT"})
    engine = Engine({"tool:bash": fake}, store)
    flow_id = await engine.run(conduit, {})
    data = yaml.safe_load((store._flow_dir(flow_id) / "outputs.yaml").read_text())
    assert data == {"review": "VERDICT: REJECT", "deploy": None}


async def test_run_does_not_write_outputs_yaml_on_failure(store):
    """A failed flow must not write outputs.yaml; logs.json still present."""
    conduit = _conduit(
        [
            {"name": "boom", "description": "d", "task": "x", "tool": "tool:bash", "depends_on": []},
        ]
    )
    fake = FakeExecutor(fail={"boom"})
    engine = Engine({"tool:bash": fake}, store)
    with pytest.raises(RuntimeError):
        await engine.run(conduit, {})
    flow_id = store.list_flows()[0]
    flow_dir = store._flow_dir(flow_id)
    assert not (flow_dir / "outputs.yaml").exists()
    assert (flow_dir / "logs.jsonl").exists()


async def test_run_outputs_yaml_preserves_declaration_order(store):
    """outputs.yaml keys must follow the conduit's task declaration order."""
    conduit = _conduit(
        [
            {"name": "c", "description": "d", "task": "x", "tool": "tool:bash", "depends_on": []},
            {"name": "a", "description": "d", "task": "x", "tool": "tool:bash", "depends_on": []},
            {"name": "b", "description": "d", "task": "x", "tool": "tool:bash", "depends_on": []},
        ]
    )
    fake = FakeExecutor()
    engine = Engine({"tool:bash": fake}, store)
    flow_id = await engine.run(conduit, {})
    raw = (store._flow_dir(flow_id) / "outputs.yaml").read_text()
    data = yaml.safe_load(raw)
    assert list(data.keys()) == ["c", "a", "b"]


def _skipping_conduit() -> Conduit:
    """Build a two-task conduit whose second task is always skipped.

    :returns: a conduit whose final output map always carries a None value.
    """
    return _conduit(
        [
            {"name": "review", "description": "d", "task": "x", "tool": "tool:bash",
             "depends_on": []},
            {"name": "deploy", "description": "d", "task": "x", "tool": "tool:bash",
             "depends_on": ["review.output.match(APPROVE)"]},
        ]
    )


class _OutputsAtCompletion(FilesystemStore):
    """Store that reads outputs.yaml the moment `completed` is persisted.

    Stands in for a second process — `atelier wait` — that sees the completed
    status and immediately reads the results of that run.
    """

    def __init__(self, base_dir):
        """Start with nothing observed.

        :param base_dir: store root, as for FilesystemStore.
        """
        super().__init__(base_dir)
        self.observed: dict | None = None

    def write_progress(self, flow_id, progress):
        """Snapshot outputs.yaml just before a terminal `completed` lands.

        :param flow_id: flow being written.
        :param progress: the snapshot the engine is publishing.
        """
        if progress.status == FlowStatus.completed:
            self.observed = self.read_outputs(flow_id)
        super().write_progress(flow_id, progress)


class _FinalOutputWriteFails(FilesystemStore):
    """Store whose *final* outputs.yaml write fails, as a full disk would."""

    def __init__(self, base_dir):
        """Start with no recorded statuses.

        :param base_dir: store root, as for FilesystemStore.
        """
        super().__init__(base_dir)
        self.statuses: list[FlowStatus] = []

    def write_outputs(self, flow_id, outputs):
        """Fail only the full end-of-run map, letting intermediate writes pass.

        :param flow_id: flow being written.
        :param outputs: the map the engine is publishing.
        """
        # Only the last write carries every declared task, so a skipped task's
        # None value identifies it without counting calls.
        if None in outputs.values():
            raise OSError("no space left on device")
        super().write_outputs(flow_id, outputs)

    def write_progress(self, flow_id, progress):
        """Record every status the engine publishes.

        :param flow_id: flow being written.
        :param progress: the snapshot the engine is publishing.
        """
        self.statuses.append(progress.status)
        super().write_progress(flow_id, progress)


async def test_completed_status_is_published_after_the_final_outputs(tmp_path):
    """A reader that acts on `completed` must already see the whole output map."""
    store = _OutputsAtCompletion(tmp_path / ".atelier")
    fake = FakeExecutor(outputs={"review": "VERDICT: REJECT"})
    engine = Engine({"tool:bash": fake}, store)

    await engine.run(_skipping_conduit(), {})

    assert store.observed == {"review": "VERDICT: REJECT", "deploy": None}


async def test_a_failed_final_output_write_never_reports_completed(tmp_path):
    """If the results cannot be saved, the run fails instead of claiming success."""
    store = _FinalOutputWriteFails(tmp_path / ".atelier")
    fake = FakeExecutor(outputs={"review": "VERDICT: REJECT"})
    engine = Engine({"tool:bash": fake}, store)

    with pytest.raises(OSError):
        await engine.run(_skipping_conduit(), {})

    flow_id = store.list_flows()[0]
    assert FlowStatus.completed not in store.statuses
    assert store.read_progress(flow_id).status == FlowStatus.failed


# ---------------------------------------------------------------- last_turn_output


async def test_engine_uses_last_turn_output_when_executor_sets_it(store):
    """When an executor returns last_turn_output, the engine stores it in outputs[task]
    (so both {{task.output}} templating and outputs.yaml see only the last turn).
    """
    captured = {}

    class LastTurnExecutor(ExecutorBase):
        async def execute(self, task, resolved_command, context):
            if task.name == "agent":
                return ExecutionResult(
                    exit_code=0,
                    output="full transcript across turns",
                    stdout="full transcript across turns",
                    last_turn_output="just the last turn",
                )
            captured["cmd"] = resolved_command
            return ExecutionResult(exit_code=0, output="downstream-ran")

    conduit = _conduit(
        [
            {"name": "agent", "description": "d", "task": "x", "tool": "tool:bash",
             "depends_on": []},
            {"name": "downstream", "description": "d", "task": "echo {{agent.output}}",
             "tool": "tool:bash", "depends_on": ["agent"]},
        ]
    )
    engine = Engine({"tool:bash": LastTurnExecutor()}, store)
    flow_id = await engine.run(conduit, {})
    # Downstream templating saw only the last turn.
    assert captured["cmd"] == "echo just the last turn"
    # outputs.yaml also holds only the last turn.
    data = yaml.safe_load((store._flow_dir(flow_id) / "outputs.yaml").read_text())
    assert data["agent"] == "just the last turn"


async def test_engine_falls_back_to_output_when_last_turn_is_none(store):
    """Without last_turn_output set, the engine continues to use result.output."""
    conduit = _conduit(
        [
            {"name": "a", "description": "d", "task": "x", "tool": "tool:bash", "depends_on": []},
        ]
    )
    fake = FakeExecutor(outputs={"a": "alpha"})  # last_turn_output stays at default (None)
    engine = Engine({"tool:bash": fake}, store)
    flow_id = await engine.run(conduit, {})
    data = yaml.safe_load((store._flow_dir(flow_id) / "outputs.yaml").read_text())
    assert data == {"a": "alpha"}


# ---------------------------------------------------------------- loop feedback


class RecordingScriptedExecutor(ScriptedExecutor):
    """Scripted executor that also records each resolved command string."""

    def __init__(self, outputs_per_call: list[str]):
        """Initialize and prepare a command-capture list.

        :param outputs_per_call: outputs returned, one per call, in order.
        """
        super().__init__(outputs_per_call)
        self.commands: list[str] = []

    async def execute(self, task, resolved_command, context):
        """Record the resolved command, then defer to the scripted output.

        :param task: task definition being executed.
        :param resolved_command: command string after template resolution.
        :param context: flow context provided by the engine.
        """
        self.commands.append(resolved_command)
        return await super().execute(task, resolved_command, context)


async def test_loop_previous_feeds_prior_iteration_output(store):
    """Verify {{loop.previous}} injects the prior iteration's output.

    :param store: FilesystemStore fixture.
    """
    conduit = _conduit(
        [
            {"name": "refine", "description": "d", "task": "prev=[{{loop.previous}}]",
             "tool": "tool:bash", "depends_on": [], "repeat": 3},
        ]
    )
    fake = RecordingScriptedExecutor(["o1", "o2", "o3"])
    engine = Engine({"tool:bash": fake}, store)
    await engine.run(conduit, {})
    assert fake.commands == ["prev=[]", "prev=[o1]", "prev=[o2]"]


async def test_loop_history_accumulates_all_iterations(store):
    """Verify {{loop.history}} accumulates every prior iteration's output.

    :param store: FilesystemStore fixture.
    """
    conduit = _conduit(
        [
            {"name": "refine", "description": "d", "task": "{{loop.history}}",
             "tool": "tool:bash", "depends_on": [], "repeat": 3},
        ]
    )
    fake = RecordingScriptedExecutor(["o1", "o2", "o3"])
    engine = Engine({"tool:bash": fake}, store)
    await engine.run(conduit, {})
    assert fake.commands == [
        "",
        "--- iteration 1 ---\no1",
        "--- iteration 1 ---\no1\n\n--- iteration 2 ---\no2",
    ]


async def test_loop_history_respects_engine_limit(store):
    """Verify the engine's loop_history_limit caps {{loop.history}} rendering.

    :param store: FilesystemStore fixture.
    """
    conduit = _conduit(
        [
            {"name": "refine", "description": "d", "task": "{{loop.history}}",
             "tool": "tool:bash", "depends_on": [], "repeat": 3},
        ]
    )
    fake = RecordingScriptedExecutor(["o1", "o2", "o3"])
    engine = Engine({"tool:bash": fake}, store, loop_history_limit=1)
    await engine.run(conduit, {})
    assert fake.commands == [
        "",
        "--- iteration 1 ---\no1",
        "--- 1 earlier iterations omitted ---\n\n--- iteration 2 ---\no2",
    ]


# ---------------------------------------------------------------- hitl exemptions


class BlockingExecutor(ExecutorBase):
    """Executor that blocks until released, simulating a human at the keyboard."""

    def __init__(self):
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def execute(self, task, resolved_command, context):
        """Block until the test releases the executor.

        :param task: task definition being executed.
        :param resolved_command: command string after template resolution.
        :param context: flow context provided by the engine.
        """
        self.started.set()
        await self.release.wait()
        return ExecutionResult(exit_code=0, output="human-ok", stdout="human-ok")


async def test_hitl_task_does_not_occupy_concurrency_slot(store):
    """A blocked hitl task must not starve parallel-ready tasks.

    :param store: FilesystemStore fixture.
    """
    conduit = _conduit(
        [
            {"name": "ask", "description": "d", "task": "x", "tool": "tool:hitl",
             "depends_on": []},
            {"name": "work", "description": "d", "task": "x", "tool": "tool:bash",
             "depends_on": []},
        ],
        max_concurrency=1,
    )
    hitl = BlockingExecutor()
    bash = FakeExecutor()
    engine = Engine({"tool:hitl": hitl, "tool:bash": bash}, store)
    run = asyncio.create_task(engine.run(conduit, {}))
    await asyncio.wait_for(hitl.started.wait(), 5)
    # With max_concurrency=1, the bash task only runs while hitl is still
    # blocked if hitl does not hold the semaphore slot.
    for _ in range(200):
        if bash.calls:
            break
        await asyncio.sleep(0.01)
    assert bash.calls == ["work"]
    hitl.release.set()
    flow_id = await asyncio.wait_for(run, 5)
    p = store.read_progress(flow_id)
    assert p.status == FlowStatus.completed


async def test_hitl_task_survives_past_conduit_timeout(store, monkeypatch):
    """The backstop timeout must not kill a hitl task waiting on a human.

    :param store: FilesystemStore fixture.
    :param monkeypatch: pytest monkeypatch fixture.
    """
    import flow_atelier.modules.engine as engine_mod

    monkeypatch.setattr(engine_mod, "BACKSTOP_GRACE_SECONDS", 0)

    class SlowHitl(ExecutorBase):
        async def execute(self, task, resolved_command, context):
            await asyncio.sleep(1.3)
            return ExecutionResult(exit_code=0, output="ok", stdout="ok")

    conduit = _conduit(
        [
            {"name": "ask", "description": "d", "task": "x", "tool": "tool:hitl",
             "depends_on": []},
        ],
        timeout=1,
    )
    engine = Engine({"tool:hitl": SlowHitl()}, store)
    flow_id = await engine.run(conduit, {})
    p = store.read_progress(flow_id)
    assert p.status == FlowStatus.completed
    assert p.tasks["ask"].status == TaskStatus.completed


async def test_backstop_timeout_still_kills_non_hitl(store, monkeypatch):
    """Non-hitl executors that ignore ctx.timeout are killed by the backstop.

    :param store: FilesystemStore fixture.
    :param monkeypatch: pytest monkeypatch fixture.
    """
    import flow_atelier.modules.engine as engine_mod

    monkeypatch.setattr(engine_mod, "BACKSTOP_GRACE_SECONDS", 0)

    class IgnoresTimeout(ExecutorBase):
        async def execute(self, task, resolved_command, context):
            await asyncio.sleep(1.3)
            return ExecutionResult(exit_code=0, output="ok", stdout="ok")

    conduit = _conduit(
        [
            {"name": "slow", "description": "d", "task": "x", "tool": "tool:bash",
             "depends_on": []},
        ],
        timeout=1,
    )
    engine = Engine({"tool:bash": IgnoresTimeout()}, store)
    captured: dict[str, str] = {}
    with pytest.raises(RuntimeError, match="exit=124"):
        await engine.run(
            conduit, {}, on_flow_started=lambda fid: captured.update(id=fid)
        )
    p = store.read_progress(captured["id"])
    assert p.status == FlowStatus.failed
    logs = store.read_logs(captured["id"])
    assert logs[-1].exit_code == 124


async def test_per_task_timeout_override_kills_early(store, monkeypatch):
    """A per-task timeout supersedes the larger conduit ceiling for that task.

    :param store: FilesystemStore fixture.
    :param monkeypatch: pytest monkeypatch fixture.
    """
    import flow_atelier.modules.engine as engine_mod

    monkeypatch.setattr(engine_mod, "BACKSTOP_GRACE_SECONDS", 0)

    class IgnoresTimeout(ExecutorBase):
        async def execute(self, task, resolved_command, context):
            await asyncio.sleep(1.3)
            return ExecutionResult(exit_code=0, output="ok", stdout="ok")

    # Conduit ceiling is huge (30s); the task's own 1s override drives the kill.
    conduit = _conduit(
        [
            {"name": "slow", "description": "d", "task": "x", "tool": "tool:bash",
             "depends_on": [], "timeout": 1},
        ],
        timeout=30,
    )
    engine = Engine({"tool:bash": IgnoresTimeout()}, store)
    captured: dict[str, str] = {}
    with pytest.raises(RuntimeError, match="exit=124"):
        await engine.run(
            conduit, {}, on_flow_started=lambda fid: captured.update(id=fid)
        )
    logs = store.read_logs(captured["id"])
    assert logs[-1].exit_code == 124
    assert "engine timeout after 1s" in logs[-1].stderr


async def test_effective_timeout_override_vs_inheritance(store):
    """ctx.timeout reflects a task's override when set, else the conduit value.

    :param store: FilesystemStore fixture.
    """
    seen: dict[str, int] = {}

    class CaptureTimeout(ExecutorBase):
        async def execute(self, task, resolved_command, context):
            seen[task.name] = context.timeout
            return ExecutionResult(exit_code=0, output="ok", stdout="ok")

    conduit = _conduit(
        [
            {"name": "over", "description": "d", "task": "x", "tool": "tool:bash",
             "depends_on": [], "timeout": 7},
            {"name": "inherit", "description": "d", "task": "x", "tool": "tool:bash",
             "depends_on": []},
        ],
        timeout=42,
    )
    engine = Engine({"tool:bash": CaptureTimeout()}, store)
    flow_id = await engine.run(conduit, {})
    assert store.read_progress(flow_id).status == FlowStatus.completed
    assert seen["over"] == 7
    assert seen["inherit"] == 42


async def test_hitl_ignores_configured_timeout(store, monkeypatch):
    """A configured timeout on a hitl task is ignored — it stays unbounded.

    :param store: FilesystemStore fixture.
    :param monkeypatch: pytest monkeypatch fixture.
    """
    import flow_atelier.modules.engine as engine_mod

    monkeypatch.setattr(engine_mod, "BACKSTOP_GRACE_SECONDS", 0)

    class SlowHitl(ExecutorBase):
        async def execute(self, task, resolved_command, context):
            await asyncio.sleep(1.3)
            return ExecutionResult(exit_code=0, output="ok", stdout="ok")

    conduit = _conduit(
        [
            {"name": "ask", "description": "d", "task": "x", "tool": "tool:hitl",
             "depends_on": [], "timeout": 1},
        ],
        timeout=30,
    )
    engine = Engine({"tool:hitl": SlowHitl()}, store)
    flow_id = await engine.run(conduit, {})
    p = store.read_progress(flow_id)
    assert p.status == FlowStatus.completed
    assert p.tasks["ask"].status == TaskStatus.completed


# --------------------------------------------------------------- nested cycle guard


def _named_conduit(name, tasks):
    """Build a Conduit with an explicit name (the _conduit helper hardcodes one).

    :param name: conduit name.
    :param tasks: list of task dicts each containing a ``name`` and task fields.
    """
    return Conduit.model_validate(
        {
            "name": name,
            "description": "d",
            "tasks": [
                {t["name"]: {k: v for k, v in t.items() if k != "name"}}
                for t in tasks
            ],
        }
    )


def _count_progress_files(store):
    """Return how many flow ``progress.json`` files exist under the store.

    :param store: FilesystemStore fixture.
    """
    return len(list((store.base_dir / "flows").rglob("progress.json")))


async def test_nested_conduit_self_cycle_fails_fast(store):
    """A conduit whose tool:conduit task targets itself fails with a cycle
    error and creates no nested flow (only the top-level flow exists).

    :param store: FilesystemStore fixture.
    """
    a = _named_conduit(
        "selfcyc",
        [{"name": "again", "description": "d", "task": "selfcyc",
          "tool": "tool:conduit"}],
    )
    store.write_conduit(a)
    engine = Engine({"tool:conduit": ConduitExecutor()}, store)
    with pytest.raises(Exception) as ei:  # noqa: PT011
        await engine.run(a, {})
    assert "cycle" in str(ei.value).lower()
    # Only the top-level flow's progress was written; the cycle is caught
    # before any nested flow dir is created.
    assert _count_progress_files(store) == 1


async def test_nested_conduit_mutual_cycle_fails_fast(store):
    """A -> B -> A mutual recursion is caught and bounded.

    :param store: FilesystemStore fixture.
    """
    a = _named_conduit(
        "muta",
        [{"name": "callb", "description": "d", "task": "mutb",
          "tool": "tool:conduit"}],
    )
    b = _named_conduit(
        "mutb",
        [{"name": "calla", "description": "d", "task": "muta",
          "tool": "tool:conduit"}],
    )
    store.write_conduit(a)
    store.write_conduit(b)
    engine = Engine({"tool:conduit": ConduitExecutor()}, store)
    with pytest.raises(Exception) as ei:  # noqa: PT011
        await engine.run(a, {})
    assert "cycle" in str(ei.value).lower()
    # A and B each created one flow; the re-entry into A is rejected before
    # a third flow dir is created.
    assert _count_progress_files(store) == 2


async def test_nested_conduit_legitimate_chain_completes(store):
    """A legitimate A -> B (B has a non-conduit leaf task) still completes.

    :param store: FilesystemStore fixture.
    """
    a = _named_conduit(
        "parent",
        [{"name": "callchild", "description": "d", "task": "child",
          "tool": "tool:conduit"}],
    )
    b = _named_conduit(
        "child",
        [{"name": "leaf", "description": "d", "task": "echo hi",
          "tool": "tool:bash"}],
    )
    store.write_conduit(a)
    store.write_conduit(b)
    engine = Engine(
        {"tool:conduit": ConduitExecutor(), "tool:bash": FakeExecutor()}, store
    )
    flow_id = await engine.run(a, {})
    p = store.read_progress(flow_id)
    assert p.status == FlowStatus.completed


def test_validate_rejects_literal_conduit_traversal_target():
    """A literal tool:conduit target with path separators fails validation."""
    c = _conduit(
        [{"name": "n", "description": "d", "task": "../evil",
          "tool": "tool:conduit"}]
    )
    with pytest.raises(ConduitValidationError):
        validate_conduit(c)


def test_validate_allows_templated_conduit_target():
    """A templated tool:conduit target is deferred to runtime, not rejected."""
    c = _conduit(
        [
            {"name": "pick", "description": "d", "task": "echo", "tool": "tool:bash"},
            {"name": "n", "description": "d", "task": "{{pick.output}}",
             "tool": "tool:conduit", "depends_on": ["pick"]},
        ]
    )
    validate_conduit(c)  # must not raise


# ---------------------------------------------------------------- retry on failure


class FailNThenSucceed(FakeExecutor):
    """Fail the first ``fail_times`` executions, then succeed."""

    def __init__(self, fail_times: int):
        """Initialize the executor with a budget of failing calls.

        :param fail_times: number of leading executions that should fail.
        """
        super().__init__()
        self.fail_times = fail_times

    async def execute(self, task, resolved_command, context):
        """Fail until ``fail_times`` is exhausted, then succeed.

        :param task: task definition being executed.
        :param resolved_command: command string after template resolution.
        :param context: flow context provided by the engine.
        """
        self.calls.append(task.name)
        if len(self.calls) <= self.fail_times:
            return ExecutionResult(exit_code=1, stderr="boom")
        return ExecutionResult(exit_code=0, output="ok")


async def test_retry_succeeds_after_failures(store):
    """A task that fails N times then succeeds completes when retries >= N.

    :param store: FilesystemStore fixture.
    """
    conduit = _conduit(
        [
            {"name": "a", "description": "d", "task": "x", "tool": "tool:bash",
             "depends_on": [], "retries": 2},
        ]
    )
    fake = FailNThenSucceed(fail_times=2)
    engine = Engine({"tool:bash": fake}, store)
    flow_id = await engine.run(conduit, {})
    p = store.read_progress(flow_id)
    assert p.status == FlowStatus.completed
    assert p.tasks["a"].status == TaskStatus.completed
    assert fake.calls == ["a", "a", "a"]  # 2 failures + 1 success


async def test_retry_exhausted_fails(store):
    """A task that always fails trips fail-fast after 1 + retries attempts.

    :param store: FilesystemStore fixture.
    """
    conduit = _conduit(
        [
            {"name": "a", "description": "d", "task": "x", "tool": "tool:bash",
             "depends_on": [], "retries": 2},
        ]
    )
    fake = FakeExecutor(fail={"a"})
    engine = Engine({"tool:bash": fake}, store)
    with pytest.raises(RuntimeError):
        await engine.run(conduit, {})
    assert fake.calls == ["a", "a", "a"]  # exactly 3 attempts


async def test_retry_logs_each_attempt(store):
    """Every attempt is persisted as its own LogEntry with attempt metadata.

    :param store: FilesystemStore fixture.
    """
    conduit = _conduit(
        [
            {"name": "a", "description": "d", "task": "x", "tool": "tool:bash",
             "depends_on": [], "retries": 2},
        ]
    )
    fake = FailNThenSucceed(fail_times=2)
    engine = Engine({"tool:bash": fake}, store)
    flow_id = await engine.run(conduit, {})
    logs = [e for e in store.read_logs(flow_id) if e.task == "a"]
    assert [e.exit_code for e in logs] == [1, 1, 0]
    assert [e.extra["attempt"] for e in logs] == [1, 2, 3]
    assert all(e.extra["of_attempts"] == 3 for e in logs)


async def test_retries_zero_identical_to_current(store):
    """retries: 0 fails after a single attempt, unchanged from today.

    :param store: FilesystemStore fixture.
    """
    conduit = _conduit(
        [
            {"name": "a", "description": "d", "task": "x", "tool": "tool:bash",
             "depends_on": []},
        ]
    )
    fake = FakeExecutor(fail={"a"})
    engine = Engine({"tool:bash": fake}, store)
    with pytest.raises(RuntimeError):
        await engine.run(conduit, {})
    assert fake.calls == ["a"]  # exactly one call, no retry
    # No retries configured: the persisted log entry must carry no attempt
    # metadata, byte-for-byte identical to pre-retry behavior.
    flow_id = store.list_flows()[0]
    logs = [e for e in store.read_logs(flow_id) if e.task == "a"]
    assert len(logs) == 1
    assert logs[0].extra == {}


async def test_hitl_not_retried(store):
    """A tool:hitl task never auto-retries even when retries is set.

    :param store: FilesystemStore fixture.
    """
    conduit = _conduit(
        [
            {"name": "ask", "description": "d", "task": "x", "tool": "tool:hitl",
             "depends_on": [], "retries": 3},
        ]
    )
    fake = FakeExecutor(fail={"ask"})
    engine = Engine({"tool:hitl": fake}, store)
    with pytest.raises(RuntimeError):
        await engine.run(conduit, {})
    assert fake.calls == ["ask"]  # one attempt only, retries ignored


def test_negative_retries_rejected():
    """Conduit validation rejects a negative retries value."""
    with pytest.raises(Exception, match="retries"):  # noqa: PT011
        _conduit(
            [{"name": "a", "description": "d", "task": "x",
              "tool": "tool:bash", "retries": -1}]
        )


def test_negative_retry_backoff_rejected():
    """Conduit validation rejects a negative retry_backoff value."""
    with pytest.raises(Exception, match="retry_backoff"):  # noqa: PT011
        _conduit(
            [{"name": "a", "description": "d", "task": "x",
              "tool": "tool:bash", "retry_backoff": -1}]
        )
