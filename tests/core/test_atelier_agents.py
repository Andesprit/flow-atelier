"""Facade coverage for per-task agent selection: persistence, resume, re-run.

The agents are stub executors that record which tool each task was routed to,
so routing and the saved assignment record are read back rather than inferred.
The real ACP transport is exercised end-to-end in
``tests/cli/test_agent_binding_workflow.py``.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from flow_atelier.core.atelier import Atelier
from flow_atelier.core.settings import AtelierSettings
from flow_atelier.modules.binding import BindingError
from flow_atelier.schemas.conduit import Conduit
from flow_atelier.schemas.log import ExecutionResult
from flow_atelier.schemas.progress import FlowStatus
from flow_atelier.services.executor.base import ExecutorBase


class StubAgent(ExecutorBase):
    """An agent that answers with its own name and records what it was asked.

    :param name: the harness name, echoed as the task's output.
    :param seen: shared list every call appends ``(task, prompt)`` to.
    :param fail: when true, every call fails the way a logged-out agent does.
    :param ready: what :meth:`is_available` reports.
    """

    def __init__(
        self,
        name: str,
        seen: list[tuple[str, str]],
        *,
        fail: bool = False,
        ready: bool = True,
    ) -> None:
        self.name = name
        self.seen = seen
        self.fail = fail
        self.ready = ready

    async def execute(self, task, resolved_command, context) -> ExecutionResult:
        """Record the prompt and answer with this agent's name.

        :param task: the task being run.
        :param resolved_command: the resolved prompt.
        :param context: the flow context (unused).
        :returns: the execution result.
        """
        self.seen.append((task.name, resolved_command))
        if self.fail:
            return ExecutionResult(exit_code=1, stderr=f"{self.name} not logged in")
        return ExecutionResult(exit_code=0, output=f"{self.name.upper()}_SAID")

    def is_available(self) -> tuple[bool, str]:
        """Report the scripted readiness.

        :returns: ``(ready, reason)``.
        """
        return (True, "") if self.ready else (False, f"{self.name} not installed")

    def with_model(self, model: str, effort: str | None = None) -> StubAgent:
        """Return an executor pinned to ``model``, naming it in its output.

        :param model: the model id named in the tool string.
        :param effort: the effort id, when named.
        :returns: a stub whose name carries the model and effort.
        """
        suffix = f":{model}" + (f":{effort}" if effort else "")
        return StubAgent(self.name + suffix, self.seen, fail=self.fail, ready=self.ready)


CHAIN = {
    "name": "chain",
    "description": "a two-agent handoff",
    "inputs": {"brief": {"description": "the shared task"}},
    "tasks": [
        {
            "name": "step_1",
            "description": "first",
            "task": "Draft: {{inputs.brief}}",
            "tool": "harness:alpha",
            "depends_on": [],
        },
        {
            "name": "step_2",
            "description": "second",
            "task": "Review [{{step_1.tool}}]: {{step_1.output}}",
            "tool": "harness:beta",
            "depends_on": ["step_1"],
        },
    ],
}


@pytest.fixture
def workspace(tmp_path, _isolate_global_atelier_dir):
    """An Atelier whose alpha/beta/gamma agents are recording stubs.

    :param tmp_path: pytest temp directory fixture.
    :param _isolate_global_atelier_dir: isolated global atelier dir fixture.
    :returns: ``(atelier, seen, agents)``.
    """
    global_dir: Path = _isolate_global_atelier_dir
    atelier = Atelier(
        settings=AtelierSettings(
            atelier_dir=tmp_path / ".atelier", global_atelier_dir=global_dir
        )
    )
    seen: list[tuple[str, str]] = []
    agents = {name: StubAgent(name, seen) for name in ("alpha", "beta", "gamma")}
    for name, agent in agents.items():
        atelier.executors[f"harness:{name}"] = agent
    atelier.store.write_conduit(Conduit.model_validate(CHAIN))
    return atelier, seen, agents


def _recipe_bytes(atelier: Atelier) -> bytes:
    """Return the installed recipe's exact bytes.

    :param atelier: the configured Atelier.
    :returns: the conduit.yaml contents.
    """
    return (atelier.settings.atelier_dir / "conduits" / "chain" / "conduit.yaml").read_bytes()


# ------------------------------------------------------------- run and persist


async def test_a_bound_run_routes_and_records_the_effective_agents(workspace):
    """The selection routes the task and is saved before anything else."""
    atelier, seen, _ = workspace
    before = _recipe_bytes(atelier)

    flow_id = await atelier.run_conduit(
        "chain", {"brief": "B"}, agents={"step_2": "harness:gamma"}
    )

    assert [name for name, _ in seen] == ["step_1", "step_2"]
    progress = atelier.get_status(flow_id)
    assert progress.status is FlowStatus.completed
    assert progress.task_agents == {"step_2": "harness:gamma"}
    assert atelier.get_outputs(flow_id) == {
        "step_1": "ALPHA_SAID", "step_2": "GAMMA_SAID"
    }
    # The log names the tool that actually ran it.
    tools = {e.task: e.tool for e in atelier.get_flow_logs(flow_id)}
    assert tools == {"step_1": "harness:alpha", "step_2": "harness:gamma"}
    # The recipe on disk is untouched.
    assert _recipe_bytes(atelier) == before


async def test_a_model_and_effort_selection_is_recorded_as_chosen(workspace):
    """The saved record identifies the harness, model and effort actually used."""
    atelier, seen, _ = workspace
    flow_id = await atelier.run_conduit(
        "chain", {"brief": "B"}, agents={"step_1": "harness:gamma:pro-2:high"}
    )
    assert atelier.get_status(flow_id).task_agents == {
        "step_1": "harness:gamma:pro-2:high"
    }
    assert atelier.get_outputs(flow_id)["step_1"] == "GAMMA:PRO-2:HIGH_SAID"


async def test_the_downstream_prompt_attributes_the_agent_that_ran(workspace):
    """`{{step_1.tool}}` follows the selection, not the recipe's default."""
    atelier, seen, _ = workspace
    await atelier.run_conduit(
        "chain", {"brief": "B"}, agents={"step_1": "harness:gamma"}
    )
    second = next(prompt for task, prompt in seen if task == "step_2")
    assert "[harness:gamma]" in second
    assert "harness:alpha" not in second


