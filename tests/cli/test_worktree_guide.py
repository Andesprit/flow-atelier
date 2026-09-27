"""Runs `docs/parallel-agents-in-separate-checkouts.md` for real.

The published blocks are executed verbatim, in order, in one shell with stdin
closed and a workspace path containing a space. Only the agents are faked:
`claude-code` and `codex` are two scripted ACP writers that rewrite
``NOTES.md`` in whatever directory they were started in, and `gemini` is a
reviewer that only replies. All three record every prompt they are handed.

The second test breaks the guide's own prerequisite check and asserts that
nothing downstream of it ran — the `|| exit 1` on every line is the promise
being tested.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from flow_atelier.services.executor.bash import to_bash_path
from tests._shell import record_path, run_script, write_shim

_REPO_ROOT = Path(__file__).resolve().parents[2]
_GUIDE = _REPO_ROOT / "docs" / "parallel-agents-in-separate-checkouts.md"
_FAKE_AGENT = _REPO_ROOT / "tests" / "fixtures" / "fake_acp_agent.py"
_BASH_BLOCK = re.compile(r"^```bash\n(.*?)^```$", re.MULTILINE | re.DOTALL)
# prerequisites, repository, compose, plan, run, read back, source untouched,
# broken run, resume, again.
_EXPECTED_BLOCKS = 10

GEMINI_SAID = "GEMINI_REPLY: keep the codex version, it names the escalation."
CLAUDE_EDIT = "Page the on-call. Runbook: /ops/billing.\n"
CODEX_EDIT = "If billing overruns, see /ops/billing and page the on-call.\n"
CLAUDE_SAID = "CLAUDE_REPLY: rewrote it around the runbook."
CODEX_SAID = "CODEX_REPLY: rewrote it around the escalation path."


def _guide_script() -> str:
    """Return the guide's bash blocks, concatenated in document order.

    No ``set -e``: the published blocks gate themselves, and one of them runs a
    command that is *meant* to fail, so an ambient abort would hide the
    recovery the guide is about.

    :returns: one shell script.
    """
    blocks = [m.group(1) for m in _BASH_BLOCK.finditer(_GUIDE.read_text(encoding="utf-8"))]
    assert len(blocks) == _EXPECTED_BLOCKS, (
        f"expected {_EXPECTED_BLOCKS} bash blocks in {_GUIDE.name}, got {len(blocks)}"
    )
    body = "\n".join(blocks)
    for required in (
        "atelier plan candidates --worktree step_1 --worktree step_2",
        "atelier run candidates --worktree step_1 --worktree step_2",
        "atelier run --resume",
        "atelier run --again",
    ):
        assert required in body, f"the guide no longer runs `{required}`"
    tail = (
        "\nstatus=$?\n"
        + record_path("work_dir", "WORKSPACE_MARKER")
        + 'printf "%s\\n" "$flow_id" "$broken_id" "$new_id" > "$IDS_MARKER"\n'
        + "exit $status\n"
    )
    return body + tail


def _agent_env(record_dir: Path, text: str, edit: str | None = None) -> str:
    """Build a launch-command override for one scripted fake agent.

    :param record_dir: directory the agent writes its per-process prompt log to.
    :param text: what the agent replies.
    :param edit: the contents it writes to ``NOTES.md`` in its own directory;
        ``None`` for a reviewer that only reads and replies.
    :returns: the JSON argv for an ``ATELIER_*_LAUNCH_CMD`` variable.
    """
    record_dir.mkdir(parents=True, exist_ok=True)
    turn: dict[str, object] = {"chunks": [text]}
    if edit is not None:
        turn["write"] = {"path": "NOTES.md", "text": edit}
    script = {"turns": [turn], "record_path": str(record_dir)}
    return json.dumps([sys.executable, str(_FAKE_AGENT), "--script", json.dumps(script)])


@pytest.fixture
def guide_env(tmp_path, request):
    """An isolated environment whose `atelier` is this checkout's CLI.

    :param tmp_path: pytest temp directory fixture.
    :param request: pytest request; a truthy ``param`` breaks `harness check`.
    :yields: ``(env, marker, ids, records)`` — the child environment, the file
        the script writes its workspace path into, the file it writes the three
        flow ids into, and the prompt-log directories.
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
    ids = tmp_path / "flow-ids"
    records = {
        name: tmp_path / f"{name}-prompts"
        for name in ("claude", "codex", "gemini")
    }

    env = {k: v for k, v in os.environ.items() if not k.startswith("ATELIER_")}
    env["ATELIER_GLOBAL_ATELIER_DIR"] = str(tmp_path / "global")
    env["ATELIER_NO_UPDATE_CHECK"] = "1"
    env["ATELIER_CLAUDE_LAUNCH_CMD"] = _agent_env(
        records["claude"], CLAUDE_SAID, CLAUDE_EDIT
    )
    env["ATELIER_CODEX_LAUNCH_CMD"] = _agent_env(records["codex"], CODEX_SAID, CODEX_EDIT)
    env["ATELIER_HARNESSES"] = json.dumps(
        {"gemini": json.loads(_agent_env(records["gemini"], GEMINI_SAID))}
    )
    env["PATH"] = f"{bin_dir}{os.pathsep}{env.get('PATH', '')}"
    env["WORKSPACE_MARKER"] = to_bash_path(marker)
    env["IDS_MARKER"] = to_bash_path(ids)
    yield env, marker, ids, records
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


