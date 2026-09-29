"""Dotted agent choices through real CLI processes and nested loop runs."""
from __future__ import annotations

import json
import sys
from pathlib import Path

from flow_atelier.modules.binding import valid_agent_selectors
from flow_atelier.schemas.conduit import MAX_NESTED_CONDUIT_DEPTH, Conduit
from tests.cli.test_agent_binding_workflow import FAKE_AGENT, Project

PARENT = """\
name: parent
description: loop then review
tasks:
  - name: loop
    description: repeat the child until its test passes
    task: child
    tool: tool:conduit
    depends_on: []
    repeat: 3
    until: output.match(TESTS PASSED)
  - name: review
    description: review the result
    task: 'Review: {{loop.output}}'
    tool: harness:housebot
    depends_on: [loop]
"""

CHILD = """\
name: child
description: one fix and test attempt
tasks:
  - name: fix
    description: fix the code
    task: 'Fix from feedback'
    tool: harness:codex
    depends_on: []
  - name: test
    description: run tests
    task: |
      n=$(cat .attempts 2>/dev/null || echo 0); n=$((n+1)); echo $n > .attempts
      if [ $n -ge 2 ]; then echo TESTS PASSED; else echo TESTS FAILED; fi
    tool: tool:bash
    depends_on: [fix]
"""


def _project(tmp_path):
    project = Project(tmp_path)
    project.install("parent", PARENT)
    project.install("child", CHILD)
    return project


def test_plan_run_and_resume_nested_loop_choice(tmp_path):
    project = _project(tmp_path)
    before = {name: project.recipe(name) for name in ("parent", "child")}

    plain = project.cli("plan", "parent", "--json")
    assert plain.returncode == 0, plain.stdout + plain.stderr
    loop = json.loads(plain.stdout)["waves"][0][0]
    assert [t["tool"] for wave in loop["child"]["waves"] for t in wave] == [
        "harness:codex", "tool:bash"
    ]
    preview = project.cli("plan", "parent", "--json", "--agent", "loop.fix=gemini")
    assert preview.returncode == 0, preview.stdout + preview.stderr
    changed = json.loads(preview.stdout)["waves"][0][0]["child"]["waves"][0][0]
    assert changed["tool"] == "harness:gemini"
    assert changed["recipe_tool"] == "harness:codex"

    project.break_agent("gemini")
    failed = project.cli("run", "parent", "--agent", "loop.fix=gemini")
    assert failed.returncode != 0
    flow_id = project.flow_id(failed)
    assert project.status(flow_id)["task_agents"] == {"loop.fix": "harness:gemini"}
    human_status = project.cli("status", flow_id)
    assert human_status.returncode == 0
    assert "loop.fix: harness:gemini" in human_status.stdout
    project.fix_agent("gemini")
    resumed = project.cli("run", "--resume", flow_id)
    assert resumed.returncode == 0, resumed.stdout + resumed.stderr
    assert len(project.prompts("gemini")) >= 2
    assert not project.prompts("codex")
    assert project.status(flow_id)["task_agents"] == {"loop.fix": "harness:gemini"}
    child_flows = list((project.work / ".atelier" / "flows" / flow_id / "flows").glob("*_child"))
    assert len(child_flows) >= 2
    assert all(json.loads((f / "progress.json").read_text())["task_agents"]["fix"]
               == "harness:gemini" for f in child_flows)
    assert {name: project.recipe(name) for name in before} == before


def test_nested_selector_errors_precede_flow_and_shared_call_isolated(tmp_path):
    project = _project(tmp_path)
    for selector in ("missing.fix=gemini", "loop.missing=gemini",
                     "loop.test=gemini", "loop..fix=gemini"):
        result = project.cli("run", "parent", "--agent", selector)
        assert result.returncode != 0, result.stdout + result.stderr
        assert "loop.fix" in result.stdout
        assert not project.flows()
    old_hint = project.cli("run", "parent", "--agent", "fix=gemini")
    assert old_hint.returncode != 0
    assert "use a dotted selector" in old_hint.stdout
    assert "did you mean 'loop'" not in old_hint.stdout

    parent = PARENT.replace("  - name: review", "  - name: second\n    description: call again\n    task: child\n    tool: tool:conduit\n    depends_on: [loop]\n  - name: review")
    project.install("parent", parent)
    run = project.cli("run", "parent", "--agent", "loop.fix=gemini")
    assert run.returncode == 0, run.stdout + run.stderr
    assert len(project.prompts("gemini")) >= 2
    assert len(project.prompts("codex")) == 1