async def test_two_runs_with_different_selections_do_not_leak(workspace):
    """Each flow carries its own assignments; neither sees the other's."""
    atelier, _, _ = workspace
    first = await atelier.run_conduit(
        "chain", {"brief": "B"}, agents={"step_2": "harness:gamma"}
    )
    second = await atelier.run_conduit(
        "chain", {"brief": "B"}, agents={"step_1": "harness:gamma"}
    )
    assert atelier.get_status(first).task_agents == {"step_2": "harness:gamma"}
    assert atelier.get_status(second).task_agents == {"step_1": "harness:gamma"}


async def test_an_unbound_run_records_nothing_and_behaves_as_before(workspace):
    """Existing callers keep their behavior and an empty assignment record."""
    atelier, _, _ = workspace
    flow_id = await atelier.run_conduit("chain", {"brief": "B"})
    assert atelier.get_status(flow_id).task_agents == {}
    assert atelier.get_outputs(flow_id)["step_2"] == "BETA_SAID"


# ------------------------------------------------------------------ rejections


async def test_an_invalid_selection_creates_no_flow(workspace):
    """Validation happens before the flow directory exists."""
    atelier, seen, _ = workspace
    with pytest.raises(BindingError, match="has no task 'step_9'"):
        await atelier.run_conduit(
            "chain", {"brief": "B"}, agents={"step_9": "harness:gamma"}
        )
    assert atelier.list_flows() == [] and seen == []


async def test_an_unregistered_agent_is_refused_before_the_run(workspace):
    """A name no executor is registered for cannot start a flow."""
    atelier, seen, _ = workspace
    with pytest.raises(BindingError, match="unknown agent 'harness:nope'"):
        await atelier.run_conduit(
            "chain", {"brief": "B"}, agents={"step_1": "harness:nope"}
        )
    assert atelier.list_flows() == [] and seen == []


async def test_readiness_follows_the_selection_in_both_directions(workspace):
    """A replaced unavailable default passes; an unavailable replacement fails."""
    atelier, _, agents = workspace
    agents["beta"].ready = False
    recipe = atelier.store.read_conduit("chain")

    assert atelier.tool_readiness(recipe) == [
        "task 'step_2' [harness:beta]: beta not installed"
    ]
    replaced = atelier.bind_agents(recipe, {"step_2": "harness:gamma"})
    assert atelier.tool_readiness(replaced) == []

    agents["gamma"].ready = False
    bad = atelier.bind_agents(recipe, {"step_1": "harness:gamma"})
    assert atelier.tool_readiness(bad) == [
        "task 'step_1' [harness:gamma]: gamma not installed",
        "task 'step_2' [harness:beta]: beta not installed",
    ]


# --------------------------------------------------------------------- resume


