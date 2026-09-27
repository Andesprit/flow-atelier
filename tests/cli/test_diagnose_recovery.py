"""The failure -> diagnose -> repair -> resume path, driven as real processes.

Every `atelier` here is a separate process started through a shim on PATH, in a
workspace whose path contains a space, with stdin closed. The resume is not
composed by the test: it is the command the report printed, executed verbatim.

Only the agents are faked. Two scripted ACP agents record every prompt they are
served, so "the completed worker was not run again" is read back from the agent
itself rather than from the report that claims it.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from tests._shell import bash, run_script, write_shim

_REPO_ROOT = Path(__file__).resolve().parents[2]
_FAKE_AGENT = _REPO_ROOT / "tests" / "fixtures" / "fake_acp_agent.py"

CLAUDE_SAID = "CLAUDE_REPLY: the retry window is the cause."
CODEX_SAID = "CODEX_REPLY: both findings agree."

CONDUIT_YAML = """
name: triage
description: two workers and a synthesis
tasks:
  - name: worker_a
    description: the agent that answers first
    tool: harness:codex
    task: "read the incident and name the cause"
  - name: worker_b
    description: the local check that breaks
    tool: tool:bash
    depends_on: [worker_a]
    task: |
      if [ -f fixed.txt ]; then
        echo "worker B check passed"
      else
        echo "fixed.txt is missing" >&2
        exit 4
      fi
  - name: synthesis
    description: needs both
    tool: harness:codex
    task: "combine {{worker_a.output}} and {{worker_b.output}}"
    depends_on: [worker_a, worker_b]
"""


def _agent_argv(record_dir: Path, text: str, *, logged_out: bool = False) -> str:
    """Build an ``ATELIER_*_LAUNCH_CMD`` value for one scripted fake agent.

    :param record_dir: directory the agent appends its prompt log to.
    :param text: what the agent replies.
    :param logged_out: when true, opening a session fails the way a logout does.
    :returns: the JSON argv.
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


class Fixture:
    """A real workspace, a real `atelier` on PATH, and two scripted agents.

    :param tmp_path: pytest temp directory everything lives under.
    """

    def __init__(self, tmp_path: Path) -> None:
        self.tmp_path = tmp_path
        # A space in the path: every command the report prints has to survive it.
        self.workspace = tmp_path / "incident work"
        (self.workspace / ".atelier" / "conduits" / "triage").mkdir(parents=True)
        (
            self.workspace / ".atelier" / "conduits" / "triage" / "conduit.yaml"
        ).write_text(CONDUIT_YAML, newline="\n")
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        write_shim(bin_dir)
        self.records = {n: tmp_path / f"{n}-log" for n in ("claude", "codex")}
        self.env = {k: v for k, v in os.environ.items() if not k.startswith("ATELIER_")}
        self.env["ATELIER_GLOBAL_ATELIER_DIR"] = str(tmp_path / "global")
        self.env["ATELIER_NO_UPDATE_CHECK"] = "1"
        self.env["PATH"] = f"{bin_dir}{os.pathsep}{self.env.get('PATH', '')}"
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

    def sh(self, body: str, name: str, timeout: float = 240):
        """Run a shell script in the workspace, stdin closed.

        :param body: the script text.
        :param name: file name to write it to.
        :param timeout: seconds before the shell is killed.
        :returns: the completed process.
        """
        return run_script(body, self.tmp_path / name, self.workspace, self.env, timeout)

    def run_verbatim(self, command: str, timeout: float = 240):
        """Execute one printed command exactly as it was printed.

        :param command: the command line from the report.
        :param timeout: seconds before the shell is killed.
        :returns: the completed process.
        """
        return subprocess.run(
            [bash(), "-c", command],
            cwd=self.workspace,
            env=self.env,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )

    def prompts(self, who: str) -> list[str]:
        """Return every prompt one agent was actually served.

        :param who: ``claude`` or ``codex``.
        :returns: one entry per prompt, oldest file first.
        """
        found: list[str] = []
        for path in sorted(self.records[who].glob("*.jsonl")):
            found.extend(
                line for line in path.read_text(encoding="utf-8").splitlines() if line
            )
        return found

    def diagnose(self, flow_id: str) -> dict:
        """Read the report for ``flow_id`` as JSON, from its own process.

        :param flow_id: the flow id to diagnose.
        :returns: the parsed report.
        """
        done = self.sh(f"atelier diagnose {flow_id} --json\n", "diagnose.sh")
        assert done.returncode == 0, done.stdout + done.stderr
        return json.loads(done.stdout)


