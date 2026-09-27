"""Two coding agents editing the same file at once, through the real CLI.

Every `atelier` call is a separate process, the engine, Git and the ACP
transport are real, and the repository is a throwaway one under ``tmp_path``
whose path contains a space. The agents are scripted fakes that write a file
into whatever directory they were started in — which is the whole point: what
these tests read back is the bytes on disk, the source checkout's own
``git status``, and the exact paths the run recorded.

Fixtures establish routing, separation and recovery. They establish nothing
about provider authentication, model quality, or what a real agent would write.
"""
from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from tests._shell import CLI

FAKE_AGENT = Path(__file__).resolve().parents[1] / "fixtures" / "fake_acp_agent.py"

AGENTS = ("claude-code", "codex", "gemini")
SAID = {name: f"{name.upper().replace('-', '_')}_SAID" for name in AGENTS}
EDIT = {name: f"# rewritten by {name}\n" for name in AGENTS}

BRIEF = "Rewrite NOTES.md. Constraint: keep it one line."

PAIR = """\
name: pair
description: two independent writers, then a comparison
max_concurrency: 2
inputs:
  brief:
    description: the shared task
tasks:
  - name: writer_a
    description: first candidate
    task: "Candidate A. {{inputs.brief}}"
    tool: harness:claude-code
    depends_on: []
  - name: writer_b
    description: second candidate
    task: "Candidate B. {{inputs.brief}}"
    tool: harness:codex
    depends_on: []
  - name: compare
    description: read both candidates
    task: |
      Compare the two candidates below.

      --- BEGIN CANDIDATE writer_a ---
      workspace: {{writer_a.workspace}}
      agent: {{writer_a.tool}}
      said: {{writer_a.output}}
      --- END CANDIDATE writer_a ---
      --- BEGIN CANDIDATE writer_b ---
      workspace: {{writer_b.workspace}}
      agent: {{writer_b.tool}}
      said: {{writer_b.output}}
      --- END CANDIDATE writer_b ---
    tool: harness:gemini
    depends_on: [writer_a, writer_b]
"""

PANEL = """\
name: panel
description: two writers and a synthesis that never mentions a directory
max_concurrency: 2
tasks:
  - name: writer_a
    description: first candidate
    task: "Candidate A."
    tool: harness:claude-code
    depends_on: []
  - name: writer_b
    description: second candidate
    task: "Candidate B."
    tool: harness:codex
    depends_on: []
  - name: synthesis
    description: merge
    task: |
      Merge both candidates.

      --- BEGIN RESULT FROM writer_a ---
      {{writer_a.output}}
      --- END RESULT FROM writer_a ---
      --- BEGIN RESULT FROM writer_b ---
      {{writer_b.output}}
      --- END RESULT FROM writer_b ---
    tool: harness:gemini
    depends_on: [writer_a, writer_b]
"""

SHELL = """\
name: shell
description: a shell worker beside an agent worker
max_concurrency: 2
tasks:
  - name: writer_a
    description: agent
    task: "edit it"
    tool: harness:claude-code
    depends_on: []
  - name: build
    description: shell
    task: "pwd > WHERE.txt && printf 'built\\n' > NOTES.md"
    tool: tool:bash
    depends_on: []
"""

GATED = """\
name: gated
description: an agent and a human approval
tasks:
  - name: writer_a
    description: agent
    task: "edit it"
    tool: harness:claude-code
    depends_on: []
  - name: sign_off
    description: human
    task: "ship it?"
    tool: tool:hitl
    depends_on: [writer_a]
"""

CHILD = """\
name: child
description: a nested recipe with a same-named task
tasks:
  - name: writer_a
    description: the child's own writer
    task: "child work"
    tool: harness:codex
    depends_on: []
"""

PARENT = """\
name: parent
description: one isolated task and one nested conduit
tasks:
  - name: writer_a
    description: the parent's writer
    task: "parent work"
    tool: harness:claude-code
    depends_on: []
  - name: sub
    description: run the child
    task: child
    tool: tool:conduit
    depends_on: [writer_a]
"""


def git(*args: str, cwd) -> str:
    """Run one git command in ``cwd``, asserting it succeeded.

    :param args: the arguments after ``git``.
    :param cwd: the directory to run in.
    :returns: stdout, stripped.
    """
    done = subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True
    )
    assert done.returncode == 0, f"git {args}: {done.stderr}"
    return done.stdout.strip()


