"""Runs `docs/multi-agent-workflow.md` for real, so the guide cannot drift.

The published blocks are executed verbatim, in order, in one shell with stdin
closed and a workspace path containing a space. Only the agents are faked:
the two `ATELIER_*_LAUNCH_CMD` overrides point `claude-code` and `codex` at
the scripted ACP agent, which records every prompt it is handed.

The second test breaks the guide's own prerequisite check and asserts that
nothing downstream of it ran — the `|| exit 1` on every line is the promise
being tested.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import sys
from pathlib import Path

import pytest

from flow_atelier.services.executor.bash import to_bash_path
from flow_atelier.services.store.filesystem import FilesystemStore
from tests._shell import record_path, run_script, write_shim

_REPO_ROOT = Path(__file__).resolve().parents[2]
_GUIDE = _REPO_ROOT / "docs" / "multi-agent-workflow.md"
_FAKE_AGENT = _REPO_ROOT / "tests" / "fixtures" / "fake_acp_agent.py"
_BASH_BLOCK = re.compile(r"^```bash\n(.*?)^```$", re.MULTILINE | re.DOTALL)
# prerequisites, workspace, compose, inspect, run, read back, compose the
# panel, run the panel.
_EXPECTED_BLOCKS = 8

CLAUDE_SAID = "CLAUDE_REPLY: move it onto the scheduler."
CODEX_SAID = "CODEX_REPLY: the retry window is wrong."


def _guide_script() -> str:
    """Return the guide's bash blocks, concatenated in document order.

    No ``set -e``: the published blocks gate themselves with `|| exit 1`, and
    that self-gating is exactly what this test has to exercise.

    :returns: one shell script.
    """
    blocks = [m.group(1) for m in _BASH_BLOCK.finditer(_GUIDE.read_text(encoding="utf-8"))]
    assert len(blocks) == _EXPECTED_BLOCKS, (
        f"expected {_EXPECTED_BLOCKS} bash blocks in {_GUIDE.name}, got {len(blocks)}"
    )
    body = "\n".join(blocks)
    for required in ("atelier compose triage", "atelier compose panel --parallel"):
        assert required in body, f"the guide no longer runs `{required}`"
    tail = "\nstatus=$?\n" + record_path("work_dir", "WORKSPACE_MARKER") + "exit $status\n"
    return body + tail


def _agent_env(record_dir: Path, text: str) -> str:
    """Build a launch-command override for one scripted fake agent.

    :param record_dir: directory the agent writes its per-process prompt log to.
    :param text: what the agent replies.
    :returns: the JSON argv for an ``ATELIER_*_LAUNCH_CMD`` variable.
    """
    record_dir.mkdir(parents=True, exist_ok=True)
    script = {"turns": [{"chunks": [text]}], "record_path": str(record_dir)}
    return json.dumps([sys.executable, str(_FAKE_AGENT), "--script", json.dumps(script)])


@pytest.fixture
def guide_env(tmp_path, request):
    """An isolated environment whose `atelier` is this checkout's CLI.

    :param tmp_path: pytest temp directory fixture.
    :param request: pytest request; a truthy ``param`` breaks `harness check`.
    :yields: ``(env, marker, records)`` — the child environment, the file the
        script writes its workspace path into, and the prompt-log directories.
    """
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    guard = (
        'if [ "$1" = harness ]; then echo "INJECTED agent unreachable" >&2; exit 1; fi\n'
        if getattr(request, "param", False)
        else ""
    )
    write_shim(bin_dir, guard)
    marker = tmp_path / "workspace-path"
    records = {name: tmp_path / f"{name}-prompts" for name in ("claude", "codex")}

    env = {k: v for k, v in os.environ.items() if not k.startswith("ATELIER_")}
    env["ATELIER_GLOBAL_ATELIER_DIR"] = str(tmp_path / "global")
    env["ATELIER_NO_UPDATE_CHECK"] = "1"
    env["ATELIER_CLAUDE_LAUNCH_CMD"] = _agent_env(records["claude"], CLAUDE_SAID)
    env["ATELIER_CODEX_LAUNCH_CMD"] = _agent_env(records["codex"], CODEX_SAID)
    env["PATH"] = f"{bin_dir}{os.pathsep}{env.get('PATH', '')}"
    env["WORKSPACE_MARKER"] = to_bash_path(marker)
    yield env, marker, records
    # `mktemp -d` puts the workspace outside tmp_path and the guide never
    # deletes it, so the test is what cleans up.
    if marker.exists():
        shutil.rmtree(Path(marker.read_text().strip()).parent, ignore_errors=True)


def _prompts(record_dir: Path) -> list[str]:
    """Return every prompt one agent received, across its processes.

    :param record_dir: the agent's per-process prompt log directory.
    :returns: one joined text block per prompt call.
    """
    return [
        "\n".join(block.get("text", "") for block in json.loads(line))
        for log in sorted(record_dir.glob("*.jsonl"))
        for line in log.read_text(encoding="utf-8").splitlines()
    ]


def test_readme_points_at_a_guide_that_exists():
    """The discovery link beside the compose quickstart resolves to this file."""
    readme = (_REPO_ROOT / "README.md").read_text(encoding="utf-8")
    assert "(docs/multi-agent-workflow.md)" in readme
    assert _GUIDE.is_file()


def test_the_guide_runs_end_to_end_and_claims_only_what_happens(guide_env, tmp_path):
    """Execute the published commands, then verify the saved runs."""
    env, marker, records = guide_env
    run = run_script(
        _guide_script(), tmp_path / "guide.sh", tmp_path, env, timeout=300
    )
    assert run.returncode == 0, f"stdout:\n{run.stdout}\nstderr:\n{run.stderr}"
    out = run.stdout

    # The guide's stated intermediate observations, as the commands printed them.
    assert '"required_inputs": [\n      "brief"\n    ]' in out
    # Both saved results were read back by flow id, not by `latest`.
    assert out.count(CLAUDE_SAID) >= 2 and out.count(CODEX_SAID) >= 1

    workspace = Path(marker.read_text().strip())
    assert " " in workspace.name, "the workspace path should contain a space"

    store = FilesystemStore(workspace / ".atelier")
    triage = store.list_flows("triage")
    panel = store.list_flows("panel")
    assert len(triage) == 1 and len(panel) == 1
    assert store.read_progress(triage[0]).status.value == "completed"
    assert store.read_progress(panel[0]).status.value == "completed"

    # The chain handed work along; the panel's reviewers did not see each other.
    assert store.read_outputs(triage[0]) == {
        "step_1": CLAUDE_SAID, "step_2": CODEX_SAID
    }
    assert store.read_outputs(panel[0]) == {
        "step_1": CLAUDE_SAID, "step_2": CODEX_SAID, "synthesis": CLAUDE_SAID
    }

    codex_prompts = _prompts(records["codex"])
    assert len(codex_prompts) == 2
    chain_review = next(p for p in codex_prompts if "RESULT FROM" in p)
    assert "--- BEGIN RESULT FROM step_1 (harness:claude-code) ---" in chain_review
    assert CLAUDE_SAID in chain_review
    panel_review = next(p for p in codex_prompts if "RESULT FROM" not in p)
    assert "security risks" in panel_review and CLAUDE_SAID not in panel_review

    synthesis = [p for p in _prompts(records["claude"]) if "RESULT FROM" in p]
    assert len(synthesis) == 1
    assert "--- BEGIN RESULT FROM step_1 (harness:claude-code) ---" in synthesis[0]
    assert "--- BEGIN RESULT FROM step_2 (harness:codex) ---" in synthesis[0]
    assert CODEX_SAID in synthesis[0]


@pytest.mark.parametrize("guide_env", [True], indirect=True)
def test_a_failed_prerequisite_stops_before_anything_runs(guide_env, tmp_path):
    """A broken `harness check` ends the script; nothing is composed or run."""
    env, marker, records = guide_env
    run = run_script(
        _guide_script(), tmp_path / "guide.sh", tmp_path, env, timeout=120
    )
    assert run.returncode == 1, f"stdout:\n{run.stdout}\nstderr:\n{run.stderr}"
    assert "INJECTED agent unreachable" in run.stderr

    # Nothing past the prerequisite block executed.
    for forbidden in ("composed", "flow_id:", CLAUDE_SAID, CODEX_SAID):
        assert forbidden not in run.stdout, run.stdout
    assert not marker.exists(), "the workspace block ran after a failed check"
    assert _prompts(records["claude"]) == [] and _prompts(records["codex"]) == []


# ------------------------------------- the README quickstart, as a real shell

_README = _REPO_ROOT / "README.md"
_README_SECTION = "### Your first multi-agent workflow: chain or panel"
GLOBAL_RAN = "GLOBAL_RECIPE_RAN"


def _readme_script() -> str:
    """Return the README quickstart's bash block, exactly as published.

    :returns: one shell script, with no error handling the README omits.
    """
    body = _README.read_text(encoding="utf-8").split(_README_SECTION, 1)[1]
    body = body.split("\n### ", 1)[0]
    blocks = [m.group(1) for m in _BASH_BLOCK.finditer(body)]
    assert len(blocks) == 1, f"expected 1 bash block in the section, got {len(blocks)}"
    assert "atelier compose triage" in blocks[0]
    # Deliberately no `set -e`: the published block gates itself, and its own
    # final status is the status this test reads.
    return blocks[0]


def _global_triage(env: dict, marker: Path) -> None:
    """Install a usable global `triage` conduit under the quickstart's name.

    Its one task is called `step_2` and it declares a `brief` input, so every
    command after `compose` in the README block would succeed against it.
    Without the gating, that fallback is what masks the failure.

    :param env: the child environment, whose global dir receives the conduit.
    :param marker: file the recipe writes when it runs.
    """
    path = Path(env["ATELIER_GLOBAL_ATELIER_DIR"]) / "conduits" / "triage"
    path.mkdir(parents=True)
    (path / "conduit.yaml").write_text(
        "name: triage\n"
        "description: An unrelated recipe that already owns this name\n"
        "inputs:\n"
        "  brief:\n"
        "    description: whatever the caller passes\n"
        "tasks:\n"
        "  - name: step_2\n"
        "    description: prove the wrong workflow ran\n"
        f'    task: "echo {GLOBAL_RAN} | tee \'{to_bash_path(marker)}\'"\n'
        "    tool: tool:bash\n"
        "    depends_on: []\n",
        encoding="utf-8",
    )


def _run_readme(env: dict, tmp_path: Path):
    """Execute the quickstart block in a workspace whose path has a space.

    :param env: child environment from the fixture.
    :param tmp_path: pytest temp directory fixture.
    :returns: ``(completed process, workspace path)``.
    """
    workspace = tmp_path / "quickstart demo"
    workspace.mkdir()
    return (
        run_script(_readme_script(), tmp_path / "readme.sh", workspace, env, timeout=180),
        workspace,
    )


def test_the_readme_quickstart_runs_and_claims_only_what_happens(guide_env, tmp_path):
    """The published block composes, checks, runs and reads the result back."""
    env, _, records = guide_env
    run, workspace = _run_readme(env, tmp_path)
    assert run.returncode == 0, f"stdout:\n{run.stdout}\nstderr:\n{run.stderr}"
    assert CODEX_SAID in run.stdout

    store = FilesystemStore(workspace / ".atelier")
    flows = store.list_flows("triage")
    assert len(flows) == 1
    assert store.read_progress(flows[0]).status.value == "completed"
    assert store.read_outputs(flows[0]) == {"step_1": CLAUDE_SAID, "step_2": CODEX_SAID}
    assert len(_prompts(records["claude"])) == 1 and len(_prompts(records["codex"])) == 1


@pytest.mark.parametrize("guide_env", [True], indirect=True)
def test_the_readme_quickstart_stops_on_a_failed_prerequisite(guide_env, tmp_path):
    """A broken `harness check` ends the block before anything is composed."""
    env, _, records = guide_env
    run, workspace = _run_readme(env, tmp_path)
    assert run.returncode == 1, f"stdout:\n{run.stdout}\nstderr:\n{run.stderr}"
    assert "INJECTED agent unreachable" in run.stderr
    for forbidden in ("composed", CLAUDE_SAID, CODEX_SAID):
        assert forbidden not in run.stdout, run.stdout
    assert not (workspace / ".atelier" / "conduits" / "triage").exists()
    assert list((workspace / ".atelier").glob("flows/*")) == []


def test_the_readme_quickstart_stops_when_the_name_is_taken(guide_env, tmp_path):
    """A same-named global recipe cannot be run in place of the composition."""
    env, _, records = guide_env
    marker = tmp_path / "global-ran"
    _global_triage(env, marker)
    run, workspace = _run_readme(env, tmp_path)
    assert run.returncode == 1, f"stdout:\n{run.stdout}\nstderr:\n{run.stderr}"
    assert "already exists" in run.stdout
    # The unrelated recipe was never checked, run or read back.
    assert not marker.exists() and GLOBAL_RAN not in run.stdout
    assert list((workspace / ".atelier").glob("flows/*")) == []
    assert _prompts(records["claude"]) == [] and _prompts(records["codex"]) == []