@pytest.fixture
def fixture(tmp_path):
    """A prepared workspace with both agents working.

    :param tmp_path: pytest temp directory fixture.
    :returns: the fixture.
    """
    bash()  # skip early and clearly when there is no POSIX shell
    return Fixture(tmp_path)


def _flow_id(stdout: str) -> str:
    """Pull the flow id a run printed out of its output.

    :param stdout: the run's stdout.
    :returns: the flow id.
    """
    for line in stdout.splitlines():
        if line.startswith("flow_id: "):
            return line.removeprefix("flow_id: ").strip()
    raise AssertionError(f"no flow_id in output:\n{stdout}")


def _resume_command(report: dict) -> str:
    """Return the single resume command the report suggests.

    :param report: the parsed report.
    :returns: the command line.
    """
    found = [
        step["command"]
        for step in report["next_steps"]
        if step["command"] and "--resume" in step["command"]
    ]
    assert len(found) == 1, report["next_steps"]
    return found[0]


def test_diagnose_then_resume_keeps_the_completed_agent_work(fixture):
    """The whole path: one agent finishes, a task breaks, the report drives the fix."""
    first = fixture.sh(
        "atelier run triage --agent worker_a=claude-code\n", "run.sh"
    )
    assert first.returncode != 0, first.stdout
    flow_id = _flow_id(first.stdout)

    report = fixture.diagnose(flow_id)
    # The failure is the task that failed, and the cancellation is not a second one.
    assert [f["task"] for f in report["failures"]] == ["worker_b"]
    assert report["failures"][0]["exit_code"] == 4
    assert "fixed.txt is missing" in report["failures"][0]["excerpt"]["text"]
    assert report["not_run"] == []
    assert [t["task"] for t in report["cancelled"]] == ["synthesis"]
    assert report["cancelled"][0]["status"] == "cancelled"
    # It never started, and nothing saved proves that, so the report says so.
    assert report["cancelled"][0]["execution"] == "unknown"
    assert report["observed"] == {
        "state": "failed",
        "certainty": "proven",
        "note": report["observed"]["note"],
    }

    # The completed work, the agent that produced it, and the fact that the
    # recipe names a different one.
    kept = [k for k in report["kept"] if k["task"] == "worker_a"]
    assert len(kept) == 1
    assert kept[0]["ran_on"] == {"value": "harness:claude-code", "source": "log"}
    assert kept[0]["current_tool"] == "harness:codex"
    assert kept[0]["output_saved"] is True

    # Every suggested command names this exact run.
    commands = [s["command"] for s in report["next_steps"] if s["command"]]
    assert commands
    assert all(flow_id in c for c in commands)
    assert f"atelier logs {flow_id} --task worker_b --show all" in commands
    assert f"atelier outputs {flow_id}" in commands

    # One prompt so far, and it went to the selected agent, not the recipe's.
    assert len(fixture.prompts("claude")) == 1
    assert fixture.prompts("codex") == []

    # Repair exactly what the report named, then run the command it printed.
    (fixture.workspace / "fixed.txt").write_text("repaired\n", newline="\n")
    resumed = fixture.run_verbatim(_resume_command(report))
    assert resumed.returncode == 0, resumed.stdout + resumed.stderr

    # The completed worker was never asked again; only the synthesis ran.
    assert len(fixture.prompts("claude")) == 1
    assert len(fixture.prompts("codex")) == 1
    assert CLAUDE_SAID in fixture.prompts("codex")[0]

    outputs = fixture.sh(f"atelier outputs {flow_id} --json\n", "outputs.sh")
    assert outputs.returncode == 0, outputs.stdout + outputs.stderr
    saved = json.loads(outputs.stdout)
    assert CLAUDE_SAID in saved["worker_a"]
    assert "worker B check passed" in saved["worker_b"]
    assert CODEX_SAID in saved["synthesis"]

    after = fixture.diagnose(flow_id)
    assert after["observed"]["state"] == "completed"
    assert after["failures"] == []
    assert sorted(k["task"] for k in after["kept"]) == [
        "synthesis",
        "worker_a",
        "worker_b",
    ]
    assert not any("--resume" in (s["command"] or "") for s in after["next_steps"])