class Project:
    """A committed repository whose three agents are scripted fakes.

    :param root: directory holding the repository, the global store and logs.
    """

    def __init__(self, root: Path) -> None:
        self.root = root
        self.work = root / "my repo"
        self.work.mkdir(parents=True)
        (self.work / ".atelier" / "conduits").mkdir(parents=True)
        (root / "global" / "conduits").mkdir(parents=True)
        # `.atelier/` holds the flow records and the checkouts; keeping it out
        # of the index is what every real project does, and what the guide says.
        (self.work / ".gitignore").write_text(".atelier/\n", encoding="utf-8")
        (self.work / "NOTES.md").write_text("original\n", encoding="utf-8")
        git("init", "-q", ".", cwd=self.work)
        git("config", "user.email", "t@example.com", cwd=self.work)
        git("config", "user.name", "T", cwd=self.work)
        git("add", "-A", cwd=self.work)
        git("commit", "-qm", "init", cwd=self.work)
        self.base = git("rev-parse", "HEAD", cwd=self.work)

        self.records = {name: root / f"{name}-prompts" for name in AGENTS}
        self.env = {
            k: v for k, v in os.environ.items() if not k.startswith("ATELIER_")
        }
        self.env["ATELIER_GLOBAL_ATELIER_DIR"] = str(root / "global")
        self.env["ATELIER_NO_UPDATE_CHECK"] = "1"
        # Per-agent script overrides: what it writes, whether it waits for the
        # other one, and whether it fails after writing.
        self.behaviour: dict[str, dict] = {}
        self.apply_agents()

    # ------------------------------------------------------------- agents

    def apply_agents(self) -> None:
        """Point every agent at the fake, with its current behaviour."""
        self.env["ATELIER_CLAUDE_LAUNCH_CMD"] = self._for("claude-code")
        self.env["ATELIER_CODEX_LAUNCH_CMD"] = self._for("codex")
        self.env["ATELIER_HARNESSES"] = json.dumps(
            {"gemini": json.loads(self._for("gemini"))}
        )

    def _for(self, name: str) -> str:
        """Return one agent's launch argv as JSON.

        :param name: the agent name.
        :returns: the JSON argv for a launch-command environment variable.
        """
        spec = self.behaviour.get(name, {})
        self.records[name].mkdir(parents=True, exist_ok=True)
        turn: dict[str, object] = {"chunks": [SAID[name]]}
        if spec.get("writes"):
            turn["write"] = {"path": "NOTES.md", "text": spec["writes"]}
        if spec.get("delay"):
            turn["delay_before"] = spec["delay"]
        if spec.get("barrier"):
            turn["barrier"] = {"dir": spec["barrier"], "size": 2, "timeout": 30}
        if spec.get("fail"):
            turn["stop"] = "refusal"
        script = {"turns": [turn], "record_path": str(self.records[name])}
        return json.dumps(
            [sys.executable, str(FAKE_AGENT), "--script", json.dumps(script)]
        )

    def set_agent(self, name: str, **spec) -> None:
        """Replace one agent's scripted behaviour.

        :param name: the agent name.
        :param spec: ``writes``, ``delay``, ``barrier`` and/or ``fail``.
        """
        self.behaviour[name] = spec
        self.apply_agents()

    # --------------------------------------------------------------- store

    def install(self, name: str, text: str) -> None:
        """Write a recipe into the project store.

        :param name: the conduit name (and folder).
        :param text: the conduit.yaml contents.
        """
        path = self.work / ".atelier" / "conduits" / name
        path.mkdir(parents=True, exist_ok=True)
        (path / "conduit.yaml").write_text(text, encoding="utf-8")

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

    def workspaces_root(self) -> Path:
        """Return the directory every run's checkouts are created under.

        :returns: the path, which may not exist.
        """
        return self.work / ".atelier" / "workspaces"

    def flows(self) -> list[str]:
        """Return the ids of every top-level flow on disk.

        :returns: sorted flow directory names.
        """
        root = self.work / ".atelier" / "flows"
        return sorted(p.name for p in root.glob("*") if p.is_dir())

    def recipe_unchanged(self) -> bool:
        """Report whether the installed recipe still holds its agent names.

        :returns: True when no run rewrote the file on disk.
        """
        text = (
            self.work / ".atelier" / "conduits" / "pair" / "conduit.yaml"
        ).read_text(encoding="utf-8")
        return text == PAIR

    def source_untouched(self) -> None:
        """Assert the user's own checkout is exactly as it was."""
        assert (self.work / "NOTES.md").read_text(encoding="utf-8") == "original\n"
        assert git("rev-parse", "HEAD", cwd=self.work) == self.base
        assert git("status", "--porcelain", "--untracked-files=no", cwd=self.work) == ""


def _flat(text: str) -> str:
    """Return ``text`` with its line wrapping collapsed.

    :param text: captured output.
    :returns: the same words, single-spaced.
    """
    return " ".join(text.split())


@pytest.fixture
def project(tmp_path):
    """A repository with the two-writer recipe and three fake agents.

    :param tmp_path: pytest temp directory fixture.
    :returns: the :class:`Project`.
    """
    p = Project(tmp_path)
    p.install("pair", PAIR)
    p.set_agent("claude-code", writes=EDIT["claude-code"])
    p.set_agent("codex", writes=EDIT["codex"])
    return p


# ------------------------------------------------------- parallel isolation


def test_two_agents_edit_the_same_file_at_once_and_both_survive(project, tmp_path):
    """The headline: concurrent writers, one filename, two surviving results."""
    gate = tmp_path / "gate"
    project.set_agent("claude-code", writes=EDIT["claude-code"], barrier=str(gate))
    project.set_agent("codex", writes=EDIT["codex"], barrier=str(gate))

    done = project.cli(
        "run", "pair",
        "--worktree", "writer_a", "--worktree", "writer_b",
        "--input", f"brief={BRIEF}", "--hide-steps",
    )
    assert done.returncode == 0, done.stdout + done.stderr
    flow_id = project.flow_id(done)

    # Overlap in time is the barrier's doing: each agent blocks until the other
    # arrives, so a run that executed them one after another could not finish.
    assert len(list(gate.iterdir())) == 2

    saved = project.status(flow_id)["workspaces"]
    a, b = Path(saved["paths"]["writer_a"]), Path(saved["paths"]["writer_b"])
    assert a != b
    assert saved["base"] == project.base
    assert Path(saved["source"]).samefile(project.work)

    # Real bytes, in two directories, from one relative filename.
    assert (a / "NOTES.md").read_text(encoding="utf-8") == EDIT["claude-code"]
    assert (b / "NOTES.md").read_text(encoding="utf-8") == EDIT["codex"]
    for path, text in ((a, EDIT["claude-code"]), (b, EDIT["codex"])):
        assert git("status", "--porcelain", cwd=path) == "M NOTES.md"
        assert text.strip() in git("diff", project.base, "--", "NOTES.md", cwd=path)
    project.source_untouched()

    # The synthesis is told which directory each candidate is in.
    [compare] = project.prompts("gemini")
    assert f"workspace: {a}" in compare
    assert f"workspace: {b}" in compare
    assert "agent: harness:claude-code" in compare
    assert "agent: harness:codex" in compare