async def _failed_run(atelier, agents, **kwargs) -> str:
    """Run the chain with beta logged out and return the failed flow id.

    :param atelier: the configured Atelier.
    :param agents: the stub agent map.
    :param kwargs: forwarded to ``run_conduit``.
    :returns: the failed flow's id.
    """
    agents["beta"].fail = True
    with pytest.raises(RuntimeError):
        await atelier.run_conduit("chain", {"brief": "B"}, **kwargs)
    return atelier.list_flows()[-1]


async def test_resume_reuses_the_saved_assignments_without_repeating_work(workspace):
    """A resume with no --agent inherits the choices and keeps completed work."""
    atelier, seen, agents = workspace
    agents["gamma"].fail = True
    with pytest.raises(RuntimeError):
        await atelier.run_conduit(
            "chain", {"brief": "B"}, agents={"step_2": "harness:gamma"}
        )
    flow_id = atelier.list_flows()[-1]
    assert atelier.get_status(flow_id).task_agents == {"step_2": "harness:gamma"}

    agents["gamma"].fail = False
    seen.clear()
    assert await atelier.resume_flow(flow_id) == flow_id

    # Only the failed step ran again, and it ran on the chosen agent.
    assert [name for name, _ in seen] == ["step_2"]
    progress = atelier.get_status(flow_id)
    assert progress.status is FlowStatus.completed
    assert progress.task_agents == {"step_2": "harness:gamma"}
    assert atelier.get_outputs(flow_id) == {
        "step_1": "ALPHA_SAID", "step_2": "GAMMA_SAID"
    }


async def test_resume_may_replace_the_agent_of_the_failed_task(workspace):
    """The explicit recovery: hand the unfinished step to another agent."""
    atelier, seen, agents = workspace
    flow_id = await _failed_run(atelier, agents)
    seen.clear()

    await atelier.resume_flow(flow_id, agents={"step_2": "harness:gamma"})

    assert [name for name, _ in seen] == ["step_2"]
    replacement = seen[0][1]
    # The replacement receives the brief and the result step_1 already produced.
    assert "ALPHA_SAID" in replacement
    assert atelier.get_status(flow_id).task_agents == {"step_2": "harness:gamma"}
    assert atelier.get_outputs(flow_id)["step_2"] == "GAMMA_SAID"


async def test_resume_refuses_to_re_point_a_completed_task(workspace):
    """A completed task's result is already downstream; it keeps its agent."""
    atelier, seen, agents = workspace
    flow_id = await _failed_run(atelier, agents)
    before_progress = (
        atelier.store._flow_dir(flow_id) / "progress.json"
    ).read_bytes()
    before_outputs = (atelier.store._flow_dir(flow_id) / "outputs.yaml").read_bytes()
    seen.clear()

    with pytest.raises(BindingError, match="is completed in this flow"):
        await atelier.resume_flow(flow_id, agents={"step_1": "harness:gamma"})

    # Nothing ran and nothing on disk moved.
    assert seen == []
    assert (atelier.store._flow_dir(flow_id) / "progress.json").read_bytes() == (
        before_progress
    )
    assert (atelier.store._flow_dir(flow_id) / "outputs.yaml").read_bytes() == (
        before_outputs
    )


async def test_resume_fails_clearly_when_the_recipe_lost_a_bound_task(workspace):
    """A saved selector the recipe no longer has is never silently dropped."""
    atelier, seen, agents = workspace
    agents["gamma"].fail = True
    with pytest.raises(RuntimeError):
        await atelier.run_conduit(
            "chain", {"brief": "B"}, agents={"step_2": "harness:gamma"}
        )
    flow_id = atelier.list_flows()[-1]

    # The recipe is edited: step_2 is renamed, so the saved choice is stale.
    edited = dict(CHAIN)
    edited["tasks"] = [
        CHAIN["tasks"][0],
        {**CHAIN["tasks"][1], "name": "review"},
    ]
    atelier.store.write_conduit(Conduit.model_validate(edited))
    seen.clear()

    with pytest.raises(BindingError) as err:
        await atelier.resume_flow(flow_id)
    assert "saved agent choices" in str(err.value)
    assert "start a fresh run" in str(err.value)
    assert seen == []


async def test_a_flow_without_the_metadata_resumes_as_a_legacy_run(workspace):
    """Progress written before selection existed means "the recipe decides"."""
    atelier, seen, agents = workspace
    flow_id = await _failed_run(atelier, agents)
    path = atelier.store._flow_dir(flow_id) / "progress.json"
    legacy = json.loads(path.read_text())
    legacy.pop("task_agents")
    path.write_text(json.dumps(legacy))

    agents["beta"].fail = False
    seen.clear()
    await atelier.resume_flow(flow_id)

    assert [name for name, _ in seen] == ["step_2"]
    assert atelier.get_status(flow_id).task_agents == {}
    assert atelier.get_outputs(flow_id)["step_2"] == "BETA_SAID"