def test_a_logged_out_agent_is_reported_as_a_session_failure(fixture):
    """An agent that never opens a session is a distinct diagnostic, not a task bug."""
    (fixture.workspace / "fixed.txt").write_text("fine\n", newline="\n")
    fixture.log_out("codex")
    first = fixture.sh(
        "atelier run triage --agent worker_a=claude-code\n", "run.sh"
    )
    assert first.returncode != 0, first.stdout
    flow_id = _flow_id(first.stdout)

    report = fixture.diagnose(flow_id)
    assert [f["task"] for f in report["failures"]] == ["synthesis"]
    failure = report["failures"][0]
    assert failure["kind"] == "agent_session"
    assert failure["ran_on"]["value"] == "harness:codex"
    assert "Authentication required" in failure["excerpt"]["text"]
    assert failure["step_records"] == 0
    # The work the agents already did is still kept and still attributed.
    assert sorted(k["task"] for k in report["kept"]) == ["worker_a", "worker_b"]

    rendered = fixture.sh(f"atelier diagnose {flow_id}\n", "render.sh")
    assert rendered.returncode == 0, rendered.stdout + rendered.stderr
    flat = " ".join(rendered.stdout.split())
    assert "the agent refused to open a session" in flat
    assert "logged in" in flat



def test_a_login_refused_after_the_prompt_is_not_called_no_work(fixture):
    """Authentication can fail at prompt time, after the agent has acted.

    The agent opens its session, receives the prompt, writes a file, and only
    then answers auth_required with no chunk. Its words match a logout, but the
    report must not say no work was asked of it: the side effect is on disk.
    """
    (fixture.workspace / "fixed.txt").write_text("fine\n", newline="\n")
    fixture.records["codex"].mkdir(parents=True, exist_ok=True)
    script = {
        "turns": [
            {
                "write": {"path": "prompt-side-effect.txt", "text": "acted\n"},
                "fail_auth": "login expired",
            }
        ],
        "record_path": str(fixture.records["codex"]),
    }
    fixture.env["ATELIER_CODEX_LAUNCH_CMD"] = json.dumps(
        [sys.executable, str(_FAKE_AGENT), "--script", json.dumps(script)]
    )
    first = fixture.sh("atelier run triage --agent worker_a=claude-code\n", "run.sh")
    assert first.returncode != 0, first.stdout
    flow_id = _flow_id(first.stdout)
    assert (fixture.workspace / "prompt-side-effect.txt").exists()

    failure = fixture.diagnose(flow_id)["failures"][0]
    assert failure["task"] == "synthesis"
    assert failure["kind"] == "agent_auth"
    assert failure["step_records"] == 0

    rendered = fixture.sh(f"atelier diagnose {flow_id}\n", "render.sh")
    assert rendered.returncode == 0, rendered.stdout + rendered.stderr
    flat = " ".join(rendered.stdout.split())
    assert "no work was asked of it" not in flat
    assert "refused to open a session" not in flat
    assert "authentication problem" in flat
    assert "treat any change it could have made as unknown" in flat

