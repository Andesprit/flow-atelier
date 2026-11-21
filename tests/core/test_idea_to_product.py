"""Run the bundled product workflow in an empty target with fake agent turns."""
import os

import pytest

from flow_atelier.core.atelier import Atelier
from flow_atelier.schemas.log import ExecutionResult
from flow_atelier.services.executor.base import ExecutorBase

PHRASE = "Build a reading list"
PRD = "# Reading list PRD\n\nReaders can save a title.\n\nA saved title survives restart."
SPEC = "Save titles on disk.\n\n## Acceptance criteria\n- A saved title survives restart."
TEST = "bash verify.sh"


class FakeHarness(ExecutorBase):
    def __init__(self, tool):
        self.tool = tool
        self.spec = SPEC + "\n\nTest command: " + TEST
        self.calls = []

    async def execute(self, task, resolved_command, context):
        assert task.tool == self.tool
        self.calls.append((task.name, resolved_command))
        if task.name == "spec":
            assert task.permissions == "read-only"
            return ExecutionResult(output=self.spec)
        if task.name == "build":
            script = context.working_dir / "verify.sh"
            script.write_text(
                'echo test >> attempts\n'
                'if [ "$(wc -l < attempts)" -eq 1 ]; then echo "first test failed"; exit 1; fi\n'
            )
            # A marker in a builder response is never enough to finish a pass.
            return ExecutionResult(output="PASS")
        assert task.name == "review" and task.permissions == "read-only"
        reviews = sum(name == "review" for name, _ in self.calls)
        return ExecutionResult(output="Fix persistence" if reviews == 1 else "PASS")


@pytest.fixture
def workflow(tmp_path, monkeypatch):
    target = tmp_path / "empty target"
    target.mkdir()
    monkeypatch.chdir(target)
    atelier = Atelier()
    atelier.install_package("builtin:platform")
    planner = FakeHarness("harness:codex")
    builder = FakeHarness("harness:claude-code")
    atelier.executors.update({planner.tool: planner, builder.tool: builder})
    # Capture the trusted shell step's invocation of the separately tested sender.
    bindir = tmp_path / "bin"
    bindir.mkdir()
    sender = bindir / "atelier"
    sender.write_text(
        '#!/bin/sh\n[ "$1" = notify ] && [ "$2" = "$ATELIER_FLOW_ID" ] || exit 1\n'
        'printf "%s\\n" "$@" > receipt\n'
    )
    sender.chmod(0o755)
    monkeypatch.setenv("PATH", str(bindir) + os.pathsep + os.environ["PATH"])

    async def run(idea=PHRASE, answer="APPROVE", *, overrides=None, agents=None):
        async def respond(*_args, **_kwargs):
            return answer
        monkeypatch.setattr("flow_atelier.cli.rendering.multiline_input.multiline_input", respond)
        return await atelier.run_conduit(
            "idea-to-product", {"idea": idea, **(overrides or {})}, working_dir=target, agents=agents,
        )

    return run, target, planner, builder, atelier


async def test_rejected_phrase_leaves_target_untouched(workflow, capsys):
    run, target, planner, builder, _ = workflow
    with pytest.raises(RuntimeError, match="approval"):
        await run(answer="decline", overrides={"decision": "APPROVE"})
    assert PHRASE in planner.calls[0][1]
    assert "Acceptance criteria" in planner.calls[0][1]
    prompt = capsys.readouterr().out
    assert SPEC in prompt and TEST in prompt and str(target) in prompt
    assert list(target.iterdir()) == [] and builder.calls == []


async def test_prd_needs_green_test_and_independent_review(workflow):
    run, target, planner, builder, atelier = workflow
    flow = await run(PRD)
    assert PRD in planner.calls[0][1]
    prompts = [prompt for _, prompt in builder.calls]
    assert len(prompts) == 3 and all(SPEC in prompt and TEST in prompt for prompt in prompts)
    assert "first test failed" in prompts[1] and "Fix persistence" in prompts[2]
    assert [name for name, _ in planner.calls] == ["spec", "review", "review"]
    assert (target / "attempts").read_text().splitlines() == ["test"] * 3
    assert (target / "receipt").read_text().splitlines()[:2] == ["notify", flow]
    assert atelier.get_status(flow).status == "completed"


async def test_explicit_command_wins_and_stops_after_three_failures(workflow):
    run, target, planner, builder, _ = workflow
    planner.spec = SPEC + "\nTest command: touch wrong-command"
    with pytest.raises(RuntimeError):
        await run(overrides={"test_command": "echo red >> runs; exit 7"})
    assert (target / "runs").read_text().splitlines() == ["red"] * 3
    assert len(builder.calls) == 3 and [n for n, _ in planner.calls] == ["spec"]
    assert not (target / "receipt").exists()
    assert not (target / "wrong-command").exists()


@pytest.mark.parametrize(("spec", "reason"), [
    (SPEC, "test command"),
    ("A spec with no criteria.\nTest command: true", "acceptance criteria"),
])
async def test_incomplete_spec_stops_before_approval(workflow, capsys, spec, reason):
    run, target, planner, builder, _ = workflow
    planner.spec = spec
    with pytest.raises(RuntimeError, match=reason):
        await run()
    assert "[hitl]" not in capsys.readouterr().out
    assert not list(target.iterdir()) and not builder.calls


@pytest.mark.parametrize(("problem", "reason"), [
    ("same", "require different harnesses"),
    ("alias", "require different harnesses"),
    ("missing", "missing harness"),
    ("unavailable", "not installed"),
])
async def test_requires_two_available_harnesses_before_build(workflow, problem, reason, monkeypatch):
    run, target, planner, builder, atelier = workflow
    agents = {}
    if problem == "same":
        agents = {"attempt.review": "claude-code"}
    elif problem == "alias":
        builder.launch_cmd = planner.launch_cmd = ["same-agent"]
    elif problem == "missing":
        del atelier.executors["harness:claude-code"]
    else:
        monkeypatch.setattr(builder, "is_available", lambda: (False, "not installed"))
    with pytest.raises((ValueError, RuntimeError), match=reason):
        await run(agents=agents)
    assert not list(target.iterdir()) and not builder.calls