def test_the_checkouts_are_not_inside_the_flow_record(project):
    done = project.cli(
        "run", "pair", "--worktree", "writer_a",
        "--input", f"brief={BRIEF}", "--hide-steps",
    )
    assert done.returncode == 0, done.stdout + done.stderr
    flow_id = project.flow_id(done)
    path = Path(project.status(flow_id)["workspaces"]["paths"]["writer_a"])
    assert not path.is_relative_to(project.work / ".atelier" / "flows")


def test_removing_the_run_record_leaves_the_edits_alone(project):
    done = project.cli(
        "run", "pair", "--worktree", "writer_a",
        "--input", f"brief={BRIEF}", "--hide-steps",
    )
    flow_id = project.flow_id(done)
    path = Path(project.status(flow_id)["workspaces"]["paths"]["writer_a"])

    removed = project.cli("rm", flow_id, "--yes")
    assert removed.returncode == 0, removed.stdout + removed.stderr
    assert flow_id not in project.flows()
    assert (path / "NOTES.md").read_text(encoding="utf-8") == EDIT["claude-code"]


def test_an_unselected_task_keeps_the_shared_directory(project):
    done = project.cli(
        "run", "pair", "--worktree", "writer_a",
        "--input", f"brief={BRIEF}", "--hide-steps",
    )
    assert done.returncode == 0, done.stdout + done.stderr
    saved = project.status(project.flow_id(done))["workspaces"]
    assert list(saved["paths"]) == ["writer_a"]
    # writer_b had no checkout of its own, so it wrote where the run started.
    assert (project.work / "NOTES.md").read_text(encoding="utf-8") == EDIT["codex"]


def test_a_shell_task_can_be_isolated_too(tmp_path):
    p = Project(tmp_path)
    p.install("shell", SHELL)
    p.set_agent("claude-code", writes=EDIT["claude-code"])

    done = p.cli("run", "shell", "--worktree", "writer_a", "--worktree", "build",
                 "--hide-steps")
    assert done.returncode == 0, done.stdout + done.stderr
    saved = p.status(p.flow_id(done))["workspaces"]
    build = Path(saved["paths"]["build"])
    assert (build / "NOTES.md").read_text(encoding="utf-8") == "built\n"
    assert Path((build / "WHERE.txt").read_text(encoding="utf-8").strip()).samefile(build)
    assert (Path(saved["paths"]["writer_a"]) / "NOTES.md").read_text(
        encoding="utf-8"
    ) == EDIT["claude-code"]
    p.source_untouched()


def test_isolation_composes_with_choosing_the_agent(project):
    done = project.cli(
        "run", "pair",
        "--worktree", "writer_b", "--agent", "writer_b=claude-code",
        "--input", f"brief={BRIEF}", "--hide-steps",
    )
    assert done.returncode == 0, done.stdout + done.stderr
    payload = project.status(project.flow_id(done))
    assert payload["task_agents"]["writer_b"] == "harness:claude-code"
    path = Path(payload["workspaces"]["paths"]["writer_b"])
    assert (path / "NOTES.md").read_text(encoding="utf-8") == EDIT["claude-code"]
    assert project.recipe_unchanged()


def test_a_nested_conduit_does_not_inherit_the_selection(tmp_path):
    """Same task name, different recipe: the child runs where the run does."""
    p = Project(tmp_path)
    p.install("parent", PARENT)
    p.install("child", CHILD)
    p.set_agent("claude-code", writes=EDIT["claude-code"])
    p.set_agent("codex", writes=EDIT["codex"])

    done = p.cli("run", "parent", "--worktree", "writer_a", "--hide-steps")
    assert done.returncode == 0, done.stdout + done.stderr
    saved = p.status(p.flow_id(done))["workspaces"]
    assert list(saved["paths"]) == ["writer_a"]
    assert (Path(saved["paths"]["writer_a"]) / "NOTES.md").read_text(
        encoding="utf-8"
    ) == EDIT["claude-code"]
    # The child's own writer_a wrote in the shared directory, untouched by the
    # parent's selector.
    assert (p.work / "NOTES.md").read_text(encoding="utf-8") == EDIT["codex"]


# ----------------------------------------------------------------- previews


def test_plan_shows_the_choice_and_creates_nothing(project):
    done = project.cli("plan", "pair", "--worktree", "writer_a", "--worktree", "writer_b")
    assert done.returncode == 0, done.stdout + done.stderr
    flat = _flat(done.stdout)
    assert "own checkout: writer_a, writer_b" in flat
    assert project.base[:12] in flat
    assert "not copied" in flat
    assert not project.workspaces_root().exists()
    assert project.flows() == []
    assert project.prompts("claude-code") == []


def test_plan_json_carries_the_same_answer(project):
    done = project.cli(
        "plan", "pair", "--json", "--worktree", "writer_a"
    )
    assert done.returncode == 0, done.stdout + done.stderr
    payload = json.loads(done.stdout)
    assert payload["isolation"]["tasks"] == ["writer_a"]
    assert payload["isolation"]["base"] == project.base
    assert payload["isolation"]["problem"] is None
    flat = {t["name"]: t["isolated"] for wave in payload["waves"] for t in wave}
    assert flat == {"writer_a": True, "writer_b": False, "compare": False}


def test_plan_reports_a_source_that_would_refuse_the_run(project):
    (project.work / "NOTES.md").write_text("dirty\n", encoding="utf-8")
    done = project.cli("plan", "pair", "--json", "--worktree", "writer_a")
    assert done.returncode == 0, done.stdout + done.stderr
    problem = json.loads(done.stdout)["isolation"]["problem"]
    assert "uncommitted changes" in problem and "NOTES.md" in problem


# ---------------------------------------------------------------- refusals


