"""One unchanged recipe, run across four agents, through the real CLI.

Every `atelier` call is a separate process, the engine and the ACP transport
are real, and each agent is the scripted fake recording every prompt it is
handed. What the tests read back is what a user would: the recipe bytes, the
exact flow id a run printed, `status --json`, `outputs`, and the per-agent
prompt logs. The fixtures establish routing and transport, not live-provider
authentication or model quality.

Four agents on purpose: `claude-code` and `codex` through their launch-command
overrides, `gemini` through a registry name overridden by ``ATELIER_HARNESSES``,
and `housebot` as a custom agent the registry has never heard of.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from tests._shell import CLI

FAKE_AGENT = Path(__file__).resolve().parents[1] / "fixtures" / "fake_acp_agent.py"

AGENTS = ("claude-code", "codex", "gemini", "housebot")
SAID = {name: f"{name.upper().replace('-', '_')}_SAID" for name in AGENTS}

BRIEF = "Move the billing job off cron. Constraint: budget=0, deadline=friday."

CHAIN = """\
name: chain
description: a two-agent handoff nobody has to edit
inputs:
  brief:
    description: the shared task
tasks:
  - name: step_1
    description: draft
    task: |
      Propose one fix.

      --- BEGIN BRIEF ---
      {{inputs.brief}}
      --- END BRIEF ---
    tool: harness:claude-code
    depends_on: []
  - name: step_2
    description: review
    task: |
      Review the proposal.

      --- BEGIN BRIEF ---
      {{inputs.brief}}
      --- END BRIEF ---
      --- BEGIN RESULT FROM step_1 ({{step_1.tool}}) ---
      {{step_1.output}}
      --- END RESULT FROM step_1 ---
    tool: harness:codex
    depends_on: [step_1]
"""

PANEL = """\
name: panel
description: two reviewers and a synthesis
max_concurrency: 2
inputs:
  brief:
    description: the shared task
tasks:
  - name: step_1
    description: correctness
    task: "Correctness: {{inputs.brief}}"
    tool: harness:claude-code
    depends_on: []
  - name: step_2
    description: security
    task: "Security: {{inputs.brief}}"
    tool: harness:codex
    depends_on: []
  - name: synthesis
    description: merge
    task: |
      Merge both.

      --- BEGIN RESULT FROM step_1 ({{step_1.tool}}) ---
      {{step_1.output}}
      --- END RESULT FROM step_1 ---
      --- BEGIN RESULT FROM step_2 ({{step_2.tool}}) ---
      {{step_2.output}}
      --- END RESULT FROM step_2 ---
    tool: harness:claude-code
    depends_on: [step_1, step_2]
