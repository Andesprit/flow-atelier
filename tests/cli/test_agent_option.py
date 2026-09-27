"""`--agent TASK=HARNESS` on `atelier run` and `atelier plan`.

Option-level behavior: what a selection means, what it refuses, what `plan`
shows, and what `status` reports. The real multi-agent runs are in
``test_agent_binding_workflow.py``.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest
from typer.testing import CliRunner

from flow_atelier.cli import app

FAKE_AGENT = Path(__file__).resolve().parents[1] / "fixtures" / "fake_acp_agent.py"

RECIPE = """\
name: chain
description: a two-agent handoff with a script in the middle
inputs:
  brief:
    description: the shared task
tasks:
  - name: step_1
    description: first agent
    task: "Draft: {{inputs.brief}}"
    tool: harness:alpha
    depends_on: []
  - name: gate
    description: a plain script
    task: echo gate
    tool: tool:bash
    depends_on: [step_1]
  - name: step_2
    description: second agent
    task: "Review ({{step_1.tool}}): {{step_1.output}}"
    tool: harness:beta
    depends_on: [gate]
"""


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    """A project with the recipe installed and three registered fake agents.

    ``ghost`` is registered but its binary does not exist, so it is the
    not-installed case; ``alpha`` and ``beta`` are the scripted ACP agent.

    :param tmp_path: pytest temp directory fixture.
    :param monkeypatch: pytest monkeypatch fixture.
    :returns: the project path.
    """
    work = tmp_path / "project"
    conduit = work / ".atelier" / "conduits" / "chain"
    conduit.mkdir(parents=True)
    (conduit / "conduit.yaml").write_text(RECIPE, encoding="utf-8")
    (tmp_path / "global" / "conduits").mkdir(parents=True)
    for key in list(os.environ):
        if key.startswith("ATELIER_"):
            monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("ATELIER_GLOBAL_ATELIER_DIR", str(tmp_path / "global"))
    monkeypatch.setenv("ATELIER_NO_UPDATE_CHECK", "1")
    monkeypatch.setenv(
        "ATELIER_HARNESSES",
        json.dumps(
            {
                name: [sys.executable, str(FAKE_AGENT), "--script", "{}"]
                for name in ("alpha", "beta")
            }
            | {"ghost": [str(tmp_path / "no-such-agent-binary")]}
        ),
    )
    monkeypatch.chdir(work)
    return work


def _recipe(work: Path) -> str:
    """Return the installed recipe text.

    :param work: the project path.
    :returns: the conduit.yaml contents.
    """
    return (work / ".atelier" / "conduits" / "chain" / "conduit.yaml").read_text(
        encoding="utf-8"
    )


def _flows(work: Path) -> list[Path]:
    """Return every flow directory the project holds.

    :param work: the project path.
    :returns: the flow dirs.
    """
    return sorted((work / ".atelier" / "flows").glob("*"))


# ------------------------------------------------------------------------ plan


def test_plan_shows_the_effective_tool_and_the_recipe_default(workspace):
    """The wave block names what would run and what the file says."""
    result = CliRunner().invoke(app, ["plan", "chain", "--agent", "step_2=alpha"])
    assert result.exit_code == 0, result.output
    assert "harness:alpha" in result.output
    assert "recipe: harness:beta" in result.output
    # Read-only: no flow, and the recipe is byte-identical.
    assert _flows(workspace) == []
    assert _recipe(workspace) == RECIPE


def test_plan_json_carries_the_effective_tool_and_the_recipe_default(workspace):
    """The machine-readable plan answers the same question."""
    result = CliRunner().invoke(
        app, ["plan", "chain", "--json", "--agent", "step_2=alpha:m1:high"]
    )
    assert result.exit_code == 0, result.output
    plan = json.loads(result.stdout)
    tasks = {t["name"]: t for wave in plan["waves"] for t in wave}
    assert tasks["step_2"]["tool"] == "harness:alpha:m1:high"
    assert tasks["step_2"]["recipe_tool"] == "harness:beta"
    # An untouched task reports no override at all.
    assert tasks["step_1"]["tool"] == "harness:alpha"
    assert tasks["step_1"]["recipe_tool"] is None


def test_plan_without_the_option_is_unchanged(workspace):
    """Existing output keeps its shape: nothing is marked as overridden."""
    result = CliRunner().invoke(app, ["plan", "chain", "--json"])
    assert result.exit_code == 0, result.output
    plan = json.loads(result.stdout)
    assert all(
        t["recipe_tool"] is None for wave in plan["waves"] for t in wave
    )


# ------------------------------------------------------------------ rejections


@pytest.mark.parametrize(
    ("value", "code", "expected"),
    [
        ("step_2", 2, "expected TASK=HARNESS"),
        ("=alpha", 2, "expected TASK=HARNESS"),
        ("step_2=", 2, "expected TASK=HARNESS"),
        ("step_2=tool:bash", 2, "is not an agent"),
        ("step_2=Alpha", 2, "is not a harness name"),
        ("step_9=alpha", 1, "has no task 'step_9'"),
        ("gate=alpha", 1, "not an agent"),
        ("step_2=nobody", 1, "unknown agent 'harness:nobody'"),
    ],
)
@pytest.mark.parametrize("command", ["run", "plan"])
def test_a_bad_selection_is_refused_by_both_commands(
    workspace, command, value, code, expected
):
    """run and plan share one parser, so they refuse the same things alike."""
    argv = [command, "chain", "--agent", value]
    if command == "run":
        argv += ["--input", "brief=B"]
    result = CliRunner().invoke(app, argv)
    assert result.exit_code == code, result.output
    assert expected in result.output
    assert _flows(workspace) == []
    assert _recipe(workspace) == RECIPE


def test_a_repeated_task_selector_is_refused(workspace):
    """Two agents for one task never silently resolve to the last one."""
    result = CliRunner().invoke(
        app,
        ["run", "chain", "--agent", "step_2=alpha", "--agent", "step_2=beta",
         "--input", "brief=B"],
    )
    assert result.exit_code == 2, result.output
    assert "twice" in result.output
    assert _flows(workspace) == []


# ------------------------------------------------------------------- readiness


def test_replacing_an_agent_you_cannot_run_lets_the_run_start(workspace):
    """The readiness gate probes the effective tools, not the recipe's."""
    runner = CliRunner()
    blocked = runner.invoke(
        app, ["run", "chain", "--agent", "step_1=ghost", "--input", "brief=B"]
    )
    assert blocked.exit_code == 1, blocked.output
    assert "cannot run:" in blocked.output and "step_1" in blocked.output
    assert _flows(workspace) == []


def test_a_recipe_whose_default_is_missing_runs_on_the_replacement(
    workspace, tmp_path, monkeypatch
):
    """A logged-out default is exactly what a replacement is for."""
    monkeypatch.setenv(
        "ATELIER_HARNESSES",
        json.dumps(
            {
                "alpha": [sys.executable, str(FAKE_AGENT), "--script", "{}"],
                "beta": [str(tmp_path / "no-such-agent-binary")],
            }
        ),
    )
    runner = CliRunner()
    blocked = runner.invoke(app, ["run", "chain", "--input", "brief=B"])
    assert blocked.exit_code == 1, blocked.output
    assert "cannot run:" in blocked.output

    ready = runner.invoke(
        app, ["plan", "chain", "--agent", "step_2=alpha"]
    )
    assert ready.exit_code == 0, ready.output
    assert "recipe: harness:beta" in ready.output