@pytest.mark.parametrize(
    ("args", "code", "phrase"),
    [
        (["--worktree", "writer_z"], 1, "has no task 'writer_z'"),
        (["--worktree", "writer_a", "--worktree", "writer_a"], 2, "twice"),
        (["--worktree", ""], 2, "expected a task name"),
        (["--worktree", "compare=/tmp/x"], 2, "task name only"),
    ],
)
def test_a_bad_selection_costs_nothing(project, args, code, phrase):
    done = project.cli("run", "pair", *args, "--input", f"brief={BRIEF}", "--hide-steps")
    assert done.returncode == code, done.stdout + done.stderr
    assert phrase in _flat(done.stdout + done.stderr)
    assert project.flows() == []
    assert not project.workspaces_root().exists()
    assert project.prompts("claude-code") == []


def test_a_human_step_cannot_be_given_a_checkout(tmp_path):
    p = Project(tmp_path)
    p.install("gated", GATED)
    done = p.cli("run", "gated", "--worktree", "sign_off", "--hide-steps")
    assert done.returncode == 1, done.stdout + done.stderr
    assert "no working directory of its own" in _flat(done.stdout + done.stderr)
    assert p.flows() == []


def test_uncommitted_work_stops_the_run_before_any_agent(project):
    (project.work / "NOTES.md").write_text("half a thought\n", encoding="utf-8")
    done = project.cli(
        "run", "pair", "--worktree", "writer_a",
        "--input", f"brief={BRIEF}", "--hide-steps",
    )
    assert done.returncode == 1, done.stdout + done.stderr
    flat = _flat(done.stdout + done.stderr)
    assert "uncommitted changes" in flat and "NOTES.md" in flat
    assert project.prompts("claude-code") == []
    assert (project.work / "NOTES.md").read_text(encoding="utf-8") == "half a thought\n"


def test_outside_a_repository_the_run_says_so(project, tmp_path):
    plain = tmp_path / "not a repo"
    (plain / ".atelier" / "conduits" / "pair").mkdir(parents=True)
    (plain / ".atelier" / "conduits" / "pair" / "conduit.yaml").write_text(
        PAIR, encoding="utf-8"
    )
    project.work = plain

    done = project.cli("run", "pair", "--worktree", "writer_a",
                       "--input", f"brief={BRIEF}", "--hide-steps")
    assert done.returncode == 1, done.stdout + done.stderr
    assert "not inside a Git repository" in _flat(done.stdout + done.stderr)
    assert project.prompts("claude-code") == []


# ------------------------------------------------------------------ resume


def _fail_writer_b(project):
    """Run the pair with writer_b failing after it has edited its checkout.

    :param project: the :class:`Project` to run in.
    :returns: ``(flow_id, writer_a path, writer_b path)``.
    """
    # The delay is what makes the failure deterministic: writer_a is recorded
    # complete well before writer_b refuses, so fail-fast has nothing in flight
    # to cancel and the resume has one finished sibling to keep.
    project.set_agent("codex", writes=EDIT["codex"], fail=True, delay=3)
    done = project.cli(
        "run", "pair", "--worktree", "writer_a", "--worktree", "writer_b",
        "--input", f"brief={BRIEF}", "--hide-steps",
    )
    assert done.returncode == 1, done.stdout + done.stderr
    flow_id = project.flow_id(done)
    saved = project.status(flow_id)["workspaces"]["paths"]
    return flow_id, Path(saved["writer_a"]), Path(saved["writer_b"])


def test_a_failed_worker_leaves_its_partial_edit_where_it_is(project):
    flow_id, a, b = _fail_writer_b(project)
    payload = project.status(flow_id)
    assert payload["tasks"]["writer_a"]["status"] == "completed"
    assert payload["tasks"]["writer_b"]["status"] == "failed"
    assert payload["tasks"]["compare"]["status"] != "completed"
    # The edit it managed before failing is still in its own checkout.
    assert (b / "NOTES.md").read_text(encoding="utf-8") == EDIT["codex"]
    assert (a / "NOTES.md").read_text(encoding="utf-8") == EDIT["claude-code"]
    project.source_untouched()


def test_resume_continues_in_the_same_checkout_without_redoing_the_sibling(project):
    flow_id, a, b = _fail_writer_b(project)
    # Something only this directory has, to prove the resume went back to it
    # rather than cutting a replacement.
    (b / "HALF_DONE.txt").write_text("in progress\n", encoding="utf-8")
    project.set_agent("codex", writes="# second attempt\n")

    again = project.cli("run", "--resume", flow_id, "--hide-steps")
    assert again.returncode == 0, again.stdout + again.stderr

    saved = project.status(flow_id)["workspaces"]["paths"]
    assert Path(saved["writer_b"]) == b
    assert (b / "HALF_DONE.txt").read_text(encoding="utf-8") == "in progress\n"
    assert (b / "NOTES.md").read_text(encoding="utf-8") == "# second attempt\n"
    # The finished sibling was not prompted a second time.
    assert len(project.prompts("claude-code")) == 1
    assert (a / "NOTES.md").read_text(encoding="utf-8") == EDIT["claude-code"]
    assert project.status(flow_id)["status"] == "completed"


def test_resume_composes_with_replacing_the_failed_agent(project):
    flow_id, _a, b = _fail_writer_b(project)
    project.set_agent("gemini", writes=EDIT["gemini"])

    again = project.cli(
        "run", "--resume", flow_id, "--agent", "writer_b=gemini", "--hide-steps"
    )
    assert again.returncode == 0, again.stdout + again.stderr
    assert (b / "NOTES.md").read_text(encoding="utf-8") == EDIT["gemini"]
    payload = project.status(flow_id)
    assert payload["task_agents"]["writer_b"] == "harness:gemini"
    assert payload["workspaces"]["paths"]["writer_b"] == str(b)