"""


def _script(
    record_dir: Path, text: str, *, fail: bool = False, extra: dict | None = None
) -> str:
    """Build one fake agent's launch argv as JSON.

    :param record_dir: directory the agent appends its per-process prompt log to.
    :param text: what the agent replies.
    :param fail: when true, opening a session fails the way a logout does.
    :param extra: extra script keys, e.g. the models and efforts on offer.
    :returns: the JSON argv for a launch-command environment variable.
    """
    record_dir.mkdir(parents=True, exist_ok=True)
    script: dict[str, object] = {
        "turns": [{"chunks": [text]}],
        "record_path": str(record_dir),
        **(extra or {}),
    }
    if fail:
        script["fail_session"] = "not logged in"
    return json.dumps(
        [sys.executable, str(FAKE_AGENT), "--script", json.dumps(script)]
    )


class Project:
    """An isolated project whose four agents are scripted fakes.

    :param root: directory holding the project, the global store and the logs.
    """

    def __init__(self, root: Path) -> None:
        self.root = root
        self.work = root / "agent choices"
        (self.work / ".atelier" / "conduits").mkdir(parents=True)
        (root / "global" / "conduits").mkdir(parents=True)
        self.records = {name: root / f"{name}-prompts" for name in AGENTS}
        self.env = {
            k: v for k, v in os.environ.items() if not k.startswith("ATELIER_")
        }
        self.env["ATELIER_GLOBAL_ATELIER_DIR"] = str(root / "global")
        self.env["ATELIER_NO_UPDATE_CHECK"] = "1"
        self.broken: set[str] = set()
        # Per-agent script additions, e.g. the models and efforts an agent
        # advertises — off by default, so most tests exercise plain routing.
        self.extras: dict[str, dict] = {}
        self.apply_agents()

    def apply_agents(self) -> None:
        """Point every agent at the fake, failing the ones marked broken."""
        self.env["ATELIER_CLAUDE_LAUNCH_CMD"] = self._for("claude-code")
        self.env["ATELIER_CODEX_LAUNCH_CMD"] = self._for("codex")
        self.env["ATELIER_HARNESSES"] = json.dumps(
            {
                name: json.loads(self._for(name))
                for name in ("gemini", "housebot")
            }
        )

    def _for(self, name: str) -> str:
        """Return one agent's launch argv, honouring the broken set.

        :param name: the agent name.
        :returns: the JSON argv.
        """
        return _script(
            self.records[name],
            SAID[name],
            fail=name in self.broken,
            extra=self.extras.get(name),
        )

    def break_agent(self, name: str) -> None:
        """Make ``name`` fail to open a session, as a logged-out agent does.

        :param name: the agent to break.
        """
        self.broken.add(name)
        self.apply_agents()

    def fix_agent(self, name: str) -> None:
        """Let ``name`` open sessions again.

        :param name: the agent to repair.
        """
        self.broken.discard(name)
        self.apply_agents()

    def add_unavailable_agent(self, name: str) -> None:
        """Register ``name`` pointing at a binary that does not exist.

        The not-installed case, as opposed to :meth:`break_agent`'s logged-out
        one: readiness can see this before a session is ever opened.

        :param name: the agent name to register.
        """
        harnesses = json.loads(self.env["ATELIER_HARNESSES"])
        harnesses[name] = [str(self.root / "no-such-agent-binary")]
        self.env["ATELIER_HARNESSES"] = json.dumps(harnesses)

    def remove_agent(self, name: str) -> None:
        """Drop ``name`` from the configuration, as uninstalling it would.

        :param name: the custom agent to forget.
        """
        harnesses = json.loads(self.env["ATELIER_HARNESSES"])
        harnesses.pop(name, None)
        self.env["ATELIER_HARNESSES"] = json.dumps(harnesses)

    def progress_bytes(self, flow_id: str) -> bytes:
        """Return a flow's saved progress file, byte for byte.

        :param flow_id: the exact flow id.
        :returns: the ``progress.json`` contents.
        """
        return (self.work / ".atelier" / "flows" / flow_id / "progress.json").read_bytes()

    def saved_bytes(self, flow_id: str) -> dict[str, bytes]:
        """Return every file a flow saved, byte for byte.

        :param flow_id: the exact flow id.
        :returns: mapping of file name to contents.
        """
        flow_dir = self.work / ".atelier" / "flows" / flow_id
        return {f.name: f.read_bytes() for f in sorted(flow_dir.glob("*")) if f.is_file()}

    def install(self, name: str, text: str) -> None:
        """Write a recipe into the project store.

        :param name: the conduit name (and folder).
        :param text: the conduit.yaml contents.
        """
        path = self.work / ".atelier" / "conduits" / name
        path.mkdir(parents=True, exist_ok=True)
        (path / "conduit.yaml").write_text(text, encoding="utf-8")

    def recipe(self, name: str) -> bytes:
        """Return a recipe's exact bytes.

        :param name: the conduit name.
        :returns: the conduit.yaml contents.
        """
        return (
            self.work / ".atelier" / "conduits" / name / "conduit.yaml"
        ).read_bytes()

    def cli(self, *args: str, timeout: float = 180) -> subprocess.CompletedProcess:
        """Run one `atelier` command as its own process, with stdin closed.

        :param args: the command line after ``atelier``.
        :param timeout: seconds before the process is killed.
        :returns: the completed process.
        """
        return subprocess.run(
            [sys.executable, "-c", CLI, *args],
            cwd=self.work,
            env=self.env,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )

    def flow_id(self, result: subprocess.CompletedProcess) -> str:
        """Extract the exact flow id a run printed.

        :param result: the finished ``atelier run`` process.
        :returns: the flow id.
        """
        for line in result.stdout.splitlines():
            if line.startswith("flow_id:"):
                return line.split(":", 1)[1].strip()
        raise AssertionError(f"no flow_id in:\n{result.stdout}\n{result.stderr}")

    def status(self, flow_id: str) -> dict:
        """Return a flow's ``status --json`` payload.

        :param flow_id: the exact flow id.
        :returns: the parsed payload.
        """
        done = self.cli("status", flow_id, "--json")
        assert done.returncode == 0, done.stdout + done.stderr
        return json.loads(done.stdout)

    def prompts(self, name: str) -> list[str]:
        """Return every prompt one agent received, across all its processes.

        :param name: the agent name.
        :returns: one joined text block per prompt call.
        """
        return [
            "\n".join(block.get("text", "") for block in json.loads(line))
            for log in sorted(self.records[name].glob("*.jsonl"))
            for line in log.read_text(encoding="utf-8").splitlines()
        ]

    def flows(self) -> list[str]:
        """Return the ids of every top-level flow on disk.

        :returns: sorted flow directory names.
        """
        return sorted(
            p.name for p in (self.work / ".atelier" / "flows").glob("*") if p.is_dir()
        )


def _flat(text: str) -> str:
    """Return ``text`` with its line wrapping collapsed.

    The console wraps a long diagnostic to the terminal width, which can land a
    newline inside the phrase a test is looking for.

    :param text: captured output.
    :returns: the same words, single-spaced.
    """
    return " ".join(text.split())


@pytest.fixture
def project(tmp_path):
    """A project with the chain recipe installed and four fake agents.

    :param tmp_path: pytest temp directory fixture.
    :returns: the :class:`Project`.
    """
    p = Project(tmp_path)
    p.install("chain", CHAIN)
    return p


# ------------------------------------------------------- sequential handoff


def test_one_recipe_four_agents_with_the_file_untouched(project):
    """Both agent steps are re-pointed; the handoff and the labels follow."""
    before = project.recipe("chain")

    run = project.cli(
        "run", "chain",
        "--agent", "step_1=gemini",
        "--agent", "step_2=housebot",
        "--input", f"brief={BRIEF}",
    )
    assert run.returncode == 0, run.stdout + run.stderr
    flow_id = project.flow_id(run)

    # The chosen agents ran; the recipe's own two were never started.
    assert len(project.prompts("gemini")) == 1
    assert len(project.prompts("housebot")) == 1
    assert project.prompts("claude-code") == []
    assert project.prompts("codex") == []

    # The brief reached the first, and its result reached the second —
    # attributed to the agent that actually produced it.
    assert BRIEF in project.prompts("gemini")[0]
    handoff = project.prompts("housebot")[0]
    assert "--- BEGIN RESULT FROM step_1 (harness:gemini) ---" in handoff
    assert SAID["gemini"] in handoff
    assert "harness:claude-code" not in handoff

    payload = project.status(flow_id)
    assert payload["status"] == "completed"
    assert payload["task_agents"] == {
        "step_1": "harness:gemini", "step_2": "harness:housebot"
    }
    assert payload["flow_id"] == flow_id

    saved = project.cli("outputs", flow_id, "--task", "step_2")
    assert saved.returncode == 0, saved.stdout + saved.stderr
    assert SAID["housebot"] in saved.stdout

    assert project.recipe("chain") == before


def test_a_model_and_effort_choice_is_recorded_as_what_ran(project):
    """The stored assignment identifies harness, model and effort together."""
    project.extras["gemini"] = {
        "models": {
            "current": "flash",
            "available": [{"id": "flash", "name": "Flash"},
                          {"id": "gemini-3-pro", "name": "Pro"}],
        },
        "efforts": {"current": "low", "available": ["low", "high"]},
    }
    project.apply_agents()

    run = project.cli(
        "run", "chain",
        "--agent", "step_2=gemini:gemini-3-pro:high",
        "--input", f"brief={BRIEF}",
    )
    assert run.returncode == 0, run.stdout + run.stderr
    # The agent was actually told which model and effort to use.
    assert "[config_set:model=gemini-3-pro]" in run.stdout
    assert "high]" in run.stdout
    payload = project.status(project.flow_id(run))
    assert payload["task_agents"] == {"step_2": "harness:gemini:gemini-3-pro:high"}


def test_the_human_status_view_names_the_chosen_agent(project):
    """A reader of the table sees which agent each task was given."""
    run = project.cli(
        "run", "chain", "--agent", "step_2=housebot", "--input", f"brief={BRIEF}"
    )
    assert run.returncode == 0, run.stdout + run.stderr
    shown = project.cli("status", project.flow_id(run))
    assert shown.returncode == 0, shown.stdout + shown.stderr
    assert "agent" in shown.stdout and "harness:housebot" in shown.stdout
    assert "the conduit file is unchanged" in shown.stdout


def test_two_invocations_keep_their_own_mappings(project):
    """Independent runs of one recipe never inherit each other's choices."""
    first = project.cli(
        "run", "chain", "--agent", "step_1=gemini", "--input", f"brief={BRIEF}"
    )
    second = project.cli(
        "run", "chain", "--agent", "step_2=housebot", "--input", f"brief={BRIEF}"
    )
    assert first.returncode == 0 and second.returncode == 0
    one, two = project.flow_id(first), project.flow_id(second)
    assert one != two
    assert project.status(one)["task_agents"] == {"step_1": "harness:gemini"}
    assert project.status(two)["task_agents"] == {"step_2": "harness:housebot"}
    # Each run used exactly one replacement, so each agent answered once.
    assert len(project.prompts("gemini")) == 1
    assert len(project.prompts("housebot")) == 1
    assert len(project.prompts("claude-code")) == 1
    assert len(project.prompts("codex")) == 1