async def test_corrupt_assignment_metadata_refuses_to_run_the_defaults(workspace):
    """An unreadable record must not quietly launch the recipe's agents."""
    atelier, seen, agents = workspace
    flow_id = await _failed_run(atelier, agents)
    path = atelier.store._flow_dir(flow_id) / "progress.json"
    broken = json.loads(path.read_text())
    broken["task_agents"] = ["step_2", "harness:gamma"]
    path.write_text(json.dumps(broken))

    agents["beta"].fail = False
    seen.clear()
    with pytest.raises(ValueError):
        await atelier.resume_flow(flow_id)
    assert seen == []


# ---------------------------------------------------------------------- again


async def test_again_inherits_the_assignments_into_its_own_flow(workspace):
    """A fresh run of a bound flow keeps the choices and records them itself."""
    atelier, seen, _ = workspace
    source = await atelier.run_conduit(
        "chain", {"brief": "B"}, agents={"step_2": "harness:gamma"}
    )
    seen.clear()

    again = await atelier.rerun_flow(source)

    assert again != source
    assert atelier.get_status(again).task_agents == {"step_2": "harness:gamma"}
    assert [name for name, _ in seen] == ["step_1", "step_2"]
    assert atelier.get_outputs(again)["step_2"] == "GAMMA_SAID"
    # The source run is untouched.
    assert atelier.get_status(source).task_agents == {"step_2": "harness:gamma"}
    assert atelier.get_outputs(source)["step_2"] == "GAMMA_SAID"


async def test_again_accepts_an_override_on_top_of_what_was_saved(workspace):
    """Both the inherited and the new choice end up on the new flow's record."""
    atelier, _, _ = workspace
    source = await atelier.run_conduit(
        "chain", {"brief": "B"}, agents={"step_2": "harness:gamma"}
    )
    again = await atelier.rerun_flow(source, agents={"step_1": "harness:beta"})
    assert atelier.get_status(again).task_agents == {
        "step_1": "harness:beta", "step_2": "harness:gamma"
    }
    assert atelier.get_outputs(again) == {
        "step_1": "BETA_SAID", "step_2": "GAMMA_SAID"
    }


async def test_again_fails_when_a_saved_selector_now_names_a_bash_task(workspace):
    """A recipe that turned the step into a script cannot replay the choice."""
    atelier, seen, _ = workspace
    source = await atelier.run_conduit(
        "chain", {"brief": "B"}, agents={"step_2": "harness:gamma"}
    )
    edited = dict(CHAIN)
    edited["tasks"] = [
        CHAIN["tasks"][0],
        {**CHAIN["tasks"][1], "task": "echo done", "tool": "tool:bash"},
    ]
    atelier.store.write_conduit(Conduit.model_validate(edited))
    seen.clear()

    with pytest.raises(BindingError) as err:
        await atelier.rerun_flow(source)
    assert "not an agent" in str(err.value)
    assert seen == []


# --------------------------------------------------------------------- nesting


async def test_a_selection_does_not_reach_a_same_named_nested_task(workspace):
    """Selection is top-level: the child conduit is read from the store."""
    atelier, seen, _ = workspace
    atelier.store.write_conduit(
        Conduit.model_validate(
            {
                "name": "outer",
                "description": "runs the chain as a child",
                "tasks": [
                    {
                        "name": "step_1",
                        "description": "the parent's own agent step",
                        "task": "Parent work",
                        "tool": "harness:alpha",
                        "depends_on": [],
                    },
                    {
                        "name": "child",
                        "description": "the nested chain",
                        "task": "chain",
                        "tool": "tool:conduit",
                        "inputs": {"brief": "nested"},
                        "depends_on": ["step_1"],
                    },
                ],
            }
        )
    )

    flow_id = await atelier.run_conduit(
        "outer", {}, agents={"step_1": "harness:gamma"}
    )

    # The parent's step_1 was re-pointed; the child's step_1 was not.
    assert atelier.get_status(flow_id).task_agents == {"step_1": "harness:gamma"}
    child = atelier.store.list_child_flows(flow_id)[0]
    child_progress = atelier.get_status(child)
    assert child_progress.task_agents == {}
    assert atelier.get_outputs(child)["step_1"] == "ALPHA_SAID"
    assert atelier.get_outputs(flow_id)["step_1"] == "GAMMA_SAID"