PARALLEL_YAML = """
name: parallel
description: one worker is cut off while the other fails
tasks:
  - name: editing
    description: change a file, then keep working
    tool: tool:bash
    task: |
      echo changed > touched.txt
      echo work-started
      sleep 30
  - name: failing
    description: fail only once the sibling has edited
    tool: tool:bash
    task: |
      while [ ! -f touched.txt ]; do sleep 0.05; done
      sleep 0.2
      echo deliberate-failure >&2
      exit 7
"""

SLOW_YAML = """
name: slow
description: the agent opens a session, then takes too long to answer
tasks:
  - name: worker
    description: served its prompt, out of time before the reply
    tool: harness:codex
    timeout: 1
    task: "say something"
"""


def _install(fixture, name: str, body: str) -> None:
    """Add one more conduit to the fixture workspace.

    :param fixture: the prepared fixture.
    :param name: the conduit name, also its directory.
    :param body: the conduit YAML.
    """
    path = fixture.workspace / ".atelier" / "conduits" / name
    path.mkdir(parents=True, exist_ok=True)
    (path / "conduit.yaml").write_text(body, newline="\n")


def test_a_cancelled_task_that_had_already_run_is_not_called_unstarted(fixture):
    """A real mid-flight cancellation kept its side effect, so the report says so.

    Two parallel shell tasks: one writes a file and keeps going, the other waits
    for that file and then fails. The first is cancelled while it is executing —
    ``touched.txt`` is on disk to prove it. Reporting that as work that never
    got to run would tell the user their tree is clean when it is not.
    """
    _install(fixture, "parallel", PARALLEL_YAML)
    first = fixture.sh("atelier run parallel --hide-steps\n", "run.sh")
    assert first.returncode != 0, first.stdout
    flow_id = _flow_id(first.stdout)
    assert (fixture.workspace / "touched.txt").read_text().strip() == "changed"

    report = fixture.diagnose(flow_id)
    assert [f["task"] for f in report["failures"]] == ["failing"]
    # Not under not_run, and not described as never having run.
    assert report["not_run"] == []
    cut = report["cancelled"]
    assert [t["task"] for t in cut] == ["editing"]
    assert cut[0]["execution"] == "began"
    assert cut[0]["evidence"] in ("log", "steps")

    rendered = fixture.sh(f"atelier diagnose {flow_id}\n", "render.sh")
    assert rendered.returncode == 0, rendered.stdout + rendered.stderr
    flat = " ".join(rendered.stdout.split())
    assert "never got to run" not in flat
    assert "it was executing when the run failed" in flat
    assert "stays changed" in flat


def test_a_prompt_timeout_is_not_called_a_session_failure(fixture):
    """The session opened and a prompt was served; only the reply ran out of time.

    The distinction is the whole point of the failure kinds: advising a login
    here would send the user to fix an agent that is installed and logged in.
    """
    _install(fixture, "slow", SLOW_YAML)
    fixture.env["ATELIER_CODEX_LAUNCH_CMD"] = json.dumps(
        [
            sys.executable,
            str(_FAKE_AGENT),
            "--script",
            json.dumps(
                {
                    "record_path": str(fixture.records["codex"]),
                    "turns": [{"delay_before": 30, "chunks": ["eventually"]}],
                }
            ),
        ]
    )
    first = fixture.sh("atelier run slow --hide-steps\n", "run.sh")
    assert first.returncode != 0, first.stdout
    flow_id = _flow_id(first.stdout)
    # The agent's own log is the proof the session opened before the clock ran out.
    assert len(fixture.prompts("codex")) == 1

    report = fixture.diagnose(flow_id)
    failure = report["failures"][0]
    assert failure["task"] == "worker"
    assert failure["exit_code"] == 124
    assert failure["kind"] == "timeout"

    rendered = fixture.sh(f"atelier diagnose {flow_id}\n", "render.sh")
    assert rendered.returncode == 0, rendered.stdout + rendered.stderr
    flat = " ".join(rendered.stdout.split())
    assert "ran out of its own time limit" in flat
    assert "refused to open a session" not in flat
    assert "logged in" not in flat