# ---------------------------------------------------- parallel and synthesis


def test_a_parallel_panel_synthesizes_from_the_agents_that_ran(project):
    """Replaced reviewers run side by side and are attributed correctly."""
    project.install("panel", PANEL)
    run = project.cli(
        "run", "panel",
        "--agent", "step_2=gemini",
        "--agent", "synthesis=housebot",
        "--input", f"brief={BRIEF}",
    )
    assert run.returncode == 0, run.stdout + run.stderr
    flow_id = project.flow_id(run)

    # The reviewers saw only the brief; the synthesizer saw both results.
    for name in ("claude-code", "gemini"):
        assert "RESULT FROM" not in project.prompts(name)[0]
    merged = project.prompts("housebot")[0]
    assert "--- BEGIN RESULT FROM step_1 (harness:claude-code) ---" in merged
    assert "--- BEGIN RESULT FROM step_2 (harness:gemini) ---" in merged
    assert SAID["claude-code"] in merged and SAID["gemini"] in merged
    assert "harness:codex" not in merged

    payload = project.status(flow_id)
    assert payload["status"] == "completed"
    assert payload["task_agents"] == {
        "step_2": "harness:gemini", "synthesis": "harness:housebot"
    }
    saved = project.cli("outputs", flow_id, "--task", "synthesis")
    assert SAID["housebot"] in saved.stdout


