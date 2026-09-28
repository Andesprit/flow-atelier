"""A one-file loop through real CLI processes and fake ACP agents."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
import yaml
from jsonschema import Draft202012Validator

from tests.cli.test_agent_binding_workflow import FAKE_AGENT, Project

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"


def _project(tmp_path: Path, *, inline: bool = True, broken_agent: str = "") -> Project:
    project = Project(tmp_path)
    fixture = FIXTURES / ("loop_ship_inline" if inline else "loop_ship")
    project.install("ship", (fixture / "ship" / "conduit.yaml").read_text())
    if not inline:
        project.install("fix_and_test", (fixture / "fix_and_test" / "conduit.yaml").read_text())
    harnesses = {}
    for name in ("planner", "coder", "coder2", "reviewer", "lead"):
        record = tmp_path / f"{name}-prompts"
        record.mkdir()
        project.records[name] = record
        script = {
            "record_path": str(record),
            "turns": [{"chunks": [f"{name} output"]}],
        }
        if name == broken_agent:
            script["fail_session"] = "login expired"
        harnesses[name] = [sys.executable, str(FAKE_AGENT), "--script", json.dumps(script)]
    project.env["ATELIER_HARNESSES"] = json.dumps(harnesses)
    return project


@pytest.mark.parametrize("inline", [False, True])
def test_one_file_matches_shared_child_plan_run_and_probe(tmp_path: Path, inline: bool):
    project = _project(tmp_path, inline=inline)
    before = project.recipe("ship")
    for args in (("check", "ship"), ("check", "ship", "--recursive")):
        checked = project.cli(*args)
        assert checked.returncode == 0, checked.stdout + checked.stderr
    plan = project.cli("plan", "ship", "--json", "--agent", "fix_until_green.fix=coder2")
    assert plan.returncode == 0, plan.stdout + plan.stderr
    loop = json.loads(plan.stdout)["waves"][1][0]
    assert loop["child"]["conduit_name"] == (
        "ship.fix_until_green" if inline else "fix_and_test"
    )
    tasks = [task for wave in loop["child"]["waves"] for task in wave]
    assert [(t["name"], t["tool"]) for t in tasks] == [
        ("fix", "harness:coder2"), ("test", "tool:bash")
    ]
    probe = project.cli(
        "check", "ship", "--recursive", "--probe", "--json",
        "--agent", "fix_until_green.fix=coder2",
    )
    assert probe.returncode == 0, probe.stdout + probe.stderr
    agents = json.loads(probe.stdout)[0]["probe"]["agents"]
    expected_path = "ship.fix_until_green.fix" if inline else "ship.fix_until_green -> fix_and_test.fix"
    assert any(a["tool"] == "harness:coder2" and any(
        t["path"] == expected_path for t in a["tasks"])
               for a in agents)
    assert not project.prompts("coder2")

    run = project.cli("run", "ship", "--agent", "fix_until_green.fix=coder2",
                      "--input", "goal=add login")
    assert run.returncode == 0, run.stdout + run.stderr
    flow_id = project.flow_id(run)
    status = project.status(flow_id)
    assert status["tasks"]["fix_until_green"]["iteration"] == 2
    assert status["task_agents"] == {"fix_until_green.fix": "harness:coder2"}
    assert len(project.prompts("coder2")) == 2
    assert "TESTS FAILED" in str(project.prompts("coder2")[1])
    assert not project.prompts("coder")
    assert project.recipe("ship") == before
    child_flows = list((project.work / ".atelier" / "flows" / flow_id / "flows").iterdir())
    assert len(child_flows) == 2
    assert all(json.loads((f / "progress.json").read_text())["task_agents"]["fix"]
               == "harness:coder2" for f in child_flows)
    if inline:
        assert [p.name for p in (project.work / ".atelier" / "conduits").iterdir()] == ["ship"]


def test_inline_failure_diagnose_and_parent_resume(tmp_path: Path):
    project = _project(tmp_path, broken_agent="coder")
    run = project.cli("run", "ship", "--input", "goal=add login")
    assert run.returncode == 1, run.stdout + run.stderr
    flow_id = project.flow_id(run)
    assert "fix_until_green -> fix [harness:coder], loop iteration 1/4" in " ".join(run.stdout.split())
    assert "Traceback (most recent call last)" not in run.stdout
    for command in (("status", flow_id), ("diagnose", flow_id)):
        report = project.cli(*command)
        assert report.returncode == 0, report.stdout + report.stderr
        assert "fix_until_green" in report.stdout
    diagnose = project.cli("diagnose", flow_id)
    assert "fix [harness:coder]" in " ".join(diagnose.stdout.split())
    assert "fix_until_green pass 1/4" in " ".join(diagnose.stdout.split())
    assert "condition unknown" in " ".join(diagnose.stdout.split())
    assert f"atelier run --resume {flow_id}" in " ".join(diagnose.stdout.split())
    assert "Traceback (most recent call last)" not in diagnose.stdout
    resumed = project.cli("run", "--resume", flow_id,
                          "--agent", "fix_until_green.fix=coder2")
    assert resumed.returncode == 0, resumed.stdout + resumed.stderr
    assert project.status(flow_id)["status"] == "completed"
    assert len(project.prompts("coder2")) == 2


def test_inline_saved_choice_and_exhaustion(tmp_path: Path):
    project = _project(tmp_path, broken_agent="coder2")
    failed = project.cli("run", "ship", "--input", "goal=x",
                         "--agent", "fix_until_green.fix=coder2")
    assert failed.returncode == 1
    flow_id = project.flow_id(failed)
    assert project.status(flow_id)["task_agents"] == {
        "fix_until_green.fix": "harness:coder2"
    }
    harnesses = json.loads(project.env["ATELIER_HARNESSES"])
    argv = harnesses["coder2"]
    script = json.loads(argv[-1])
    script.pop("fail_session")
    argv[-1] = json.dumps(script)
    project.env["ATELIER_HARNESSES"] = json.dumps(harnesses)
    resumed = project.cli("run", "--resume", flow_id)
    assert resumed.returncode == 0, resumed.stdout + resumed.stderr
    assert len(project.prompts("coder2")) == 2
    assert not project.prompts("coder")

    (project.work / ".attempts").write_text("-100")
    exhausted = project.cli("run", "ship", "--input", "goal=x")
    assert exhausted.returncode == 0, exhausted.stdout + exhausted.stderr
    exhausted_id = project.flow_id(exhausted)
    assert project.status(exhausted_id)["tasks"]["fix_until_green"]["loop_outcome"]["met"] is False
    diagnosed = project.cli("diagnose", exhausted_id)
    assert "on_exhaust: fail" in diagnosed.stdout
    assert "stopped after 4/4" in " ".join(diagnosed.stdout.split())


def test_inline_validation_and_namespace(tmp_path: Path):
    project = _project(tmp_path)
    recipe = project.recipe("ship").decode()
    for old, new, wanted in (
        ("depends_on: [fix]", "depends_on: [missing]", "ship.fix_until_green"),
        ("tool: tool:bash", "tool: tool:unknown", "fix_until_green"),
        ("depends_on: [fix]", "depend_on: [fix]", "depend_on"),
    ):
        project.install("ship", recipe.replace(old, new, 1))
        checked = project.cli("check", "ship")
        assert checked.returncode != 0
        assert wanted in checked.stdout
    project.install("ship", recipe)
    show = project.cli("show", "ship")
    schema = project.cli("schema")
    assert show.returncode == schema.returncode == 0
    assert "tasks:" in show.stdout
    document = yaml.safe_load(recipe)
    validator = Draft202012Validator(json.loads(schema.stdout))
    assert list(validator.iter_errors(document)) == []
    normalized = json.loads(project.cli("show", "ship", "--json").stdout)["conduit"]
    assert list(validator.iter_errors(normalized)) == []
    # A physical conduit with a name resembling the internal body cannot
    # replace its contents, and ordinary installed names remain independent.
    project.install("inline-ship-fix_until_green", "name: inline-ship-fix_until_green\n"
                    "description: independent\ntasks:\n  - ping:\n      description: ping\n"
                    "      task: echo PING\n      tool: tool:bash\n")
    assert project.cli("plan", "ship").returncode == 0
    assert project.cli("plan", "inline-ship-fix_until_green").returncode == 0