def test_resume_refuses_a_new_selection_rather_than_ignoring_it(project):
    flow_id, _a, _b = _fail_writer_b(project)
    done = project.cli("run", "--resume", flow_id, "--worktree", "compare")
    assert done.returncode == 2, done.stdout + done.stderr
    flat = _flat(done.stdout + done.stderr)
    assert "--resume and --worktree are mutually exclusive" in flat
    assert project.status(flow_id)["tasks"]["writer_b"]["status"] == "failed"


def test_resume_refuses_when_the_unfinished_checkout_is_gone(project):
    flow_id, _a, b = _fail_writer_b(project)
    git("worktree", "remove", "--force", str(b), cwd=project.work)
    before = len(project.prompts("codex"))

    done = project.cli("run", "--resume", flow_id, "--hide-steps")
    assert done.returncode == 1, done.stdout + done.stderr
    flat = _flat(done.stdout + done.stderr)
    assert "that directory is gone" in flat
    assert "--again" in flat
    # No substitute was made and no agent was asked anything.
    assert not b.exists()
    assert len(project.prompts("codex")) == before
    assert project.status(flow_id)["tasks"]["writer_a"]["status"] == "completed"


def test_resume_refuses_when_the_recipe_no_longer_has_the_task(project):
    flow_id, _a, _b = _fail_writer_b(project)
    project.install("pair", PAIR.replace("writer_b", "writer_c"))

    done = project.cli("run", "--resume", flow_id, "--hide-steps")
    assert done.returncode == 1, done.stdout + done.stderr
    assert "saved worktrees" in _flat(done.stdout + done.stderr)


# ------------------------------------------------------------------- again


def test_again_makes_new_checkouts_and_leaves_the_old_ones(project):
    first = project.cli(
        "run", "pair", "--worktree", "writer_a", "--worktree", "writer_b",
        "--input", f"brief={BRIEF}", "--hide-steps",
    )
    assert first.returncode == 0, first.stdout + first.stderr
    old_id = project.flow_id(first)
    old = {k: Path(v) for k, v in project.status(old_id)["workspaces"]["paths"].items()}
    (old["writer_a"] / "KEEP.txt").write_text("mine\n", encoding="utf-8")

    # A commit after the first run: the fresh run bases on the source's HEAD
    # as it is now, not on the old run's base.
    (project.work / "NOTES.md").write_text("moved on\n", encoding="utf-8")
    git("commit", "-qam", "second", cwd=project.work)
    moved_on = git("rev-parse", "HEAD", cwd=project.work)

    second = project.cli("run", "--again", old_id, "--hide-steps")
    assert second.returncode == 0, second.stdout + second.stderr
    new_id = project.flow_id(second)
    assert new_id != old_id
    new = project.status(new_id)["workspaces"]
    assert new["base"] == moved_on != project.base
    assert sorted(new["paths"]) == ["writer_a", "writer_b"]
    for name, path in new["paths"].items():
        assert Path(path) != old[name]

    # Nothing of the first run moved.
    assert (old["writer_a"] / "KEEP.txt").read_text(encoding="utf-8") == "mine\n"
    assert project.status(old_id)["workspaces"]["paths"]["writer_a"] == str(
        old["writer_a"]
    )


@pytest.mark.parametrize("damage", ["one_path", "path_and_selection"])
def test_again_refuses_an_incomplete_record_rather_than_inheriting_less(
    project, damage
):
    """A rerun inherits the policy from this record, so it has to trust it.

    Read as written, a mapping that has lost writer_b would isolate only
    writer_a and quietly send the other writer into the user's checkout.
    """
    flow_id, a, _b = _fail_writer_b(project)
    progress = _damage(project, flow_id, damage, a)
    before = progress.read_bytes()
    prompts = {name: len(project.prompts(name)) for name in AGENTS}
    flows = project.flows()

    done = project.cli("run", "--again", flow_id, "--hide-steps")
    assert done.returncode == 1, done.stdout + done.stderr
    assert "no longer says where writer_b worked" in _flat(done.stdout + done.stderr)

    # No new flow, no new checkout, no prompt, and the old record untouched.
    assert project.flows() == flows
    assert sorted(p.name for p in project.workspaces_root().iterdir()) == [flow_id]
    assert all(len(project.prompts(name)) == n for name, n in prompts.items())
    assert progress.read_bytes() == before
    project.source_untouched()


def test_deleting_the_same_run_twice_still_leaves_the_edits(project):
    """The checkouts share their run's name; they are not a flow record.

    A second deletion — the API after the CLI, a prune after an `rm` — must not
    find ``workspaces/<flow_id>`` in place of the record it just removed.
    """
    done = project.cli(
        "run", "pair", "--worktree", "writer_a",
        "--input", f"brief={BRIEF}", "--hide-steps",
    )
    assert done.returncode == 0, done.stdout + done.stderr
    flow_id = project.flow_id(done)
    path = Path(project.status(flow_id)["workspaces"]["paths"]["writer_a"])

    assert project.cli("rm", flow_id, "--yes").returncode == 0
    script = (
        "from flow_atelier.core.atelier import Atelier\n"
        f"print(Atelier().delete_flow({flow_id!r}))\n"
    )
    repeat = subprocess.run(
        [sys.executable, "-B", "-c", script],
        cwd=str(project.work),
        env=project.env,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
    )
    assert repeat.returncode == 0, repeat.stdout + repeat.stderr
    assert repeat.stdout.strip() == "False"
    assert (path / "NOTES.md").read_text(encoding="utf-8") == EDIT["claude-code"]


def test_again_can_add_a_task_to_the_inherited_policy(project):
    first = project.cli(
        "run", "pair", "--worktree", "writer_a",
        "--input", f"brief={BRIEF}", "--hide-steps",
    )
    old_id = project.flow_id(first)
    git("checkout", "-q", "--", "NOTES.md", cwd=project.work)

    second = project.cli("run", "--again", old_id, "--worktree", "writer_b", "--hide-steps")
    assert second.returncode == 0, second.stdout + second.stderr
    paths = project.status(project.flow_id(second))["workspaces"]["paths"]
    assert sorted(paths) == ["writer_a", "writer_b"]