# -------------------------------------------------------- failure and repair


def _failed_chain(project) -> str:
    """Run the chain with step_2 on a logged-out gemini and return the flow id.

    :param project: the configured project.
    :returns: the failed flow's id.
    """
    project.break_agent("gemini")
    run = project.cli(
        "run", "chain", "--agent", "step_2=gemini", "--input", f"brief={BRIEF}"
    )
    assert run.returncode == 1, run.stdout + run.stderr
    return project.flow_id(run)


def test_a_resume_in_a_new_process_inherits_the_saved_choice(project):
    """No flags to retype: the saved assignment is reused as it was."""
    flow_id = _failed_chain(project)
    failed = project.status(flow_id)
    assert failed["status"] == "failed"
    assert failed["tasks"]["step_1"]["status"] == "completed"
    assert failed["tasks"]["step_2"]["status"] == "failed"
    assert failed["task_agents"] == {"step_2": "harness:gemini"}

    project.fix_agent("gemini")
    resumed = project.cli("run", "--resume", flow_id)
    assert resumed.returncode == 0, resumed.stdout + resumed.stderr

    # The completed step was not asked again; the repaired one ran on the
    # agent the first invocation chose, not on the recipe's codex.
    assert len(project.prompts("claude-code")) == 1
    assert len(project.prompts("gemini")) == 1
    assert project.prompts("codex") == []
    done = project.status(flow_id)
    assert done["status"] == "completed"
    assert done["task_agents"] == {"step_2": "harness:gemini"}
    assert SAID["claude-code"] in project.prompts("gemini")[0]

    outputs = project.cli("outputs", flow_id)
    assert SAID["claude-code"] in outputs.stdout
    assert SAID["gemini"] in outputs.stdout


def test_a_resume_can_hand_the_failed_task_to_another_agent(project):
    """The deliberate recovery: name a different agent for the step that broke."""
    project.break_agent("codex")
    run = project.cli("run", "chain", "--input", f"brief={BRIEF}")
    assert run.returncode == 1, run.stdout + run.stderr
    flow_id = project.flow_id(run)
    assert project.status(flow_id)["task_agents"] == {}

    resumed = project.cli(
        "run", "--resume", flow_id, "--agent", "step_2=housebot"
    )
    assert resumed.returncode == 0, resumed.stdout + resumed.stderr

    # The upstream work was not repeated, and the replacement got its result.
    assert len(project.prompts("claude-code")) == 1
    replacement = project.prompts("housebot")[0]
    assert BRIEF in replacement and SAID["claude-code"] in replacement
    assert "--- BEGIN RESULT FROM step_1 (harness:claude-code) ---" in replacement

    done = project.status(flow_id)
    assert done["status"] == "completed"
    assert done["task_agents"] == {"step_2": "harness:housebot"}
    saved = project.cli("outputs", flow_id, "--task", "step_2")
    assert SAID["housebot"] in saved.stdout


