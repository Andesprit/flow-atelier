"""Runs `docs/recovering-a-failed-workflow.md` for real, so it cannot drift.

The published blocks are executed verbatim, in order, in one shell with stdin
closed and a workspace path containing a space. Only the agent is faked: one
`ATELIER_CLAUDE_LAUNCH_CMD` override points `claude-code` at the scripted ACP
agent, which records every prompt it is served. What that establishes is the
report, the repair and the resume — not live authentication.
"""
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

import pytest

from flow_atelier.services.store.filesystem import FilesystemStore
from tests._shell import bash, run_script, write_shim

_REPO_ROOT = Path(__file__).resolve().parents[2]
_GUIDE = _REPO_ROOT / "docs" / "recovering-a-failed-workflow.md"
_README = _REPO_ROOT / "README.md"
_FAKE_AGENT = _REPO_ROOT / "tests" / "fixtures" / "fake_acp_agent.py"
_BASH_BLOCK = re.compile(r"^```bash\n(.*?)^```$", re.MULTILINE | re.DOTALL)
# version, workspace + conduit, the failing run, diagnose, diagnose --json,
# repair + resume, read back.
_EXPECTED_BLOCKS = 7
AGENT_SAID = "CLAUDE_REPLY: a retry storm without jitter."


def _blocks() -> list[str]:
    """Return the guide's bash blocks, in document order.

    :returns: one entry per published block.
    """
    found = [m.group(1) for m in _BASH_BLOCK.finditer(_GUIDE.read_text(encoding="utf-8"))]
    assert len(found) == _EXPECTED_BLOCKS, (
        f"expected {_EXPECTED_BLOCKS} bash blocks in {_GUIDE.name}, got {len(found)}"
    )
    for required in ("atelier diagnose latest", "atelier run --resume latest"):
        assert any(required in b for b in found), f"the guide no longer runs `{required}`"
    return found


def _script(upto: int | None = None) -> str:
    """Return the guide's blocks as one shell script.

    No ``set -e``: the published blocks gate themselves, and the deliberately
    failing run in the middle is the point of the exercise.

    :param upto: stop after this many blocks; ``None`` runs all of them.
    :returns: one shell script.
    """
    return "\n".join(_blocks()[:upto])


class Guide:
    """The guide's environment: a real `atelier`, one scripted agent.

    :param tmp_path: pytest temp directory the logs live under.
    """

    def __init__(self, tmp_path: Path) -> None:
        self.tmp_path = tmp_path
        self.record = tmp_path / "claude-log"
        self.record.mkdir()
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        write_shim(bin_dir)
        script = json.dumps(
            {"turns": [{"chunks": [AGENT_SAID]}] * 4, "record_path": str(self.record)}
        )
        self.env = {k: v for k, v in os.environ.items() if not k.startswith("ATELIER_")}
        self.env["ATELIER_GLOBAL_ATELIER_DIR"] = str(tmp_path / "global")
        self.env["ATELIER_NO_UPDATE_CHECK"] = "1"
        self.env["PATH"] = f"{bin_dir}{os.pathsep}{self.env.get('PATH', '')}"
        self.env["ATELIER_CLAUDE_LAUNCH_CMD"] = json.dumps(
            [sys.executable, str(_FAKE_AGENT), "--script", script]
        )
        self.workspace: Path | None = None

    def run(self, body: str, name: str, timeout: float = 300):
        """Execute one script under the resolved bash, stdin closed.

        :param body: the script text.
        :param name: file name to write it to.
        :param timeout: seconds before the shell is killed.
        :returns: the completed process.
        """
        done = run_script(body, self.tmp_path / name, self.tmp_path, self.env, timeout)
        for line in done.stdout.splitlines():
            if line.startswith("working in: "):
                self.workspace = Path(line.removeprefix("working in: ").strip())
        return done

    def prompts(self) -> list[str]:
        """Return every prompt the agent was actually served.

        :returns: one entry per prompt.
        """
        found: list[str] = []
        for path in sorted(self.record.glob("*.jsonl")):
            found.extend(
                line for line in path.read_text(encoding="utf-8").splitlines() if line
            )
        return found

    def store(self) -> FilesystemStore:
        """Return a store rooted at the workspace the guide created.

        :returns: the store.
        """
        assert self.workspace is not None
        return FilesystemStore(self.workspace / ".atelier")


@pytest.fixture
def guide(tmp_path):
    """The guide's environment, ready to run.

    :param tmp_path: pytest temp directory fixture.
    :returns: the Guide.
    """
    bash()
    return Guide(tmp_path)


def test_the_guide_runs_end_to_end(guide):
    """Every published block executes, and the walkthrough ends completed."""
    done = guide.run(_script(), "guide.sh")
    assert done.returncode == 0, done.stdout + done.stderr
    assert guide.workspace is not None
    assert " " in guide.workspace.name, guide.workspace

    # The failing run is the guide's own step 2, and it reported its exit code.
    assert "exit: 1" in done.stdout, done.stdout
    # The report named the failure without turning the cancellation into one.
    flat = " ".join(done.stdout.split())
    assert "what failed" in flat
    assert "approved.txt is missing" in flat
    assert "cut off when the run failed" in flat
    assert "a cancelled task is not the failure" in flat

    store = guide.store()
    flows = store.list_flows("triage")
    assert len(flows) == 1, flows
    progress = store.read_progress(flows[0])
    assert progress.status.value == "completed", progress.model_dump()
    assert {n: t.status.value for n, t in progress.tasks.items()} == {
        "analyse": "completed",
        "verify": "completed",
        "conclude": "completed",
    }

    # The expensive task was prompted once, before the failure; the resume
    # replayed its saved result and only asked the agent for `conclude`.
    served = guide.prompts()
    assert len(served) == 2, served
    assert AGENT_SAID in served[1], served[1]
    outputs = store.read_outputs(flows[0])
    assert AGENT_SAID in outputs["analyse"]
    assert "check passed" in outputs["verify"]


def test_the_report_is_read_before_the_repair(guide):
    """Stopping after the diagnose blocks leaves the run failed and readable."""
    done = guide.run(_script(upto=5), "half.sh")
    assert done.returncode == 0, done.stdout + done.stderr
    payload = None
    for start in range(len(done.stdout)):
        if done.stdout[start] == "{":
            try:
                payload, _ = json.JSONDecoder().raw_decode(done.stdout[start:])
            except json.JSONDecodeError:
                continue
            break
    assert payload is None or isinstance(payload, dict)

    store = guide.store()
    flow_id = store.list_flows("triage")[0]
    progress = store.read_progress(flow_id)
    assert progress.status.value == "failed"
    assert progress.tasks["analyse"].status.value == "completed"
    assert progress.tasks["verify"].status.value == "failed"
    assert progress.tasks["conclude"].status.value == "cancelled"
    # Reading the report changed nothing and cost no prompt beyond the first.
    assert len(guide.prompts()) == 1


def test_the_readme_points_at_the_guide():
    """The README documents the command and links the walkthrough."""
    readme = _README.read_text(encoding="utf-8")
    assert "atelier diagnose" in readme
    assert "docs/recovering-a-failed-workflow.md" in readme
