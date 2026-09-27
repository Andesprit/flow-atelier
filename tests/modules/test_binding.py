"""Unit coverage for the shared per-task agent selection helper."""
from __future__ import annotations

import pytest

from flow_atelier.modules.binding import (
    BindingError,
    bind_conduit,
    check_replaceable,
    parse_agent_bindings,
    recipe_tools,
)
from flow_atelier.schemas.conduit import Conduit
from flow_atelier.schemas.progress import Progress, TaskProgress, TaskStatus


def _conduit(**tools: str) -> Conduit:
    """Build a conduit whose tasks are the given ``name=tool`` pairs.

    :param tools: task name to tool string, in definition order.
    :returns: the parsed conduit.
    """
    return Conduit.model_validate(
        {
            "name": "recipe",
            "description": "d",
            "tasks": [
                {
                    "name": name,
                    "description": name,
                    "task": f"do {name}",
                    "tool": tool,
                    "depends_on": [],
                }
                for name, tool in tools.items()
            ],
        }
    )


# --------------------------------------------------------------------- parsing


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (["step_1=codex"], {"step_1": "harness:codex"}),
        (["step_1=harness:codex"], {"step_1": "harness:codex"}),
        (["step_1=codex:gpt-5.1"], {"step_1": "harness:codex:gpt-5.1"}),
        (["step_1=codex:gpt-5.1:high"], {"step_1": "harness:codex:gpt-5.1:high"}),
        (["step_1=claude-code:opus[1m]"], {"step_1": "harness:claude-code:opus[1m]"}),
        ([" step_1 = codex "], {"step_1": "harness:codex"}),
        (["a=alpha", "b=beta"], {"a": "harness:alpha", "b": "harness:beta"}),
        ([], {}),
        (None, {}),
    ],
)
def test_accepted_forms_normalize_to_harness_tools(raw, expected):
    """Bare, prefixed, model and effort forms all reach the same tool string."""
    assert parse_agent_bindings(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        ["step_1"],          # no separator
        ["=codex"],          # no task
        ["step_1="],         # no agent
        ["  =  "],           # neither
    ],
)
def test_malformed_values_are_refused_as_misuse(raw):
    """A value that is not TASK=HARNESS exits 2 and shows the shape."""
    with pytest.raises(BindingError) as err:
        parse_agent_bindings(raw)
    assert err.value.code == 2
    assert "expected TASK=HARNESS" in str(err.value)


def test_a_repeated_task_selector_is_refused():
    """Two agents for one task would make the result argument-order dependent."""
    with pytest.raises(BindingError) as err:
        parse_agent_bindings(["step_1=alpha", "step_1=beta"])
    assert "twice" in str(err.value)


@pytest.mark.parametrize(
    "harness",
    ["tool:bash", "tool:hitl", "tool:conduit"],
)
def test_a_builtin_tool_is_not_an_agent(harness):
    """`--agent` re-points harness tasks; a tool: task is edited in the YAML."""
    with pytest.raises(BindingError) as err:
        parse_agent_bindings([f"step_1={harness}"])
    assert "is not an agent" in str(err.value)


@pytest.mark.parametrize("harness", ["Codex", "co dex", "-codex", "codex:", "*"])
def test_a_name_outside_the_harness_grammar_is_refused(harness):
    """The same grammar YAML accepts, so a name that works there works here."""
    with pytest.raises(BindingError):
        parse_agent_bindings([f"step_1={harness}"])


# --------------------------------------------------------------------- binding


def test_binding_replaces_only_the_named_task_and_copies_the_conduit():
    """The recipe object handed in is left exactly as it was."""
    recipe = _conduit(step_1="harness:alpha", step_2="harness:beta")
    bound = bind_conduit(recipe, {"step_2": "harness:gamma"})
    assert [t.tool for t in bound.tasks] == ["harness:alpha", "harness:gamma"]
    assert [t.tool for t in recipe.tasks] == ["harness:alpha", "harness:beta"]
    assert bound is not recipe