def test_a_resume_refuses_to_re_point_a_completed_task(project):
    """A finished step keeps its agent, and the refusal changes nothing."""
    flow_id = _failed_chain(project)
    flow_dir = project.work / ".atelier" / "flows" / flow_id
    before = {
        name: (flow_dir / name).read_bytes()
        for name in ("progress.json", "outputs.yaml", "logs.jsonl")
    }

    project.fix_agent("gemini")
    refused = project.cli("run", "--resume", flow_id, "--agent", "step_1=housebot")
    assert refused.returncode == 1, refused.stdout + refused.stderr
    assert "is completed in this flow" in refused.stdout
    assert "--again" in refused.stdout

    assert project.prompts("housebot") == []
    for name, data in before.items():
        assert (flow_dir / name).read_bytes() == data


def test_a_flow_recorded_before_selection_existed_resumes_on_the_recipe(project):
    """No metadata means the recipe decides, which is the old behavior."""
    project.break_agent("codex")
    run = project.cli("run", "chain", "--input", f"brief={BRIEF}")
    flow_id = project.flow_id(run)
    progress = project.work / ".atelier" / "flows" / flow_id / "progress.json"
    legacy = json.loads(progress.read_text())
    del legacy["task_agents"]
    progress.write_text(json.dumps(legacy))

    project.fix_agent("codex")
    resumed = project.cli("run", "--resume", flow_id)
    assert resumed.returncode == 0, resumed.stdout + resumed.stderr
    assert len(project.prompts("codex")) == 1
    assert project.status(flow_id)["task_agents"] == {}


def test_a_saved_choice_the_recipe_no_longer_has_fails_loudly(project):
    """Never silently dropped: the run stops and points at a fresh run."""
    flow_id = _failed_chain(project)
    project.fix_agent("gemini")
    project.install("chain", CHAIN.replace("step_2", "review"))

    refused = project.cli("run", "--resume", flow_id)
    assert refused.returncode == 1, refused.stdout + refused.stderr
    assert "saved agent choices" in refused.stdout
    assert "start a fresh run" in refused.stdout
    assert project.prompts("gemini") == []


# ------------------------------------------------------------------- --again


def test_again_inherits_the_choices_and_accepts_an_override(project):
    """A fresh flow, its own record, and the source run left as it was."""
    source_run = project.cli(
        "run", "chain", "--agent", "step_2=gemini", "--input", f"brief={BRIEF}"
    )
    assert source_run.returncode == 0, source_run.stdout + source_run.stderr
    source = project.flow_id(source_run)

    inherited = project.cli("run", "--again", source)
    assert inherited.returncode == 0, inherited.stdout + inherited.stderr
    second = project.flow_id(inherited)
    assert second != source
    assert project.status(second)["task_agents"] == {"step_2": "harness:gemini"}

    overridden = project.cli("run", "--again", source, "--agent", "step_1=housebot")
    assert overridden.returncode == 0, overridden.stdout + overridden.stderr
    third = project.flow_id(overridden)
    assert project.status(third)["task_agents"] == {
        "step_1": "harness:housebot", "step_2": "harness:gemini"
    }
    # The brief came from the source run's saved inputs, not retyped.
    assert BRIEF in project.prompts("housebot")[0]

    # The source flow is byte-identical in what it recorded.
    assert project.status(source)["task_agents"] == {"step_2": "harness:gemini"}
    assert project.flows() == sorted([source, second, third])


def test_an_invalid_selection_creates_no_flow_at_all(project):
    """Validation precedes the flow directory, so a typo costs nothing."""
    before = project.flows()
    refused = project.cli(
        "run", "chain", "--agent", "step_9=gemini", "--input", f"brief={BRIEF}"
    )
    assert refused.returncode == 1, refused.stdout + refused.stderr
    assert "has no task 'step_9'" in refused.stdout
    assert project.flows() == before
    assert all(project.prompts(name) == [] for name in AGENTS)


# ------------------------------------------------- readiness on resume and again


def test_an_unstartable_replacement_stops_an_again_before_the_first_prompt(project):
    """A run that cannot finish must not spend the step before the one that fails."""
    first = project.cli("run", "chain", "--input", f"brief={BRIEF}")
    assert first.returncode == 0, first.stdout + first.stderr
    source = project.flow_id(first)
    project.add_unavailable_agent("ghost")
    before = len(project.prompts("claude-code"))

    again = project.cli("run", "--again", source, "--agent", "step_2=ghost")

    assert again.returncode == 1, again.stdout + again.stderr
    assert "cannot run:" in _flat(again.stdout)
    # No upstream prompt was paid for, and no half-born flow was left behind.
    assert len(project.prompts("claude-code")) == before
    assert project.flows() == [source]