def test_nested_model_probe_and_replacement_on_resume(tmp_path):
    project = _project(tmp_path)
    project.extras["gemini"] = {
        "models": {"current": "flash", "available": [
            {"id": "flash", "name": "Flash"},
            {"id": "pro", "name": "Pro"},
        ]},
        "efforts": {"current": "low", "available": ["low", "high"]},
    }
    project.apply_agents()
    choice = "loop.fix=gemini:pro:high"
    plan = project.cli("plan", "parent", "--agent", choice)
    assert plan.returncode == 0, plan.stdout + plan.stderr
    assert "harness:gemini:pro:high" in plan.stdout
    probe = project.cli(
        "check", "parent", "--recursive", "--probe", "--json",
        "--agent", choice,
    )
    assert probe.returncode == 0, probe.stdout + probe.stderr
    agents = json.loads(probe.stdout)[0]["probe"]["agents"]
    assert any(a["tool"] == "harness:gemini:pro:high" for a in agents)
    assert not project.prompts("gemini")

    project.break_agent("gemini")
    failed = project.cli("run", "parent", "--agent", choice)
    assert failed.returncode != 0
    flow_id = project.flow_id(failed)
    resumed = project.cli("run", "--resume", flow_id, "--agent", "loop.fix=codex")
    assert resumed.returncode == 0, resumed.stdout + resumed.stderr
    assert project.status(flow_id)["task_agents"] == {"loop.fix": "harness:codex"}
    assert len(project.prompts("codex")) >= 2
    assert not project.prompts("gemini")


def test_dynamic_child_target_cannot_be_selected(tmp_path):
    project = _project(tmp_path)
    project.install("parent", PARENT.replace("task: child", "task: '{{inputs.child}}'", 1)
                    .replace("tasks:\n", "inputs:\n  child:\n    default: child\ntasks:\n", 1))
    result = project.cli("run", "parent", "--agent", "loop.fix=gemini")
    assert result.returncode != 0
    assert "dynamic conduit target" in result.stdout
    assert not project.flows()


def test_baseline_ship_recipe_swaps_inner_agent_without_editing_yaml(tmp_path):
    """Run the original plan → fix/test loop → parallel reviews → verdict."""
    project = Project(tmp_path)
    baseline = Path(__file__).resolve().parents[1] / "fixtures/loop_ship"
    for name in ("ship", "fix_and_test"):
        project.install(name, (baseline / name / "conduit.yaml").read_text())
    for name in ("planner", "coder", "coder2", "reviewer", "lead"):
        project.records[name] = tmp_path / f"{name}-prompts"
        project.records[name].mkdir()
    project.env["ATELIER_HARNESSES"] = json.dumps({
        name: [sys.executable, str(FAKE_AGENT), "--script", json.dumps({
            "record_path": str(project.records[name]),
            "turns": [{"chunks": [f"{name} output"]}],
        })]
        for name in ("planner", "coder", "coder2", "reviewer", "lead")
    })
    before = {name: project.recipe(name) for name in ("ship", "fix_and_test")}
    plan = project.cli("plan", "ship", "--agent", "fix_until_green.fix=coder2")
    assert plan.returncode == 0, plan.stdout + plan.stderr
    assert "fix  [harness:coder2]" in plan.stdout
    assert "test  [tool:bash]" in plan.stdout
    run = project.cli(
        "run", "ship", "--agent", "fix_until_green.fix=coder2",
        "--input", "goal=add login",
    )
    assert run.returncode == 0, run.stdout + run.stderr
    status = project.status(project.flow_id(run))
    assert status["task_agents"] == {
        "fix_until_green.fix": "harness:coder2"
    }
    assert status["tasks"]["fix_until_green"]["iteration"] == 2
    assert len(project.prompts("coder2")) == 2
    assert not project.prompts("coder")
    assert {name: project.recipe(name) for name in before} == before


def test_resume_refuses_to_reassign_a_partly_completed_loop(tmp_path):
    project = _project(tmp_path)
    child = CHILD.replace(
        "if [ $n -ge 2 ]; then echo TESTS PASSED; else echo TESTS FAILED; fi",
        "if [ $n -ge 2 ]; then exit 1; else echo TESTS FAILED; fi",
    )
    project.install("child", child)
    failed = project.cli("run", "parent", "--agent", "loop.fix=gemini")
    assert failed.returncode != 0, failed.stdout + failed.stderr
    flow_id = project.flow_id(failed)
    refused = project.cli("run", "--resume", flow_id,
                          "--agent", "loop.fix=codex")
    assert refused.returncode != 0
    # The second pass failed; status and the refusal both name that real pass.
    assert "iteration 2" in refused.stdout
    assert "--again" in refused.stdout
    assert not project.prompts("codex")


def test_selectors_reach_as_deep_as_the_engine_runs():
    depth = MAX_NESTED_CONDUIT_DEPTH - 1

    def load(name: str) -> Conduit:
        level = int(name[1:])
        task = (
            {"description": "d", "task": "x", "tool": "harness:codex"}
            if level == depth else
            {"description": "d", "task": f"c{level + 1}", "tool": "tool:conduit"}
        )
        return Conduit.model_validate(
            {"name": name, "description": "d", "tasks": [{"t": task}]}
        )

    assert valid_agent_selectors(load("c0"), load) == [".".join(["t"] * (depth + 1))]