# ------------------------------------------------- setup and interruption


def test_a_run_that_cannot_record_itself_keeps_what_it_built(project):
    """The checkouts exist before the flow does, and are not tidied away."""
    flows = project.work / ".atelier" / "flows"
    flows.mkdir(parents=True, exist_ok=True)
    flows.chmod(0o500)
    try:
        done = project.cli(
            "run", "pair", "--worktree", "writer_a",
            "--input", f"brief={BRIEF}", "--hide-steps",
        )
    finally:
        flows.chmod(0o700)
    assert done.returncode == 1, done.stdout + done.stderr
    assert "flow failed" in done.stdout
    assert project.flows() == []
    assert project.prompts("claude-code") == []
    # One directory per selected task, still there, cut from the pinned commit.
    [run_dir] = list(project.workspaces_root().iterdir())
    assert git("rev-parse", "HEAD", cwd=run_dir / "writer_a") == project.base
    project.source_untouched()


def test_stopping_a_run_leaves_every_checkout_in_place(project):
    """SIGTERM mid-flight: no false success, and nothing is cleaned up."""
    project.set_agent("claude-code", writes=EDIT["claude-code"], delay=6)
    project.set_agent("codex", writes=EDIT["codex"], delay=6)
    run = subprocess.Popen(
        [
            sys.executable, "-c", CLI, "run", "pair",
            "--worktree", "writer_a", "--worktree", "writer_b",
            "--input", f"brief={BRIEF}", "--hide-steps",
        ],
        cwd=project.work, env=project.env, stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )
    def prompted(name: str) -> bool:
        try:
            return bool(project.prompts(name))
        except ValueError:  # a prompt line still being written
            return False

    try:
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            dirs = sorted(project.workspaces_root().rglob("writer_*"))
            # Both agents prompted means the engine is running and handles
            # SIGTERM; the checkouts alone exist before the flow does.
            if (
                len(dirs) == 2
                and all((d / "NOTES.md").exists() for d in dirs)
                and prompted("claude-code")
                and prompted("codex")
            ):
                break
            time.sleep(0.1)
        else:  # pragma: no cover - only on a hung fixture
            run.kill()
            raise AssertionError("the checkouts were never created")
        run.send_signal(signal.SIGTERM)
        out = run.communicate(timeout=60)[0]
    finally:
        if run.poll() is None:  # pragma: no cover - only on a hung fixture
            run.kill()

    assert "flow stopped" in out, out
    flow_id = [line for line in out.splitlines() if "flow_id:" in line][0]
    flow_id = flow_id.split(":", 1)[1].strip()
    payload = project.status(flow_id)
    assert payload["status"] != "completed"
    for name, path in payload["workspaces"]["paths"].items():
        assert (Path(path) / "NOTES.md").exists(), name
        assert git("rev-parse", "HEAD", cwd=path) == project.base
    project.source_untouched()


# --------------------------------------------------------- downstream context


def test_a_synthesis_is_told_where_each_candidate_lives(project):
    """No template reference in the recipe, and it still knows the paths."""
    project.install("panel", PANEL)
    done = project.cli(
        "run", "panel", "--worktree", "writer_a", "--worktree", "writer_b",
        "--hide-steps",
    )
    assert done.returncode == 0, done.stdout + done.stderr
    paths = project.status(project.flow_id(done))["workspaces"]["paths"]

    [merged] = project.prompts("gemini")
    assert "BEGIN WORKSPACE PROVENANCE (authoritative)" in merged
    assert f"writer_a worked in {paths['writer_a']}" in merged
    assert f"writer_b worked in {paths['writer_b']}" in merged
    assert f"at commit {project.base}" in merged
    # The author's own text is untouched, not rewritten.
    assert "--- BEGIN RESULT FROM writer_a ---" in merged


def test_an_ordinary_run_gains_no_such_block(project):
    project.install("panel", PANEL)
    done = project.cli("run", "panel", "--hide-steps")
    assert done.returncode == 0, done.stdout + done.stderr
    [merged] = project.prompts("gemini")
    assert "WORKSPACE PROVENANCE" not in merged


# ------------------------------------------------ damaged recovery metadata


def _damage(project, flow_id, damage, a):
    """Rewrite one run's saved workspace record the way a corruption would.

    :param project: the :class:`Project` holding the flow.
    :param flow_id: the flow whose record to damage.
    :param damage: which way to break it.
    :param a: the finished sibling's checkout, for the retargeting cases.
    :returns: the progress file's path.
    """
    progress = project.work / ".atelier" / "flows" / flow_id / "progress.json"
    payload = json.loads(progress.read_text(encoding="utf-8"))
    spaces = payload["workspaces"]
    if damage == "source_checkout":
        spaces["paths"]["writer_b"] = str(project.work)
    elif damage == "sibling_checkout":
        spaces["paths"]["writer_b"] = str(a)
    elif damage == "no_paths":
        del spaces["paths"]
    elif damage == "one_path":
        del spaces["paths"]["writer_b"]
    else:
        del spaces["paths"]["writer_b"]
        spaces["selected"] = ["writer_a"]
    progress.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return progress


DAMAGE = [
    "source_checkout",
    "sibling_checkout",
    "no_paths",
    "one_path",
    "path_and_selection",
]