def test_an_unstartable_replacement_leaves_a_resume_exactly_as_it_was(project):
    """Validation finishes before the resume writes its first byte."""
    project.break_agent("codex")
    failed = project.cli("run", "chain", "--input", f"brief={BRIEF}")
    assert failed.returncode == 1, failed.stdout
    flow_id = project.flow_id(failed)
    project.add_unavailable_agent("ghost")
    before = project.saved_bytes(flow_id)

    resumed = project.cli("run", "--resume", flow_id, "--agent", "step_2=ghost")

    assert resumed.returncode == 1, resumed.stdout + resumed.stderr
    assert "cannot run:" in _flat(resumed.stdout)
    assert project.saved_bytes(flow_id) == before
    assert project.status(flow_id)["task_agents"] == {}


def test_a_resume_reuses_saved_output_from_an_agent_that_is_gone(project):
    """A completed step is read back from disk, so its agent need not be there."""
    project.break_agent("codex")
    failed = project.cli("run", "chain", "--input", f"brief={BRIEF}")
    flow_id = project.flow_id(failed)
    # The agent that produced step_1 is no longer installed.
    project.env["ATELIER_CLAUDE_LAUNCH_CMD"] = json.dumps(
        [str(project.root / "no-such-agent-binary")]
    )

    resumed = project.cli("run", "--resume", flow_id, "--agent", "step_2=gemini")

    assert resumed.returncode == 0, resumed.stdout + resumed.stderr
    handoff = project.prompts("gemini")[0]
    assert SAID["claude-code"] in handoff
    assert "--- BEGIN RESULT FROM step_1 (harness:claude-code) ---" in handoff
    assert len(project.prompts("claude-code")) == 1


# ------------------------------------------- replacing an agent that was removed


def test_a_removed_custom_agent_is_replaced_without_redoing_the_work(project):
    """The recovery the milestone is for: uninstalled agent, explicit successor."""
    project.break_agent("housebot")
    failed = project.cli(
        "run", "chain", "--agent", "step_2=housebot", "--input", f"brief={BRIEF}"
    )
    assert failed.returncode == 1, failed.stdout
    flow_id = project.flow_id(failed)
    project.remove_agent("housebot")

    resumed = project.cli("run", "--resume", flow_id, "--agent", "step_2=codex")

    assert resumed.returncode == 0, resumed.stdout + resumed.stderr
    # The upstream step was asked exactly once, in the first run.
    assert len(project.prompts("claude-code")) == 1
    assert len(project.prompts("codex")) == 1
    assert SAID["claude-code"] in project.prompts("codex")[0]
    assert project.status(flow_id)["task_agents"] == {"step_2": "harness:codex"}
    saved = project.cli("outputs", flow_id, "--task", "step_2")
    assert SAID["codex"] in saved.stdout

    # And the same recovery from scratch gets its own record, source untouched.
    recovered = project.saved_bytes(flow_id)
    again = project.cli("run", "--again", flow_id, "--agent", "step_2=codex")
    assert again.returncode == 0, again.stdout + again.stderr
    new_id = project.flow_id(again)
    assert new_id != flow_id
    assert project.status(new_id)["task_agents"] == {"step_2": "harness:codex"}
    assert project.saved_bytes(flow_id) == recovered


def test_a_saved_agent_that_is_gone_and_unreplaced_says_so(project):
    """The diagnostic names the missing agent instead of blaming the recipe."""
    project.break_agent("housebot")
    failed = project.cli(
        "run", "chain", "--agent", "step_2=housebot", "--input", f"brief={BRIEF}"
    )
    flow_id = project.flow_id(failed)
    project.remove_agent("housebot")

    resumed = project.cli("run", "--resume", flow_id)

    assert resumed.returncode == 1, resumed.stdout
    assert "unknown agent 'harness:housebot'" in _flat(resumed.stdout)
    assert "recipe changed" not in _flat(resumed.stdout)


# -------------------------------------------------------- corrupt saved metadata


