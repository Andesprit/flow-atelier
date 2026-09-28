"""The documented starter through real CLI processes and fake ACP agents."""
from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

import yaml

from tests.cli.test_agent_binding_workflow import Project

ROOT = Path(__file__).resolve().parents[2]
GUIDE = ROOT / "docs" / "loop-agents-graph.md"


def _project(tmp_path: Path) -> Project:
    project = Project(tmp_path)
    subprocess.run(["git", "init", "-q"], cwd=project.work, check=True)
    (project.work / "check.sh").write_text(
        'n=$(cat .attempts 2>/dev/null || echo 0)\n'
        'n=$((n + 1))\n'
        'echo "$n" > .attempts\n'
        'if [ "$n" -lt 2 ]; then echo "one failing test"; exit 1; fi\n'
        'echo "test suite green"\n',
    )
    (project.work / "always-fail.sh").write_text(
        'echo "one failing test"\nexit 1\n'
    )
    return project


def test_fix_loop_starter_and_walkthrough(tmp_path: Path):
    project = _project(tmp_path)
    guide = GUIDE.read_text()
    commands = (
        "atelier create ship --template fix-loop",
        "atelier check ship --recursive",
        "atelier plan ship --agent fix_until_green.fix=codex",
        "atelier run ship --input goal='make the demo pass' --input test_command='bash check.sh' --agent fix_until_green.fix=codex",
        "atelier run ship --input goal='make the demo pass' --input test_command='bash always-fail.sh' --agent fix_until_green.fix=codex",
        "atelier diagnose latest",
    )
    for command in commands:
        assert command in guide

    help_result = project.cli("create", "--help")
    assert help_result.returncode == 0
    assert "fix-loop" in help_result.stdout
    assert "reviews in parallel" in help_result.stdout

    created = project.cli("create", "ship", "--template", "fix-loop")
    assert created.returncode == 0, created.stdout + created.stderr
    recipe_path = project.work / ".atelier/conduits/ship/conduit.yaml"
    recipe = recipe_path.read_bytes()
    assert [p.name for p in recipe_path.parent.parent.iterdir()] == ["ship"]
    hints = " ".join(created.stdout.split())
    for hint in (
        "atelier check ship --recursive", "atelier plan ship",
        "atelier run ship", "--agent fix_until_green.fix=codex",
        "atelier diagnose latest",
    ):
        assert hint in hints
    assert (
        "→ atelier run ship --agent fix_until_green.fix=codex --input goal='fix tests'"
        in created.stdout
    )
    parsed = yaml.safe_load(recipe)
    assert parsed["inputs"]["test_command"]["default"] == "python -m pytest -q"
    assert parsed["max_concurrency"] >= 2

    checked = project.cli("check", "ship", "--recursive")
    assert checked.returncode == 0, checked.stdout + checked.stderr
    planned = project.cli("plan", "ship", "--json", "--agent", "fix_until_green.fix=codex")
    assert planned.returncode == 0, planned.stdout + planned.stderr
    waves = json.loads(planned.stdout)["waves"]
    assert [[task["name"] for task in wave] for wave in waves] == [
        ["plan"], ["fix_until_green"],
        ["review_correctness", "review_maintainability"], ["verdict"],
    ]
    loop = waves[1][0]
    assert loop["loop_text"] == "x4 until output.match(TESTS PASSED) on_exhaust=fail"
    body = [task for wave in loop["child"]["waves"] for task in wave]
    assert [(task["name"], task["tool"]) for task in body] == [
        ("fix", "harness:codex"), ("test", "tool:bash"),
    ]
    assert not project.prompts("codex")

    run = project.cli(
        "run", "ship", "--input", "goal=make the demo pass",
        "--input", "test_command=bash check.sh",
        "--agent", "fix_until_green.fix=codex",
    )
    assert run.returncode == 0, run.stdout + run.stderr
    flow_id = project.flow_id(run)
    status = project.status(flow_id)
    assert status["status"] == "completed"
    assert status["tasks"]["fix_until_green"]["iteration"] == 2
    assert status["tasks"]["fix_until_green"]["of"] == 4
    assert all(status["tasks"][name]["status"] == "completed" for name in (
        "review_correctness", "review_maintainability", "verdict",
    ))
    assert len(project.prompts("codex")) == 2
    assert "TESTS FAILED" in project.prompts("codex")[1]
    assert len(project.prompts("claude-code")) == 4
    assert recipe_path.read_bytes() == recipe
    banners = re.findall(r"▶ \[(\d+)/(\d+)\]", run.stdout)
    assert banners and all(int(index) <= int(total) for index, total in banners)
    assert "fix_until_green 2/4 > fix" in run.stdout
    assert "~inline~" not in run.stdout
    assert "condition not met" in run.stdout
    assert "condition met" in run.stdout
    assert [item["condition_met"] for item in status["loop_passes"]] == [False, True]
    assert all(item["agents"] == ["fix [harness:codex]"] for item in status["loop_passes"])
    passed_status_text = project.cli("status", flow_id).stdout
    assert "fix_until_green pass 2/4" in passed_status_text
    assert "TESTS PASSED · condition met" in passed_status_text

    failed = project.cli(
        "run", "ship", "--input", "goal=make the demo pass",
        "--input", "test_command=bash always-fail.sh",
        "--agent", "fix_until_green.fix=codex",
    )
    assert failed.returncode == 1, failed.stdout + failed.stderr
    failed_id = project.flow_id(failed)
    failed_status = project.status(failed_id)
    assert failed_status["status"] == "failed"
    assert "exhausted 4 iterations" in failed_status["tasks"]["fix_until_green"]["reason"]
    assert failed_status["tasks"]["fix_until_green"]["iteration"] == 4
    assert len(failed_status["loop_passes"]) == 4
    assert all(item["condition_met"] is False for item in failed_status["loop_passes"])
    failed_banners = re.findall(r"▶ \[(\d+)/(\d+)\]", failed.stdout)
    assert failed_banners and all(int(index) <= int(total) for index, total in failed_banners)
    assert "fix_until_green 4/4 > fix" in failed.stdout
    assert "~inline~" not in failed.stdout
    assert "condition not met" in failed.stdout
    assert "✓ fix_until_green [tool:conduit] (4/4)" not in failed.stdout
    failed_status_text = project.cli("status", failed_id).stdout
    assert "fix_until_green pass 4/4" in failed_status_text
    assert "TESTS FAILED · condition not met" in " ".join(failed_status_text.split())
    assert failed_status["tasks"]["review_correctness"]["status"] == "cancelled"
    diagnosis = project.cli("diagnose", failed_id)
    assert diagnosis.returncode == 0, diagnosis.stdout + diagnosis.stderr
    assert "fix_until_green" in diagnosis.stdout
    assert "exhausted 4 iterations" in " ".join(diagnosis.stdout.split())
    assert "TESTS FAILED" in diagnosis.stdout
    assert "fix_until_green pass 4/4" in diagnosis.stdout
    assert "fix [harness:codex]" in diagnosis.stdout
    assert "~inline~" not in diagnosis.stdout

    (project.work / ".attempts").unlink()
    repaired = project.cli(
        "run", "ship", "--input", "goal=make the demo pass",
        "--input", "test_command=bash check.sh",
        "--agent", "fix_until_green.fix=codex",
    )
    assert repaired.returncode == 0, repaired.stdout + repaired.stderr
    assert project.status(project.flow_id(repaired))["status"] == "completed"
    assert recipe_path.read_bytes() == recipe


def test_fix_loop_refuses_existing_name(tmp_path: Path):
    project = _project(tmp_path)
    assert project.cli("create", "ship", "--template", "fix-loop").returncode == 0
    before = project.recipe("ship")
    again = project.cli("create", "ship", "--template", "fix-loop")
    assert again.returncode == 1
    assert "already exists" in again.stdout
    assert project.recipe("ship") == before