@pytest.mark.parametrize("damage", DAMAGE)
def test_resume_refuses_a_workspace_record_it_does_not_own(project, damage):
    """A retargeted, gutted or incomplete record must stop the resume.

    The first three name a directory the run never created: the user's own
    checkout, the finished sibling's, or none at all. The last two have *lost*
    writer_b, which is worse than naming the wrong place — an unlisted task
    looks like one that was never isolated, so it would be run in the shared
    source checkout. Honouring any of them overwrites files the resume exists to
    protect, so the run has to refuse before it prompts anyone.
    """
    flow_id, a, b = _fail_writer_b(project)
    progress = _damage(project, flow_id, damage, a)
    before = progress.read_bytes()
    prompts = {name: len(project.prompts(name)) for name in AGENTS}
    project.set_agent("codex", writes="RESUMED IN THE WRONG PLACE\n")

    done = project.cli("run", "--resume", flow_id, "--hide-steps")
    assert done.returncode == 1, done.stdout + done.stderr
    # The advice has to be a command that works: --again reads this same
    # record, so it names a fresh run of the recipe instead.
    assert "--worktree" in _flat(done.stdout + done.stderr)

    # Nothing was started, nothing was rewritten, nothing was overwritten.
    assert all(len(project.prompts(name)) == n for name, n in prompts.items())
    assert progress.read_bytes() == before
    assert (a / "NOTES.md").read_text(encoding="utf-8") == EDIT["claude-code"]
    assert (b / "NOTES.md").read_text(encoding="utf-8") == EDIT["codex"]
    project.source_untouched()


def test_the_facade_refuses_the_same_record_without_the_cli(project):
    """The guard is in the API, so calling it directly cannot get past it."""
    flow_id, _a, b = _fail_writer_b(project)
    progress = project.work / ".atelier" / "flows" / flow_id / "progress.json"
    payload = json.loads(progress.read_text(encoding="utf-8"))
    payload["workspaces"]["paths"]["writer_b"] = str(project.work)
    progress.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    script = (
        "import asyncio, os\n"
        "from flow_atelier.core.atelier import Atelier\n"
        "from flow_atelier.modules.workspace import WorkspaceError\n"
        f"os.chdir({str(project.work)!r})\n"
        "try:\n"
        f"    asyncio.run(Atelier().resume_flow({flow_id!r}, show_steps=False))\n"
        "except WorkspaceError as exc:\n"
        "    print('refused:', exc)\n"
        "else:\n"
        "    raise SystemExit('the facade resumed a record it does not own')\n"
    )
    done = subprocess.run(
        [sys.executable, "-c", script],
        cwd=str(project.work),
        env=project.env,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
    )
    assert done.returncode == 0, done.stdout + done.stderr
    assert "refused:" in done.stdout
    assert (b / "NOTES.md").read_text(encoding="utf-8") == EDIT["codex"]
    project.source_untouched()


def _lose_record(project, flow_id, how):
    """Drop a run's whole workspace record, as a hand edit or bad write would.

    :param project: the :class:`Project` holding the flow.
    :param flow_id: the flow whose record to drop.
    :param how: ``removed`` deletes the key, ``null`` empties it.
    :returns: the progress file's path.
    """
    progress = project.work / ".atelier" / "flows" / flow_id / "progress.json"
    payload = json.loads(progress.read_text(encoding="utf-8"))
    if how == "removed":
        del payload["workspaces"]
    else:
        payload["workspaces"] = None
    progress.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return progress


@pytest.mark.parametrize("how", ["removed", "null"])
@pytest.mark.parametrize("command", ["--resume", "--again"])
def test_a_lost_record_is_not_read_as_a_shared_directory_run(project, how, command):
    """No record looks like an older shared run; the checkouts on disk say not.

    Read as legacy, the resume or rerun would send both writers into the
    user's checkout. The run's own checkout directories still exist, and that
    is enough to refuse before anyone is prompted or anything is written.
    """
    flow_id, a, b = _fail_writer_b(project)
    progress = _lose_record(project, flow_id, how)
    before = progress.read_bytes()
    prompts = {name: len(project.prompts(name)) for name in AGENTS}
    flows = project.flows()

    done = project.cli("run", command, flow_id, "--hide-steps")
    assert done.returncode == 1, done.stdout + done.stderr
    flat = _flat(done.stdout + done.stderr)
    assert "the record is gone but their checkouts are still in" in flat
    assert "--worktree" in flat

    assert project.flows() == flows
    assert all(len(project.prompts(name)) == n for name, n in prompts.items())
    assert progress.read_bytes() == before
    assert (a / "NOTES.md").read_text(encoding="utf-8") == EDIT["claude-code"]
    assert (b / "NOTES.md").read_text(encoding="utf-8") == EDIT["codex"]
    project.source_untouched()


@pytest.mark.parametrize("target", ["source", "sibling"])
def test_a_checkout_replaced_by_a_link_is_not_followed(project, target):
    """The record is untouched; the directory it names now points elsewhere."""
    flow_id, a, b = _fail_writer_b(project)
    moved = b.with_name("writer_b.moved")
    b.rename(moved)
    b.symlink_to(project.work if target == "source" else a, target_is_directory=True)
    prompts = {name: len(project.prompts(name)) for name in AGENTS}
    project.set_agent("codex", writes="RESUMED IN THE WRONG PLACE\n")

    done = project.cli("run", "--resume", flow_id, "--hide-steps")
    assert done.returncode == 1, done.stdout + done.stderr
    assert "replaced by a symbolic link" in _flat(done.stdout + done.stderr)

    assert all(len(project.prompts(name)) == n for name, n in prompts.items())
    assert (a / "NOTES.md").read_text(encoding="utf-8") == EDIT["claude-code"]
    assert (moved / "NOTES.md").read_text(encoding="utf-8") == EDIT["codex"]
    project.source_untouched()


