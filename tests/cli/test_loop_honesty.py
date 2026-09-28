"""Loop exhaustion and nested failures through separate CLI processes."""
from __future__ import annotations

import json
import sys
from pathlib import Path

from flow_atelier.modules.loop_report import LoopPass, loop_pass_lines
from flow_atelier.modules.nested_failure import NestedFailure, latest_nested_failure
from flow_atelier.schemas.conduit import Conduit
from flow_atelier.schemas.progress import Progress, TaskProgress, TaskStatus
from tests.cli.test_agent_binding_workflow import FAKE_AGENT, Project

BASELINE = Path(__file__).resolve().parents[1] / "fixtures/loop_ship"


def test_long_loop_timeline_keeps_ends_and_counts_hidden_passes():
    passes = [LoopPass(
        task="repeat", iteration=index, of=12,
        agents=["fix [harness:codex]"], result="TESTS FAILED",
        condition_met=False,
    ) for index in range(1, 13)]
    lines = loop_pass_lines(passes)
    assert len(lines) == 9
    assert "pass 1/12" in lines[0]
    assert "4 passes hidden" in lines[4]
    assert "pass 12/12" in lines[-1]


def test_nested_failure_names_iteration_only_for_repeated_calls():
    once = NestedFailure(flow_id="f", calling_task="call", task="fix",
                         tool="harness:coder", iteration=1, of=1, reason="boom")
    assert once.summary == "call -> fix [harness:coder]: boom"
    looped = NestedFailure(flow_id="f", calling_task="call", task="fix",
                           tool="harness:coder", iteration=2, of=4, reason="boom")
    assert looped.summary == "call -> fix [harness:coder], loop iteration 2/4: boom"


class _Store:
    """Just enough of a store for reading one parent's saved children."""

    def __init__(self, children):
        self.children = children

    def list_child_flows(self, parent):
        return list(self.children)

    def read_progress(self, flow_id):
        return self.children[flow_id]

    def read_logs(self, flow_id):
        return []

    def read_conduit(self, name):
        assert name == "child"
        return Conduit.model_validate({"name": "child", "description": "d", "tasks": [
            {"fix": {"description": "d", "task": "x", "tool": "harness:coder"}},
        ]})


def test_nested_failure_ignores_children_of_an_earlier_attempt():
    old = Progress(
        invoking_task="call", finished_at="2026-09-28T10:00:00.000001Z",
        tasks={"fix": TaskProgress(status=TaskStatus.failed, reason="old")},
    )
    store = _Store({"20260928_aaaaaaaa_child": old})
    assert latest_nested_failure(
        store, "parent", "call", since="2026-09-28T11:00:00Z"
    ) is None
    found = latest_nested_failure(store, "parent", "call", since="2026-09-28T09:00:00Z")
    assert found is not None and found.reason == "old"
    # Failed before any log entry (e.g. a missing input): the recipe names it.
    assert found.tool == "harness:coder"


def _project(tmp_path, *, broken_coder=False):
    project = Project(tmp_path)
    for name in ("ship", "fix_and_test"):
        project.install(name, (BASELINE / name / "conduit.yaml").read_text())
    harnesses = {}
    for name in ("planner", "coder", "coder2", "reviewer", "lead"):
        script = {"turns": [{"chunks": [name.upper()]}]}
        if name == "coder" and broken_coder:
            script["fail_session"] = "login expired"
        harnesses[name] = [sys.executable, str(FAKE_AGENT), "--script", json.dumps(script)]
    project.env["ATELIER_HARNESSES"] = json.dumps(harnesses)
    return project


def _flat(text):
    return " ".join(text.split())


def test_exhaustion_is_visible_before_and_after_run(tmp_path):
    project = _project(tmp_path)
    (project.work / ".attempts").write_text("-100")
    before = {name: project.recipe(name) for name in ("ship", "fix_and_test")}
    for command in ("check", "plan"):
        result = project.cli(command, "ship")
        assert result.returncode == 0, result.stdout + result.stderr
        assert "on_exhaust: fail" in result.stdout
        assert "review_security, review_style, verdict" in _flat(result.stdout)
    planned = json.loads(project.cli("plan", "ship", "--json").stdout)
    assert planned["waves"][1][0]["exhaustion_warning"]

    run = project.cli("run", "ship", "--input", "goal=x")
    assert run.returncode == 0, run.stdout + run.stderr
    flow_id = project.flow_id(run)
    assert "stopped after 4/4 iterations without meeting until: output.match(TESTS PASSED)" in _flat(run.stdout)
    status = project.status(flow_id)
    assert status["status"] == "completed"
    assert status["tasks"]["fix_until_green"]["loop_outcome"]["met"] is False
    assert "dependent tasks review_security, review_style, verdict ran" in status["loop_warnings"][0]
    assert "stopped after 4/4" in _flat(project.cli("status", flow_id).stdout)
    diagnose = project.cli("diagnose", flow_id)
    assert "stopped after 4/4" in _flat(diagnose.stdout)
    assert "on_exhaust: fail" in diagnose.stdout
    assert "nothing to recover" not in diagnose.stdout
    assert {name: project.recipe(name) for name in before} == before


def test_nested_auth_failure_names_agent_and_parent_recovery(tmp_path):
    project = _project(tmp_path, broken_coder=True)
    (project.work / ".attempts").write_text("0")
    run = project.cli("run", "ship", "--input", "goal=x")
    assert run.returncode == 1
    flow_id = project.flow_id(run)
    assert "fix_until_green -> fix [harness:coder], loop iteration 1/4" in _flat(run.stdout)
    assert "Authentication required" in run.stdout
    assert "Traceback (most recent call last)" not in run.stdout
    assert "File \"/" not in run.stdout
    diagnose = project.cli("diagnose", flow_id)
    assert diagnose.returncode == 0
    assert "fix_until_green -> fix [harness:coder], loop iteration 1/4" in _flat(diagnose.stdout)
    assert "Traceback (most recent call last)" not in diagnose.stdout
    assert f"atelier run --resume {flow_id}" in _flat(diagnose.stdout)
    assert f"atelier run --resume {flow_id} --agent fix_until_green.fix=<harness>" in _flat(diagnose.stdout)
    report = json.loads(project.cli("diagnose", flow_id, "--json").stdout)
    assert report["nested_failures"][0]["tool"] == "harness:coder"
    # The raw child log remains available for the full Python traceback.
    child = report["nested_failures"][0]["flow_id"]
    raw = project.cli("logs", child, "--show", "all")
    assert "Traceback (most recent call last)" in raw.stdout
    resumed = project.cli("run", "--resume", flow_id, "--agent", "fix_until_green.fix=coder2")
    assert resumed.returncode == 0, resumed.stdout + resumed.stderr
    assert project.status(flow_id)["status"] == "completed"


def test_explicit_exhaustion_failure_keeps_exit_code_and_suppresses_warning(tmp_path):
    project = _project(tmp_path)
    (project.work / ".attempts").write_text("-100")
    recipe = project.recipe("ship").decode().replace(
        "until: output.match(TESTS PASSED)",
        "until: output.match(TESTS PASSED)\n      on_exhaust: fail",
    )
    project.install("ship", recipe)
    for command in ("check", "plan"):
        result = project.cli(command, "ship")
        assert result.returncode == 0
        assert "dependent tasks" not in result.stdout
    run = project.cli("run", "ship", "--input", "goal=x")
    assert run.returncode == 1
    status = project.status(project.flow_id(run))
    assert status["status"] == "failed"
    assert status["tasks"]["review_security"]["status"] == "cancelled"