def test_an_empty_mapping_returns_the_same_conduit():
    """No selection means nothing to copy and nothing to change."""
    recipe = _conduit(step_1="harness:alpha")
    assert bind_conduit(recipe, {}) is recipe


def test_an_unknown_task_names_the_tasks_that_do_exist():
    """A typo must say what the conduit has, and suggest the near miss."""
    recipe = _conduit(step_1="harness:alpha", step_2="harness:beta")
    with pytest.raises(BindingError) as err:
        bind_conduit(recipe, {"step_3": "harness:gamma"})
    assert err.value.code == 1
    assert "has no task 'step_3'" in str(err.value)
    assert "['step_1', 'step_2']" in str(err.value)


@pytest.mark.parametrize("tool", ["tool:bash", "tool:hitl", "tool:conduit"])
def test_a_non_agent_task_cannot_be_converted(tool):
    """Turning a bash or approval step into an agent is a recipe edit."""
    recipe = _conduit(gate=tool)
    with pytest.raises(BindingError) as err:
        bind_conduit(recipe, {"gate": "harness:alpha"})
    assert err.value.code == 1
    assert "not an agent" in str(err.value)


def test_a_wildcard_selector_is_just_an_unknown_task():
    """No wildcard grammar: `*` names no task, and is refused as one."""
    recipe = _conduit(step_1="harness:alpha")
    with pytest.raises(BindingError) as err:
        bind_conduit(recipe, {"*": "harness:beta"})
    assert "has no task '*'" in str(err.value)


def test_recipe_tools_reports_only_the_tools_that_actually_change():
    """Re-selecting the agent a task already names is not an override."""
    recipe = _conduit(step_1="harness:alpha", step_2="harness:beta")
    assert recipe_tools(recipe, {"step_1": "harness:alpha"}) == {}
    assert recipe_tools(recipe, {"step_2": "harness:gamma"}) == {
        "step_2": "harness:beta"
    }


# --------------------------------------------------------- replacement guards


def _progress(**statuses: TaskStatus) -> Progress:
    """Build a progress snapshot with the given per-task statuses.

    :param statuses: task name to status.
    :returns: the progress snapshot.
    """
    return Progress(
        tasks={name: TaskProgress(status=s) for name, s in statuses.items()}
    )


@pytest.mark.parametrize("status", [TaskStatus.pending, TaskStatus.failed])
def test_a_pending_or_failed_task_may_be_given_another_agent(status):
    """Those are the two dispositions a recovery is actually about."""
    check_replaceable(_progress(step_2=status), {"step_2": "harness:gamma"})


@pytest.mark.parametrize(
    "status",
    [
        TaskStatus.completed,
        TaskStatus.running,
        TaskStatus.skipped,
        TaskStatus.cancelled,
    ],
)
def test_a_settled_task_keeps_the_agent_it_ran_with(status):
    """Re-pointing one would rewrite history rather than recover from failure."""
    with pytest.raises(BindingError) as err:
        check_replaceable(_progress(step_2=status), {"step_2": "harness:gamma"})
    assert err.value.code == 1
    assert status.value in str(err.value)
    assert "--again" in str(err.value)


def test_a_partly_completed_loop_is_refused_even_while_failed():
    """Half its history came from the first agent; nothing can attribute it."""
    progress = _progress(step_2=TaskStatus.failed)
    progress.tasks["step_2"].iteration = 3
    progress.tasks["step_2"].of = 5
    with pytest.raises(BindingError) as err:
        check_replaceable(
            progress, {"step_2": "harness:gamma"}, partial={"step_2"}
        )
    assert "mid-loop" in str(err.value)


def test_a_task_absent_from_the_prior_run_is_left_to_the_recipe_check():
    """A task the old flow never recorded is `bind_conduit`'s business."""
    check_replaceable(_progress(step_1=TaskStatus.completed), {"step_9": "harness:x"})