def _git(*args: str, cwd) -> str:
    """Run one git command in ``cwd``, asserting it succeeded.

    :param args: the arguments after ``git``.
    :param cwd: the directory to run in.
    :returns: stdout, stripped.
    """
    done = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True)
    assert done.returncode == 0, f"git {args}: {done.stderr}"
    return done.stdout.strip()


def test_the_published_guide_runs_and_separates_the_two_agents(tmp_path, guide_env):
    env, marker, ids, records = guide_env
    done = run_script(
        _guide_script(), tmp_path / "guide.sh", tmp_path, env, timeout=600
    )
    assert done.returncode == 0, done.stdout + done.stderr

    # Resolved: on macOS `mktemp -d` hands back /var/..., while the run
    # records the real /private/var/... path.
    work = Path(marker.read_text().strip()).resolve()
    flow_id, broken_id, new_id = ids.read_text().split()
    assert flow_id and broken_id and new_id
    assert len({flow_id, broken_id, new_id}) == 3

    # Two agents, one filename, two surviving results.
    spaces = work / ".atelier" / "workspaces" / flow_id
    assert (spaces / "step_1" / "NOTES.md").read_text(encoding="utf-8") == CLAUDE_EDIT
    assert (spaces / "step_2" / "NOTES.md").read_text(encoding="utf-8") == CODEX_EDIT

    # The user's own checkout never moved.
    base = _git("rev-parse", "HEAD", cwd=work)
    assert "nightly billing job" in (work / "NOTES.md").read_text(encoding="utf-8")
    assert _git("status", "--porcelain", "--untracked-files=no", cwd=work) == ""
    assert _git("rev-parse", "HEAD", cwd=spaces / "step_1") == base

    # The synthesis was told where each candidate lives, without the recipe
    # naming a single path.
    merged = [p for p in _prompts(records["gemini"]) if "WORKSPACE PROVENANCE" in p]
    assert merged, _prompts(records["gemini"])
    assert str(spaces / "step_1") in merged[0]
    assert str(spaces / "step_2") in merged[0]

    # The resume went back to the same directory and kept what was in it.
    draft = work / ".atelier" / "workspaces" / broken_id / "synthesis" / "DRAFT.md"
    assert draft.read_text(encoding="utf-8") == "half a thought\n"

    # The fresh run built its own checkouts and left the old ones alone.
    assert (work / ".atelier" / "workspaces" / new_id / "step_1").is_dir()
    assert (work / ".atelier" / "workspaces" / broken_id / "step_1").is_dir()

    # Three runs, and the recipe on disk is byte-identical.
    assert "byte-identical after three runs" in done.stdout


@pytest.mark.parametrize("guide_env", [True], indirect=True)
def test_a_failed_prerequisite_stops_before_anything_is_created(tmp_path, guide_env):
    env, marker, _ids, records = guide_env
    done = run_script(
        _guide_script(), tmp_path / "guide.sh", tmp_path, env, timeout=600
    )
    assert done.returncode == 1, done.stdout + done.stderr
    assert "INJECTED agent unreachable" in done.stderr
    assert not marker.exists()
    assert _prompts(records["claude"]) == []
    assert _prompts(records["codex"]) == []
    assert _prompts(records["gemini"]) == []
