"""Compare saved real CLI runs of the inline fix loop."""
from __future__ import annotations

import json

from tests.cli.test_agent_binding_workflow import Project


def _project(tmp_path):
    project = Project(tmp_path)
    created = project.cli("create", "ship", "--template", "fix-loop")
    assert created.returncode == 0, created.stdout + created.stderr
    (project.work / "check.sh").write_text(
        'n=$(cat .attempts 2>/dev/null || echo 0)\n'
        'n=$((n + 1))\n'
        'echo "$n" > .attempts\n'
        'if [ "$n" -lt 2 ]; then exit 1; fi\n'
    )
    return project


def test_compare_swapped_loop_agents_and_latest_pair(tmp_path):
    project = _project(tmp_path)
    recipe = project.recipe("ship")
    first = project.cli(
        "run", "ship", "--input", "goal=fix tests",
        "--input", "test_command=bash check.sh",
        "--agent", "fix_until_green.fix=codex",
    )
    assert first.returncode == 0, first.stdout + first.stderr
    first_id = project.flow_id(first)
    # Keep the counter to prove the comparator reports observed pass counts;
    # it does not attribute the difference to the agent swap.
    second = project.cli(
        "run", "ship", "--again", first_id,
        "--agent", "fix_until_green.fix=claude-code",
    )
    assert second.returncode == 0, second.stdout + second.stderr
    second_id = project.flow_id(second)
    before = {fid: project.saved_bytes(fid) for fid in (first_id, second_id)}

    pair = project.cli("compare", first_id, second_id, "--json")
    assert pair.returncode == 0, pair.stdout + pair.stderr
    data = json.loads(pair.stdout)
    assert data["conduit"] == "ship"
    assert [run["flow_id"] for run in data["runs"]] == [first_id, second_id]
    assert all(run["status"] == "completed" for run in data["runs"])
    assert all(run["duration_seconds"] >= 0 for run in data["runs"])
    a, b = data["runs"]
    assert a["tasks"]["fix_until_green.fix"]["agent"] == "harness:codex"
    assert b["tasks"]["fix_until_green.fix"]["agent"] == "harness:claude-code"
    assert a["tasks"]["fix_until_green"]["loop"] == {
        "passes": 2, "max_passes": 4, "condition_met": True,
    }
    assert b["tasks"]["fix_until_green"]["loop"] == {
        "passes": 1, "max_passes": 4, "condition_met": True,
    }
    assert data["tasks"] == sorted(data["tasks"], key=lambda row: row["task"])
    assert next(row for row in data["tasks"] if row["task"] == "fix_until_green.fix")["different"]
    assert a["tasks"]["plan"]["agent"] == "harness:claude-code"
    assert a["tasks"]["fix_until_green.test"]["agent"] == "tool:bash"

    text = project.cli("compare", "ship")
    assert text.returncode == 0, text.stdout + text.stderr
    assert first_id in text.stdout and second_id in text.stdout
    assert "2/4 condition met" in text.stdout
    assert "1/4 condition met" in text.stdout
    assert "condition met" in text.stdout
    assert "changed" in text.stdout
    assert project.recipe("ship") == recipe
    assert {fid: project.saved_bytes(fid) for fid in before} == before


def test_compare_errors_and_recipe_version_missing_task(tmp_path):
    project = _project(tmp_path)
    one = project.cli("compare", "ship")
    assert one.returncode != 0
    assert "two runs" in one.stdout
    assert "atelier run ship" in one.stdout
    first = project.cli("run", "ship", "--input", "goal=fix tests",
                        "--input", "test_command=bash check.sh")
    assert first.returncode == 0, first.stdout + first.stderr
    first_id = project.flow_id(first)
    assert project.cli("compare", "ship").returncode != 0

    project.install("other", """\
name: other
description: another conduit
tasks:
  - name: task
    description: say hello
    task: echo hello
    tool: tool:bash
    depends_on: []
""")
    other = project.cli("run", "other")
    assert other.returncode == 0, other.stdout + other.stderr
    different = project.cli("compare", first_id, project.flow_id(other))
    assert different.returncode != 0
    assert "different conduits" in different.stdout

    recipe = project.recipe("ship").decode()
    project.install("ship", recipe.replace(
        "  - verdict:\n",
        "  - new_step:\n      description: added later\n"
        "      task: echo new\n      tool: tool:bash\n"
        "      depends_on: [plan]\n  - verdict:\n",
    ))
    second = project.cli("run", "ship", "--again", first_id)
    assert second.returncode == 0, second.stdout + second.stderr
    compared = project.cli("compare", first_id, project.flow_id(second), "--json")
    assert compared.returncode == 0, compared.stdout + compared.stderr
    row = next(row for row in json.loads(compared.stdout)["tasks"]
               if row["task"] == "new_step")
    assert row["first"] is None
    assert row["second"]["status"] == "completed"
    assert row["different"] is True
