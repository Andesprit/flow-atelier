"""The README's build-until-green loop through the real CLI and fake agents."""
from __future__ import annotations

from pathlib import Path

from tests.cli.test_agent_binding_workflow import Project

# The README hero recipe, without its final human gate: stdin is closed here.
SHIP = """\
name: ship
description: Plan with Codex, build with Claude until tests pass
inputs:
  goal: What to build
tasks:
  - plan:
      description: Codex writes the plan
      tool: harness:codex
      task: "Plan how to achieve this goal: {{inputs.goal}}"
      depends_on: []
  - build_until_green:
      description: build and test until green
      tool: tool:conduit
      depends_on: [plan]
      repeat: 5
      until: output.match(TESTS PASSED)
      on_exhaust: fail
      inputs:
        goal: "{{inputs.goal}}"
        plan: "{{plan.output}}"
        feedback: "{{loop.previous}}"
      tasks:
        - build:
            description: Claude Code makes the change
            tool: harness:claude-code
            task: "Goal: {{inputs.goal}}\\nPlan: {{inputs.plan}}\\nLast test run: {{inputs.feedback}}"
        - test:
            description: run the tests
            tool: tool:bash
            depends_on: [build]
            task: "sh make_test.sh && echo 'TESTS PASSED'"
"""

# Stands in for `make test`: two failing runs, then a green one.
MAKE_TEST = """\
n=$(cat .runs 2>/dev/null || echo 0)
n=$((n + 1))
echo "$n" > .runs
if [ "$n" -lt 3 ]; then
  echo "test_login FAILED (run $n)"
  echo "AssertionError: expected 200, got 500" >&2
  exit 2
fi
echo "3 passed"
"""


def test_readme_loop_runs_until_the_tests_pass(tmp_path: Path):
    project = Project(tmp_path)
    project.install("ship", SHIP)
    (project.work / "make_test.sh").write_text(MAKE_TEST, newline="\n")

    run = project.cli("run", "ship", "--input", "goal=add dark mode")
    assert run.returncode == 0, run.stdout + run.stderr

    flow_id = project.flow_id(run)
    status = project.status(flow_id)
    assert status["status"] == "completed"
    loop = status["tasks"]["build_until_green"]
    assert (loop["status"], loop["iteration"], loop["of"]) == ("completed", 3, 5)
    assert loop["loop_outcome"]["met"] is True
    children = list((project.work / ".atelier" / "flows" / flow_id / "flows").iterdir())
    assert len(children) == 3

    prompts = project.prompts("claude-code")
    assert len(prompts) == 3
    assert "failed" not in prompts[0]
    for run_number, prompt in enumerate(prompts[1:], 1):
        # The body failed as a whole; the test's own output and error follow.
        assert "[this try failed with exit code 1]" in prompt
        assert f"test_login FAILED (run {run_number})" in prompt
        assert "AssertionError: expected 200, got 500" in prompt
        assert (
            "build_until_green -> test [tool:bash]: "
            "AssertionError: expected 200, got 500" in prompt
        )
    assert len(project.prompts("codex")) == 1