def test_the_facade_refuses_a_lost_record_without_the_cli(project):
    """Resume and rerun through the API hit the same guard as the CLI."""
    flow_id, _a, b = _fail_writer_b(project)
    _lose_record(project, flow_id, "removed")

    script = (
        "import asyncio, os\n"
        "from flow_atelier.core.atelier import Atelier\n"
        "from flow_atelier.modules.workspace import WorkspaceError\n"
        f"os.chdir({str(project.work)!r})\n"
        "for call in ('resume_flow', 'rerun_flow'):\n"
        "    try:\n"
        f"        asyncio.run(getattr(Atelier(), call)({flow_id!r}, show_steps=False))\n"
        "    except WorkspaceError as exc:\n"
        "        print(call, 'refused:', exc)\n"
        "    else:\n"
        "        raise SystemExit(call + ' ran a run whose record was lost')\n"
    )
    done = subprocess.run(
        [sys.executable, "-c", script],
        cwd=str(project.work),
        env=project.env,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
    )
    assert done.returncode == 0, done.stdout + done.stderr
    assert "resume_flow refused:" in done.stdout
    assert "rerun_flow refused:" in done.stdout
    assert (b / "NOTES.md").read_text(encoding="utf-8") == EDIT["codex"]
    project.source_untouched()


@pytest.mark.parametrize("stand_in", ["another repository", "no repository"])
def test_again_keeps_the_recorded_repository_when_started_elsewhere(
    project, tmp_path, stand_in
):
    """`--again` repeats the brief on the same code, wherever it is invoked."""
    first = project.cli(
        "run", "pair", "--worktree", "writer_a", "--worktree", "writer_b",
        "--input", f"brief={BRIEF}", "--hide-steps",
    )
    assert first.returncode == 0, first.stdout + first.stderr
    old_id = project.flow_id(first)
    old = project.status(old_id)["workspaces"]
    progress = project.work / ".atelier" / "flows" / old_id / "progress.json"
    outputs = project.work / ".atelier" / "flows" / old_id / "outputs.yaml"
    before = (progress.read_bytes(), outputs.read_bytes())

    # A commit in the source after the first run: the fresh checkouts are cut
    # from its HEAD as it is now, and that HEAD is read in the source, not here.
    git("checkout", "-q", "--", "NOTES.md", cwd=project.work)
    (project.work / "LATER.md").write_text("later\n", encoding="utf-8")
    git("add", "-A", cwd=project.work)
    git("commit", "-qm", "later", cwd=project.work)
    moved_on = git("rev-parse", "HEAD", cwd=project.work)

    # Same store, somewhere else to stand: an unrelated repository, or a plain
    # directory that is no repository at all.
    source = project.work
    project.env["ATELIER_ATELIER_DIR"] = str(source / ".atelier")
    if stand_in == "another repository":
        elsewhere = Project(tmp_path / "another")
        project.work = elsewhere.work
    else:
        elsewhere = None
        project.work = tmp_path / "just a folder"
        project.work.mkdir()

    second = project.cli("run", "--again", old_id, "--hide-steps")
    assert second.returncode == 0, second.stdout + second.stderr
    new = project.status(project.flow_id(second))["workspaces"]
    assert Path(new["source"]).samefile(source)
    assert new["base"] == moved_on != old["base"]
    assert sorted(new["paths"]) == ["writer_a", "writer_b"]
    assert new["paths"] != old["paths"]
    for path in new["paths"].values():
        assert (Path(path) / "LATER.md").exists()

    # The first run's record and results are byte-identical, and the directory
    # the command was typed in was left completely alone.
    assert (progress.read_bytes(), outputs.read_bytes()) == before
    assert project.status(old_id)["workspaces"]["paths"] == old["paths"]
    if elsewhere is not None:
        elsewhere.source_untouched()
        assert elsewhere.flows() == []
    else:
        assert list(project.work.iterdir()) == []


def test_again_refuses_when_the_recorded_repository_is_gone(project, tmp_path):
    """No silent substitute: a missing source stops the rerun before prompts."""
    # A store outside the repository, so the flow record and the checkouts
    # outlive the source directory itself.
    store = tmp_path / "outside store"
    recipe = store / "conduits" / "pair"
    recipe.mkdir(parents=True)
    (recipe / "conduit.yaml").write_text(PAIR, encoding="utf-8")
    project.env["ATELIER_ATELIER_DIR"] = str(store)

    first = project.cli(
        "run", "pair", "--worktree", "writer_a",
        "--input", f"brief={BRIEF}", "--hide-steps",
    )
    assert first.returncode == 0, first.stdout + first.stderr
    old_id = project.flow_id(first)
    shutil.rmtree(project.work)

    elsewhere = Project(tmp_path / "another")
    project.work = elsewhere.work
    prompts = len(project.prompts("claude-code"))

    done = project.cli("run", "--again", old_id, "--hide-steps")
    assert done.returncode == 1, done.stdout + done.stderr
    flat = _flat(done.stdout + done.stderr)
    assert "saved worktrees" in flat
    assert "does not exist" in flat
    # The repository the command was typed in was neither used nor written to.
    assert len(project.prompts("claude-code")) == prompts
    assert elsewhere.flows() == []
    elsewhere.source_untouched()


def test_a_flow_recorded_before_checkouts_existed_still_resumes(project):
    """Older records have no `workspaces` key at all; they share a directory."""
    project.set_agent("codex", writes=EDIT["codex"], fail=True, delay=3)
    first = project.cli(
        "run", "pair", "--input", f"brief={BRIEF}", "--hide-steps"
    )
    assert first.returncode == 1, first.stdout + first.stderr
    flow_id = project.flow_id(first)
    progress = project.work / ".atelier" / "flows" / flow_id / "progress.json"
    payload = json.loads(progress.read_text(encoding="utf-8"))
    del payload["workspaces"]
    progress.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    project.set_agent("codex", writes="# second attempt\n")

    done = project.cli("run", "--resume", flow_id, "--hide-steps")
    assert done.returncode == 0, done.stdout + done.stderr
    payload = project.status(flow_id)
    assert payload["status"] == "completed"
    assert payload["workspaces"] is None
    # One shared directory, as it always was: the source itself.
    assert (project.work / "NOTES.md").read_text(encoding="utf-8") == "# second attempt\n"
    assert len(project.prompts("claude-code")) == 1