def test_a_saved_choice_that_is_not_an_agent_never_becomes_a_shell(project):
    """A hand-edited record must not turn an agent prompt into a command."""
    project.install("chain", CHAIN.replace("Review the proposal.", "echo PROBE"))
    project.break_agent("codex")
    failed = project.cli("run", "chain", "--input", f"brief={BRIEF}")
    flow_id = project.flow_id(failed)
    path = project.work / ".atelier" / "flows" / flow_id / "progress.json"
    record = json.loads(path.read_text())
    record["task_agents"] = {"step_2": "tool:bash"}
    path.write_text(json.dumps(record))
    before = project.saved_bytes(flow_id)

    resumed = project.cli("run", "--resume", flow_id)

    assert resumed.returncode == 1, resumed.stdout
    assert "'tool:bash' is not an agent" in _flat(resumed.stdout)
    assert "PROBE" not in resumed.stdout
    logs = (project.work / ".atelier" / "flows" / flow_id / "logs.jsonl").read_text()
    assert "tool:bash" not in logs
    assert project.saved_bytes(flow_id) == before


# ------------------------------------------------- provenance of completed work


def test_a_completed_step_keeps_its_own_agent_when_the_recipe_moves_on(project):
    """What the next agent reads is what ran, not what the file now says."""
    project.break_agent("codex")
    failed = project.cli("run", "chain", "--input", f"brief={BRIEF}")
    flow_id = project.flow_id(failed)
    # The recipe's default for the completed step changes under the flow.
    project.install("chain", CHAIN.replace("tool: harness:claude-code", "tool: harness:gemini"))
    project.fix_agent("codex")

    resumed = project.cli("run", "--resume", flow_id)

    assert resumed.returncode == 0, resumed.stdout + resumed.stderr
    # Gemini never ran, so nothing may be attributed to it.
    assert project.prompts("gemini") == []
    assert len(project.prompts("claude-code")) == 1
    handoff = project.prompts("codex")[0]
    assert "--- BEGIN RESULT FROM step_1 (harness:claude-code) ---" in handoff
    assert "harness:gemini" not in handoff
    # Inspection agrees with the prompt.
    assert project.status(flow_id)["task_agents"]["step_1"] == "harness:claude-code"
    human = project.cli("status", flow_id)
    assert "harness:claude-code" in human.stdout


# ---------------------------------------- recipes composed before the template


# What `atelier compose` wrote before it used `{{step_1.tool}}`: the agent's
# name as a literal, fixed when the file was generated.
OLD_COMPOSED = """\
name: chain
description: a handoff composed before the label was a template
inputs:
  brief:
    description: the shared task
tasks:
  - name: step_1
    description: draft
    task: |
      Propose one fix.

      --- BEGIN BRIEF ---
      {{inputs.brief}}
      --- END BRIEF ---
    tool: harness:claude-code
    depends_on: []
  - name: step_2
    description: review
    task: |
      Review the proposal.

      --- BEGIN RESULT FROM step_1 (harness:claude-code) ---
      {{step_1.output}}
      --- END RESULT FROM step_1 (harness:claude-code) ---
    tool: harness:codex
    depends_on: [step_1]
"""


def test_an_old_static_label_is_corrected_for_the_agent_reading_it(project):
    """The literal stays; an authoritative block beneath it names what ran."""
    project.install("chain", OLD_COMPOSED)
    before = project.recipe("chain")

    run = project.cli(
        "run", "chain", "--agent", "step_1=gemini", "--input", f"brief={BRIEF}"
    )

    assert run.returncode == 0, run.stdout + run.stderr
    handoff = project.prompts("codex")[0]
    # The user's own text is untouched.
    assert "--- BEGIN RESULT FROM step_1 (harness:claude-code) ---" in handoff
    # And the receiving agent is told who actually produced it.
    assert "--- BEGIN AGENT PROVENANCE (authoritative) ---" in handoff
    assert "step_1 ran on harness:gemini" in handoff
    assert SAID["gemini"] in handoff
    assert project.recipe("chain") == before


def test_an_unreplaced_old_recipe_gains_no_provenance_block(project):
    """A label that is already right must not grow a correction beneath it."""
    project.install("chain", OLD_COMPOSED)

    run = project.cli("run", "chain", "--input", f"brief={BRIEF}")

    assert run.returncode == 0, run.stdout + run.stderr
    assert "AGENT PROVENANCE" not in project.prompts("codex")[0]


def test_a_recipe_using_the_template_label_gains_no_provenance_block(project):
    """`{{step_1.tool}}` cannot disagree with itself, so nothing is appended."""
    run = project.cli(
        "run", "chain", "--agent", "step_1=gemini", "--input", f"brief={BRIEF}"
    )

    assert run.returncode == 0, run.stdout + run.stderr
    handoff = project.prompts("codex")[0]
    assert "--- BEGIN RESULT FROM step_1 (harness:gemini) ---" in handoff
    assert "AGENT PROVENANCE" not in handoff


