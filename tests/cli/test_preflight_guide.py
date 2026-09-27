"""Runs `docs/checking-the-team-before-a-run.md` for real, so it cannot drift.

The published blocks are executed verbatim, in order, in one shell with stdin
closed and a workspace path containing a space. No safeguard is added that the
guide does not print: the `|| exit 1` and the `&&` in the published text are
the promises under test.

Only the agents are faked. The two `ATELIER_*_LAUNCH_CMD` overrides point
`claude-code` and `codex` at the scripted ACP agent, and the fixture can log
one of them out the way a real expired login does. What that establishes is
the gate, the diagnostic and the saved run — not live authentication.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import sys
from pathlib import Path

import pytest

from flow_atelier.services.store.filesystem import FilesystemStore
from tests._shell import run_script, write_shim

_REPO_ROOT = Path(__file__).resolve().parents[2]
_GUIDE = _REPO_ROOT / "docs" / "checking-the-team-before-a-run.md"
_README = _REPO_ROOT / "README.md"
_FAKE_AGENT = _REPO_ROOT / "tests" / "fixtures" / "fake_acp_agent.py"
_BASH_BLOCK = re.compile(r"^```bash\n(.*?)^```$", re.MULTILINE | re.DOTALL)
# version, workspace, compose, check + probe, the gated run, read back.
_EXPECTED_BLOCKS = 6
# Index of the block that gates a run on a probe; the failure test runs that
# one line on its own, after the blocks before it have set the workspace up.
_GATE_INDEX = 4

CLAUDE_SAID = "CLAUDE_REPLY: move it onto the scheduler."
CODEX_SAID = "CODEX_REPLY: the retry window is wrong."


def _blocks() -> list[str]:
    """Return the guide's bash blocks, in document order.

    :returns: one entry per published block.
    """
    found = [m.group(1) for m in _BASH_BLOCK.finditer(_GUIDE.read_text(encoding="utf-8"))]
    assert len(found) == _EXPECTED_BLOCKS, (
        f"expected {_EXPECTED_BLOCKS} bash blocks in {_GUIDE.name}, got {len(found)}"
    )
    for required in (
        "atelier check triage --probe --timeout 60",
        "atelier check triage --probe --agent step_2=claude-code",
        "atelier run triage --agent step_2=claude-code",
    ):
        assert any(required in b for b in found), f"the guide no longer runs `{required}`"
    return found


def _script(upto: int | None = None) -> str:
    """Return the guide's blocks as one shell script.

    No ``set -e``: the published blocks gate themselves, and that self-gating
    is exactly what these tests exercise.

    :param upto: stop after this many blocks; ``None`` runs all of them.
    :returns: one shell script.
    """
    return "\n".join(_blocks()[:upto])


def _agent_argv(record_dir: Path, text: str, *, logged_out: bool = False) -> str:
    """Build a launch-command override for one scripted fake agent.

    :param record_dir: directory the agent writes its logs to.
    :param text: what the agent replies.
    :param logged_out: when true, opening a session fails the way a logout does.
    :returns: the JSON argv for an ``ATELIER_*_LAUNCH_CMD`` variable.
    """
    record_dir.mkdir(parents=True, exist_ok=True)
    script: dict[str, object] = {
        "turns": [{"chunks": [text]}],
        "record_path": str(record_dir),
    }
    if logged_out:
        script["fail_session"] = "not logged in"
        script["auth_methods"] = [{"id": "oauth"}]
    return json.dumps([sys.executable, str(_FAKE_AGENT), "--script", json.dumps(script)])


class Guide:
    """The guide's environment, with one knob: which agent is logged out.

    :param tmp_path: pytest temp directory the workspace and logs live under.
    """

    def __init__(self, tmp_path: Path) -> None:
        self.tmp_path = tmp_path
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        write_shim(bin_dir)
        self.records = {n: tmp_path / f"{n}-log" for n in ("claude", "codex")}
        self.env = {k: v for k, v in os.environ.items() if not k.startswith("ATELIER_")}
        self.env["ATELIER_GLOBAL_ATELIER_DIR"] = str(tmp_path / "global")
        self.env["ATELIER_NO_UPDATE_CHECK"] = "1"
        self.env["PATH"] = f"{bin_dir}{os.pathsep}{self.env.get('PATH', '')}"
        self.workspace: Path | None = None
        self.log_out(None)

    def log_out(self, who: str | None) -> None:
        """Point both agents at the fake, logging one of them out.

        :param who: ``claude``, ``codex`` or ``None`` for a working pair.
        """
        self.env["ATELIER_CLAUDE_LAUNCH_CMD"] = _agent_argv(
            self.records["claude"], CLAUDE_SAID, logged_out=who == "claude"
        )
        self.env["ATELIER_CODEX_LAUNCH_CMD"] = _agent_argv(
            self.records["codex"], CODEX_SAID, logged_out=who == "codex"
        )

    def run(self, body: str, name: str, cwd: Path, timeout: float = 240):
        """Execute one script under the resolved bash.

        :param body: the script text.
        :param name: file name to write it to.
        :param cwd: working directory for the shell.
        :param timeout: seconds before the shell is killed.
        :returns: the completed process.
        """
        done = run_script(body, self.tmp_path / name, cwd, self.env, timeout)
        # The guide's own `echo "working in: $work_dir"` is how the workspace
        # is found, so a script that dies at a gate still names where it got
        # to — a marker written after the last block never would.
        for line in done.stdout.splitlines():
            if line.startswith("working in: "):
                self.workspace = Path(line.removeprefix("working in: ").strip())
        return done

    def store(self) -> FilesystemStore:
        """Return the workspace's store.

        :returns: the filesystem store rooted at the workspace.
        """
        assert self.workspace is not None
        return FilesystemStore(self.workspace / ".atelier")

    def prompts(self, who: str) -> list[str]:
        """Return every prompt one agent received.

        :param who: ``claude`` or ``codex``.
        :returns: one joined text block per prompt call.
        """
        return [
            "\n".join(block.get("text", "") for block in json.loads(line))
            for log in sorted(self.records[who].glob("*.jsonl"))
            for line in log.read_text(encoding="utf-8").splitlines()
        ]


@pytest.fixture
def guide(tmp_path):
    """A :class:`Guide` whose workspace is cleaned up afterwards.

    :param tmp_path: pytest temp directory fixture.
    :yields: the guide environment.
    """
    g = Guide(tmp_path)
    yield g
    # `mktemp -d` puts the workspace outside tmp_path and the guide never
    # deletes it, so the test is what cleans up.
    if g.workspace is not None:
        shutil.rmtree(g.workspace.parent, ignore_errors=True)


def _flat(text: str) -> str:
    """Return ``text`` with console line wrapping collapsed.

    :param text: captured output.
    :returns: the same words, single-spaced.
    """
    return " ".join(text.split())


def test_readme_points_at_a_guide_that_exists():
    """The discovery section beside the compose quickstart resolves here."""
    assert "(docs/checking-the-team-before-a-run.md)" in _README.read_text(
        encoding="utf-8"
    )
    assert _GUIDE.is_file()


def test_the_guide_runs_end_to_end_and_claims_only_what_happens(guide):
    """Compose, check, probe, gate, run, read back — exactly as published."""
    run = guide.run(_script(), "guide.sh", guide.tmp_path)
    assert run.returncode == 0, f"stdout:\n{run.stdout}\nstderr:\n{run.stderr}"
    flat = _flat(run.stdout)

    assert " " in guide.workspace.name, "the workspace path should contain a space"
    # The probe reported both agents and said what it does not prove.
    assert "2 agent task(s) on 2 configuration(s)" in flat
    assert "triage.step_1" in flat and "triage.step_2" in flat
    assert "startup only" in flat

    # The gated run happened, and its result came back by exact flow id.
    store = guide.store()
    flows = store.list_flows("triage")
    assert len(flows) == 1
    assert store.read_progress(flows[0]).status.value == "completed"
    assert flows[0] in run.stdout
    assert CLAUDE_SAID in run.stdout

    # `--agent step_2=claude-code` moved the review; the file did not change.
    # Only the reassignment is recorded; step_1's agent is the recipe's own.
    assert store.read_progress(flows[0]).task_agents == {
        "step_2": "harness:claude-code"
    }
    recipe = (guide.workspace / ".atelier" / "conduits" / "triage" / "conduit.yaml")
    assert "tool: harness:codex" in recipe.read_text(encoding="utf-8")
    # Codex opened sessions for the probes and was never prompted.
    assert guide.prompts("codex") == []
    assert len(guide.prompts("claude")) == 2


def test_a_logged_out_agent_stops_the_guide_before_any_work(guide):
    """The static check still passes; the probe fails and nothing runs."""
    guide.log_out("codex")
    run = guide.run(_script(), "guide.sh", guide.tmp_path)
    assert run.returncode == 1, f"stdout:\n{run.stdout}\nstderr:\n{run.stderr}"
    flat = _flat(run.stdout)

    # Reading the files says OK; asking the agents does not.
    assert flat.count("triage [project] — OK") == 2
    assert "not usable" in flat and "triage.step_2" in flat
    assert "log in with the agent's own CLI" in flat

    assert guide.store().list_flows("triage") == []
    assert guide.prompts("claude") == [] and guide.prompts("codex") == []


def test_the_gate_creates_no_flow_when_the_probe_fails_and_works_once_fixed(guide):
    """The published `check --probe ... && run ...` line, broken then fixed."""
    prepared = guide.run(_script(_GATE_INDEX), "prepare.sh", guide.tmp_path)
    assert prepared.returncode == 0, prepared.stdout + prepared.stderr
    workspace = guide.workspace
    gate = _blocks()[_GATE_INDEX]

    guide.log_out("claude")
    blocked = guide.run(gate, "gate.sh", workspace)
    assert blocked.returncode == 1, blocked.stdout + blocked.stderr
    assert "not usable" in _flat(blocked.stdout)
    # The run half of the `&&` never happened: no preparation, no flow.
    assert FilesystemStore(workspace / ".atelier").list_flows("triage") == []
    assert guide.prompts("claude") == []

    guide.log_out(None)
    passed = guide.run(gate, "gate.sh", workspace)
    assert passed.returncode == 0, passed.stdout + passed.stderr

    store = FilesystemStore(workspace / ".atelier")
    flows = store.list_flows("triage")
    assert len(flows) == 1
    assert store.read_progress(flows[0]).status.value == "completed"

    # The exact flow id the run printed is the one the output comes back by.
    printed = next(
        line.split(":", 1)[1].strip()
        for line in passed.stdout.splitlines()
        if line.startswith("flow_id:")
    )
    assert printed == flows[0]
    read_back = guide.run(
        f'atelier outputs "{printed}" --task step_2 || exit 1\n', "read.sh", workspace
    )
    assert read_back.returncode == 0, read_back.stdout + read_back.stderr
    assert CLAUDE_SAID in read_back.stdout