# ------------------------------- continuing work an agent that is gone finished


def test_a_resume_keeps_a_finished_step_whose_custom_agent_was_uninstalled(project):
    """Housebot's saved answer is on disk; resuming must not need Housebot back."""
    project.break_agent("codex")
    failed = project.cli(
        "run", "chain", "--agent", "step_1=housebot", "--input", f"brief={BRIEF}"
    )
    assert failed.returncode == 1, failed.stdout
    flow_id = project.flow_id(failed)
    # The agent that failed is repaired, then the custom agent that already
    # finished step_1 is uninstalled. That order matters: repairing rewrites the
    # whole agent table, which would put Housebot back.
    project.fix_agent("codex")
    project.remove_agent("housebot")

    resumed = project.cli("run", "--resume", flow_id)

    assert resumed.returncode == 0, resumed.stdout + resumed.stderr
    # Housebot was asked exactly once, in the first run.
    assert len(project.prompts("housebot")) == 1
    handoff = project.prompts("codex")[0]
    assert SAID["housebot"] in handoff
    assert "--- BEGIN RESULT FROM step_1 (harness:housebot) ---" in handoff
    assert project.status(flow_id)["task_agents"]["step_1"] == "harness:housebot"
    saved = project.cli("outputs", flow_id, "--task", "step_1")
    assert SAID["housebot"] in saved.stdout


LOOP_CHAIN = """\
name: chain
description: a loop, then a handoff
inputs:
  brief:
    description: the shared task
tasks:
  - name: step_1
    description: draft twice
    task: "Propose one fix: {{inputs.brief}}"
    tool: harness:claude-code
    repeat: 2
    depends_on: []
  - name: step_2
    description: review
    task: |
      Review the proposal.

      --- BEGIN RESULT FROM step_1 ({{step_1.tool}}) ---
      {{step_1.output}}
      --- END RESULT FROM step_1 ---
    tool: harness:codex
    depends_on: [step_1]
"""


def test_a_finished_loop_does_not_block_a_resume_when_the_recipe_drifts(project):
    """Both iterations ran, so no remaining history can be split across agents."""
    project.install("chain", LOOP_CHAIN)
    project.break_agent("codex")
    failed = project.cli("run", "chain", "--input", f"brief={BRIEF}")
    assert failed.returncode == 1, failed.stdout
    flow_id = project.flow_id(failed)
    assert project.status(flow_id)["tasks"]["step_1"]["of"] == 2
    # The recipe's default for the finished loop changes under the flow.
    project.install(
        "chain", LOOP_CHAIN.replace("tool: harness:claude-code", "tool: harness:gemini")
    )

    resumed = project.cli("run", "--resume", flow_id, "--agent", "step_2=housebot")

    assert resumed.returncode == 0, resumed.stdout + resumed.stderr
    # No iteration re-ran, and nothing is attributed to the recipe's new name.
    assert len(project.prompts("claude-code")) == 2
    assert project.prompts("gemini") == []
    handoff = project.prompts("housebot")[0]
    assert "--- BEGIN RESULT FROM step_1 (harness:claude-code) ---" in handoff
    assert "harness:gemini" not in handoff
    assert project.status(flow_id)["task_agents"]["step_1"] == "harness:claude-code"


# The same loop, left owing an iteration: it never matches its predicate, so it
# fails with history on record and a resume would continue it.
UNFINISHED_LOOP = LOOP_CHAIN.replace(
    "    repeat: 2\n",
    '    repeat: 2\n    until: output.match(NEVER_APPEARS)\n    on_exhaust: fail\n',
)


def test_a_half_run_loop_still_refuses_the_recipe_that_changed_its_agent(project):
    """The guard that matters stays: a loop with an iteration left is refused."""
    project.install("chain", UNFINISHED_LOOP)
    failed = project.cli("run", "chain", "--input", f"brief={BRIEF}")
    assert failed.returncode == 1, failed.stdout
    flow_id = project.flow_id(failed)
    assert project.status(flow_id)["tasks"]["step_1"]["status"] != "completed"
    before = project.saved_bytes(flow_id)
    # The recipe's default for the unfinished loop changes under the flow.
    project.install(
        "chain",
        UNFINISHED_LOOP.replace("tool: harness:claude-code", "tool: harness:gemini"),
    )

    resumed = project.cli("run", "--resume", flow_id)

    assert resumed.returncode == 1, resumed.stdout
    assert "recorded iterations on harness:claude-code" in _flat(resumed.stdout)
    assert project.prompts("gemini") == []
    assert project.saved_bytes(flow_id) == before
