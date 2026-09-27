"""`atelier check --probe` against real agent processes.

Every `atelier` call is its own process, the ACP transport is real, and each
agent is a scripted fake that records the protocol calls it is served. What
the tests read back is what a user would — the terminal report, the JSON
document, the exit status — plus the agents' own logs, which is where the
claims about *what the probe did* are settled: no prompt is ever sent, each
configuration is started exactly once, and the session is opened in the
directory a run would use.

Four agents on purpose: `claude-code` and `codex` through their launch-command
overrides, `gemini` through a registry name overridden by ``ATELIER_HARNESSES``,
and `housebot` as a custom agent the registry has never heard of. The fixtures
establish routing, transport and process lifecycle. They say nothing about
live-provider authentication or model quality.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from tests._shell import CLI

FAKE_AGENT = Path(__file__).resolve().parents[1] / "fixtures" / "fake_acp_agent.py"

AGENTS = ("claude-code", "codex", "gemini", "housebot")

# A launcher that starts, announces itself and never speaks ACP: the agent
# whose CLI is installed but whose ACP entry point is wrong.
NOT_ACP = "import sys; sys.stdout.write('this is not ACP\\n'); sys.stdout.flush()"

# A launcher that records its pid and then hangs: the agent that accepts a
# connection and never answers, which is what `--timeout` is for.
HANGS = (
    "import os, sys, pathlib, time; "
    "pathlib.Path(sys.argv[1]).write_text(str(os.getpid())); "
    "time.sleep(300)"
)

TEAM = """\
name: team
description: four agents, one of them behind a condition
inputs:
  brief:
    description: the shared task
tasks:
  - name: step_1
    description: draft
    task: "Draft: {{inputs.brief}}"
    tool: harness:claude-code
    depends_on: []
  - name: step_2
    description: review
    task: "Review: {{step_1.output}}"
    tool: harness:codex
    depends_on: [step_1]
  - name: step_3
    description: only when the draft says so
    task: "Ship it: {{step_1.output}}"
    tool: harness:gemini
    depends_on: ["step_1.output.match(SHIP)"]
  - name: wrap
    description: summarize
    task: "Summarize: {{step_2.output}}"
    tool: harness:housebot
    depends_on: [step_2]
"""

PARENT = """\
name: parent
description: calls the same child twice and keeps one agent of its own
tasks:
  - name: call_a
    description: first call
    task: kid
    tool: tool:conduit
    depends_on: []
  - name: call_b
    description: second call
    task: kid
    tool: tool:conduit
    depends_on: [call_a]
  - name: step_1
    description: the parent's own agent task
    task: "Parent work"
    tool: harness:claude-code
    depends_on: [call_b]
"""

KID = """\
name: kid
description: a child whose task happens to share the parent's task name
tasks:
  - name: step_1
    description: the child's own agent task
    task: "Child work"
    tool: harness:codex
    depends_on: []
"""

MODELS = """\
name: models
description: one agent, three selections
tasks:
  - name: a
    description: first
    task: "a"
    tool: harness:codex:m1
    depends_on: []
  - name: b
    description: second
    task: "b"
    tool: harness:codex:m1
    depends_on: []
  - name: c
    description: third
    task: "c"
    tool: harness:codex:m2
    depends_on: []
"""

BASH_ONLY = """\
name: chore
description: no agent anywhere
tasks:
  - name: one
    description: shell
    task: echo hi
    tool: tool:bash
    depends_on: []
"""


class Team:
    """A project whose four agents are scripted fake ACP processes.

    :param root: directory holding the project, the global store and the logs.
    """

    def __init__(self, root: Path) -> None:
        self.root = root
        self.work = root / "agent team"
        (self.work / ".atelier" / "conduits").mkdir(parents=True)
        (root / "global" / "conduits").mkdir(parents=True)
        self.records = {name: root / f"{name}-log" for name in AGENTS}
        for path in self.records.values():
            path.mkdir()
        self.env = {k: v for k, v in os.environ.items() if not k.startswith("ATELIER_")}
        self.env["ATELIER_GLOBAL_ATELIER_DIR"] = str(root / "global")
        self.env["ATELIER_NO_UPDATE_CHECK"] = "1"
        # Per-agent script additions (models on offer, a refused session) and
        # whole-launcher replacements (a missing binary, a hang).
        self.extras: dict[str, dict] = {}
        self.launchers: dict[str, list[str]] = {}
        self.apply()

    def _argv(self, name: str) -> str:
        """Return one agent's launch argv as JSON.

        :param name: the agent name.
        :returns: the JSON argv for a launch-command environment variable.
        """
        if name in self.launchers:
            return json.dumps(self.launchers[name])
        script = {
            "turns": [{"chunks": [f"{name} replied"]}],
            "record_path": str(self.records[name]),
            **self.extras.get(name, {}),
        }
        return json.dumps(
            [sys.executable, str(FAKE_AGENT), "--script", json.dumps(script)]
        )

    def apply(self) -> None:
        """Point every agent at its current launcher."""
        self.env["ATELIER_CLAUDE_LAUNCH_CMD"] = self._argv("claude-code")
        self.env["ATELIER_CODEX_LAUNCH_CMD"] = self._argv("codex")
        self.env["ATELIER_HARNESSES"] = json.dumps(
            {name: json.loads(self._argv(name)) for name in ("gemini", "housebot")}
        )

    def script(self, name: str, **keys: object) -> None:
        """Add script keys to one agent and re-apply the configuration.

        :param name: the agent name.
        :param keys: extra keys for the fake agent's script.
        """
        self.extras.setdefault(name, {}).update(keys)
        self.apply()

    def launch(self, name: str, argv: list[str]) -> None:
        """Replace one agent's whole launch command.

        :param name: the agent name.
        :param argv: the command line to run instead of the fake agent.
        """
        self.launchers[name] = argv
        self.apply()

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
        return (self.work / ".atelier" / "conduits" / name / "conduit.yaml").read_bytes()

    def events(self, name: str) -> list[dict]:
        """Return every ACP call one agent was served, across its processes.

        :param name: the agent name.
        :returns: the recorded protocol events, grouped by process.
        """
        return [
            json.loads(line)
            for log in sorted(self.records[name].glob("*.events"))
            for line in log.read_text(encoding="utf-8").splitlines()
        ]

    def sessions(self, name: str) -> list[str]:
        """Return the working directory of every session opened on one agent.

        :param name: the agent name.
        :returns: one cwd per ``session/new``, in order.
        """
        return [e["cwd"] for e in self.events(name) if e["event"] == "new_session"]

    def prompts(self, name: str) -> list[str]:
        """Return every prompt one agent received.

        :param name: the agent name.
        :returns: one joined text block per prompt call.
        """
        return [
            "\n".join(block.get("text", "") for block in json.loads(line))
            for log in sorted(self.records[name].glob("*.jsonl"))
            for line in log.read_text(encoding="utf-8").splitlines()
        ]

    def cli(self, *args: str, timeout: float = 120) -> subprocess.CompletedProcess:
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

    def report(self, result: subprocess.CompletedProcess) -> dict:
        """Parse a ``--json`` run's stdout and return the single row's probe.

        :param result: the finished process.
        :returns: the row's ``probe`` object.
        """
        rows = json.loads(result.stdout)
        assert len(rows) == 1, rows
        return rows[0]["probe"]

    def flows(self) -> list[str]:
        """Return the ids of every saved flow.

        :returns: sorted flow directory names; the store creates the folder
            itself, so what matters is that nothing was recorded in it.
        """
        root = self.work / ".atelier" / "flows"
        return sorted(p.name for p in root.glob("*")) if root.exists() else []

    def quiet(self) -> None:
        """Assert no agent was started and nothing was saved."""
        for name in AGENTS:
            assert self.events(name) == [], f"{name} was started"
        assert self.flows() == []


def _flat(text: str) -> str:
    """Return ``text`` with console line wrapping collapsed.

    :param text: captured output.
    :returns: the same words, single-spaced.
    """
    return " ".join(text.split())


def _by_tool(probe: dict) -> dict[str, dict]:
    """Index a probe report's agents by the tool they checked.

    :param probe: the ``probe`` object from a JSON report.
    :returns: tool string -> that agent's entry.
    """
    return {a["tool"]: a for a in probe["agents"]}


@pytest.fixture
def team(tmp_path):
    """A project with the four-agent recipe installed.

    :param tmp_path: pytest temp directory fixture.
    :returns: the :class:`Team`.
    """
    t = Team(tmp_path)
    t.install("team", TEAM)
    return t


# ------------------------------------------------------- the whole team, once


def test_the_whole_team_answers_and_no_agent_is_prompted(team):
    """Four providers, four sessions, zero prompts, one report."""
    before = team.recipe("team")
    result = team.cli("check", "team", "--probe", "--json", "--timeout", "30")
    assert result.returncode == 0, result.stdout + result.stderr

    probe = team.report(result)
    assert probe["ran"] is True and probe["ok"] is True
    assert probe["scope"] == "root"
    assert sorted(_by_tool(probe)) == [
        "harness:claude-code", "harness:codex", "harness:gemini", "harness:housebot"
    ]
    for entry in probe["agents"]:
        assert entry["stage"] == "ok"
        assert entry["agent"] == "fake-acp-agent 0.0.1"
        assert entry["elapsed_seconds"] >= 0

    # The agents' own logs: one handshake and one session each, no prompt.
    for name in AGENTS:
        kinds = [e["event"] for e in team.events(name)]
        assert kinds == ["initialize", "new_session"], (name, kinds)
        assert team.prompts(name) == []
        # The session opens where a run would work, not in a temp directory.
        assert team.sessions(name) == [str(team.work)]

    # Nothing was written: no flow, no rewritten recipe, no saved assignment.
    assert team.flows() == []
    assert team.recipe("team") == before


def test_every_affected_task_is_named_under_the_agent_that_answers(team):
    """Attribution is per task, and a conditional task is included and flagged."""
    result = team.cli("check", "team", "--probe", "--json", "--timeout", "30")
    agents = _by_tool(team.report(result))
    assert agents["harness:claude-code"]["tasks"] == [
        {"path": "team.step_1", "conditional": False}
    ]
    assert agents["harness:gemini"]["tasks"] == [
        {"path": "team.step_3", "conditional": True}
    ]
    assert agents["harness:housebot"]["tasks"] == [
        {"path": "team.wrap", "conditional": False}
    ]

    human = team.cli("check", "team", "--probe", "--timeout", "30")
    assert human.returncode == 0, human.stdout + human.stderr
    flat = _flat(human.stdout)
    assert "team.step_3 (conditional; a run may skip it)" in flat
    assert "4 agent task(s) on 4 configuration(s)" in flat
    assert "startup only" in flat


def test_one_agent_serving_two_tasks_is_started_once(team):
    """Two tasks, one configuration, one process."""
    team.install(
        "twice",
        TEAM.replace("name: team", "name: twice", 1).replace(
            "tool: harness:housebot", "tool: harness:claude-code"
        ),
    )
    result = team.cli("check", "twice", "--probe", "--json", "--timeout", "30")
    assert result.returncode == 0, result.stdout + result.stderr
    agents = _by_tool(team.report(result))
    assert [t["path"] for t in agents["harness:claude-code"]["tasks"]] == [
        "twice.step_1", "twice.wrap"
    ]
    assert len(team.sessions("claude-code")) == 1


def test_two_models_of_one_agent_are_two_checks(team):
    """A selection is the unit, so `codex:m1` and `codex:m2` both get started."""
    # The session opens on a model neither task names, so selecting `m1` is
    # a real request and not a no-op the client could skip.
    team.script(
        "codex",
        models={
            "current": "m0",
            "available": [
                {"id": "m0", "name": "Default"},
                {"id": "m1", "name": "One"},
                {"id": "m2", "name": "Two"},
            ],
        },
    )
    team.install("models", MODELS)
    result = team.cli("check", "models", "--probe", "--json", "--timeout", "30")
    assert result.returncode == 0, result.stdout + result.stderr

    agents = _by_tool(team.report(result))
    assert sorted(agents) == ["harness:codex:m1", "harness:codex:m2"]
    assert [t["path"] for t in agents["harness:codex:m1"]["tasks"]] == [
        "models.a", "models.b"
    ]
    # Two sessions, and each one selected the model its task names.
    assert len(team.sessions("codex")) == 2
    picked = [
        e["value"] for e in team.events("codex") if e["event"] == "set_config_option"
    ]
    assert sorted(picked) == ["m1", "m2"]


def test_a_refused_model_is_reported_as_the_selection_it_is(team):
    """The agent offers m1; the recipe asks for m9."""
    team.script(
        "codex",
        models={
            "current": "m0",
            "available": [{"id": "m0", "name": "Default"}, {"id": "m1", "name": "One"}],
        },
    )
    team.install("models", MODELS.replace("harness:codex:m2", "harness:codex:m9"))
    result = team.cli("check", "models", "--probe", "--json", "--timeout", "30")
    assert result.returncode == 1

    agents = _by_tool(team.report(result))
    assert agents["harness:codex:m1"]["ok"] is True
    bad = agents["harness:codex:m9"]
    assert bad["ok"] is False and bad["stage"] == "selection"
    assert "m9" in bad["detail"]
    assert any("atelier harness check codex" in line for line in bad["guidance"])
    # The refusal did not stop the check: the good selection still answered.
    assert team.report(result)["ok"] is False


# ------------------------------------------------------------ what goes wrong


def test_a_logged_out_agent_fails_the_team_and_names_the_login(team):
    """One refused session, collected alongside the agents that worked."""
    team.script("codex", fail_session="not logged in", auth_methods=[{"id": "oauth"}])
    result = team.cli("check", "team", "--probe", "--json", "--timeout", "30")
    assert result.returncode == 1

    probe = team.report(result)
    assert probe["ok"] is False
    agents = _by_tool(probe)
    assert agents["harness:codex"]["stage"] == "session"
    assert any("log in" in line for line in agents["harness:codex"]["guidance"])
    # Every other agent was still checked rather than abandoned.
    assert [a["ok"] for a in probe["agents"] if a["tool"] != "harness:codex"] == [
        True, True, True
    ]
    assert team.prompts("claude-code") == []


def test_an_agent_that_does_not_speak_acp_is_reported_as_such(team):
    """The command starts and the handshake never lands."""
    team.launch("gemini", [sys.executable, "-c", NOT_ACP])
    result = team.cli("check", "team", "--probe", "--json", "--timeout", "30")
    assert result.returncode == 1
    entry = _by_tool(team.report(result))["harness:gemini"]
    assert entry["ok"] is False
    assert entry["stage"] in ("initialize", "handshake", "spawn")
    assert any("did not speak ACP" in line for line in entry["guidance"])


def test_a_missing_executable_is_caught_before_anything_starts(team):
    """Readiness already answers this, so the probe never runs."""
    team.launch("housebot", [str(team.root / "no-such-agent-binary")])
    result = team.cli("check", "team", "--probe", "--json", "--timeout", "30")
    assert result.returncode == 1

    rows = json.loads(result.stdout)
    assert rows[0]["ok"] is False
    assert "no-such-agent-binary" in rows[0]["error"]
    assert rows[0]["probe"] == {
        "scope": "root",
        "ran": False,
        "ok": False,
        "reason": "the static check failed, so no agent was started",
        "agents": [],
    }
    team.quiet()


def test_a_replacement_rescues_an_agent_this_machine_cannot_run(team):
    """The unavailable default is replaced, so the check passes on merit."""
    team.launch("housebot", [str(team.root / "no-such-agent-binary")])
    blocked = team.cli("check", "team", "--probe", "--timeout", "30")
    assert blocked.returncode == 1

    rescued = team.cli(
        "check", "team", "--probe", "--agent", "wrap=gemini", "--timeout", "30"
    )
    assert rescued.returncode == 0, rescued.stdout + rescued.stderr
    assert "harness:housebot" not in rescued.stdout
    # gemini answered for its own task and for the one it took over.
    assert len(team.sessions("gemini")) == 1
    assert "team.wrap" in _flat(rescued.stdout)


@pytest.mark.skipif(os.name == "nt", reason="needs POSIX signal-0 pid probing")
def test_a_hanging_agent_is_bounded_and_its_process_is_reaped(team):
    """`--timeout` ends the wait, and nothing is left running behind it."""
    pidfile = team.root / "hung.pid"
    team.launch("gemini", [sys.executable, "-c", HANGS, str(pidfile)])
    started = time.monotonic()
    result = team.cli("check", "team", "--probe", "--json", "--timeout", "2")
    elapsed = time.monotonic() - started
    assert result.returncode == 1
    assert elapsed < 60, f"the bounded probe took {elapsed:.0f}s"

    entry = _by_tool(team.report(result))["harness:gemini"]
    assert entry["ok"] is False and entry["stage"] == "handshake"
    assert "within 2s" in entry["detail"]

    pid = int(pidfile.read_text())
    for _ in range(100):
        try:
            os.kill(pid, 0)
        except OSError:
            break
        time.sleep(0.1)
    else:
        pytest.fail(f"the hung agent {pid} was still running after the check")


# ----------------------------------------------------------- the nested scope


def test_recursive_reaches_the_children_and_keeps_the_root_choice_at_the_root(team):
    """A same-named child task keeps its own recipe's agent."""
    team.install("parent", PARENT)
    team.install("kid", KID)
    result = team.cli(
        "check", "parent", "--probe", "--recursive", "--json",
        "--agent", "step_1=gemini", "--timeout", "30",
    )
    assert result.returncode == 0, result.stdout + result.stderr

    probe = team.report(result)
    assert probe["scope"] == "recursive"
    agents = _by_tool(probe)
    # The root's own task moved; the child's identically-named task did not.
    assert [t["path"] for t in agents["harness:gemini"]["tasks"]] == ["parent.step_1"]
    assert [t["path"] for t in agents["harness:codex"]["tasks"]] == [
        "parent.call_a -> kid.step_1"
    ]
    assert "harness:claude-code" not in agents
    # The child is called twice and checked once.
    assert len(team.sessions("codex")) == 1


def test_without_recursive_the_report_says_the_children_were_not_checked(team):
    """Root-only coverage is stated, not implied."""
    team.install("parent", PARENT)
    team.install("kid", KID)
    result = team.cli("check", "parent", "--probe", "--timeout", "30")
    assert result.returncode == 0, result.stdout + result.stderr
    flat = _flat(result.stdout)
    assert "root only" in flat and "--recursive" in flat
    assert team.events("codex") == []

    quiet = team.cli("check", "team", "--probe", "--json", "--timeout", "30")
    assert "root only" not in quiet.stdout


@pytest.mark.parametrize(
    ("broken", "expected"),
    [
        ("kid", "conduit not found"),
        ('"{{inputs.which}}"', "cannot recursively check the dynamic conduit target"),
        ("parent", "nested conduit cycle detected"),
    ],
)
def test_a_broken_call_graph_starts_no_agent(team, broken, expected):
    """A missing child, a templated target and a cycle all gate the probe."""
    team.install("parent", PARENT.replace("task: kid", f"task: {broken}"))
    result = team.cli("check", "parent", "--probe", "--recursive", "--json")
    assert result.returncode == 1
    assert expected in json.loads(result.stdout)[0]["error"]
    team.quiet()


def test_an_unusable_child_binding_starts_no_agent(team):
    """The call forwards a key the child cannot use; nothing is started."""
    team.install(
        "parent",
        PARENT.replace(
            "    task: kid\n    tool: tool:conduit\n    depends_on: []\n",
            "    task: kid\n    tool: tool:conduit\n    depends_on: []\n"
            "    inputs:\n      typo: whatever\n",
            1,
        ),
    )
    team.install("kid", KID)
    result = team.cli("check", "parent", "--probe", "--recursive", "--json")
    assert result.returncode == 1
    assert "typo" in json.loads(result.stdout)[0]["error"]
    team.quiet()


# ------------------------------------------------------- refusing bad options


@pytest.mark.parametrize(
    "args",
    [
        ("check", "--probe"),
        ("check", "--agent", "step_1=codex"),
        ("check", "team", "--timeout", "5"),
        ("check", "team", "--probe", "--timeout", "0"),
        ("check", "team", "--probe", "--timeout", "-3"),
        ("check", "team", "--probe", "--timeout", "nan"),
        ("check", "team", "--probe", "--timeout", "inf"),
        ("check", "team", "--probe", "--agent", "step_1"),
        ("check", "team", "--probe", "--agent", "step_1=codex", "--agent", "step_1=gemini"),
        ("check", "team", "--probe", "--agent", "step_1=tool:bash"),
    ],
)
def test_a_misused_option_exits_two_and_starts_nothing(team, args):
    """Option misuse is a usage error, decided before any process starts."""
    result = team.cli(*args)
    assert result.returncode == 2, result.stdout + result.stderr
    team.quiet()


@pytest.mark.parametrize(
    ("selector", "expected"),
    [
        ("nope=codex", "has no task"),
        ("step_1=ghostagent", "unknown agent"),
    ],
)
def test_a_selection_the_world_refuses_is_a_failed_row(team, selector, expected):
    """Wrong about the recipe or the machine: reported, and nothing started."""
    result = team.cli("check", "team", "--probe", "--json", "--agent", selector)
    assert result.returncode == 1
    assert expected in json.loads(result.stdout)[0]["error"]
    team.quiet()


def test_a_rejected_option_leaves_json_stdout_empty(team):
    """A machine reader gets no half-written document to parse."""
    result = team.cli("check", "team", "--probe", "--json", "--agent", "step_1")
    assert result.returncode == 2
    assert result.stdout == ""
    assert "TASK=HARNESS" in _flat(result.stderr)


# --------------------------------------------- the unflagged command, as before


def test_the_ordinary_check_is_unchanged_and_starts_nothing(team):
    """No flag, no probe: the same rows, the same keys, no agent started."""
    result = team.cli("check", "team", "--json")
    assert result.returncode == 0, result.stdout + result.stderr
    rows = json.loads(result.stdout)
    assert sorted(rows[0]) == ["error", "name", "ok", "path", "required_inputs", "source"]
    assert rows[0]["required_inputs"] == ["brief"]
    team.quiet()

    every = team.cli("check", "--json")
    assert every.returncode == 0, every.stdout + every.stderr
    team.quiet()


def test_agent_without_probe_stays_static(team):
    """The selection is validated against the recipe and nothing is started."""
    ok = team.cli("check", "team", "--json", "--agent", "step_2=gemini")
    assert ok.returncode == 0, ok.stdout + ok.stderr
    assert "probe" not in json.loads(ok.stdout)[0]
    team.quiet()


def test_a_workflow_with_no_agents_reports_an_empty_team(team):
    """Nothing to start is a pass, said plainly rather than implied."""
    team.install("chore", BASH_ONLY)
    result = team.cli("check", "chore", "--probe", "--json")
    assert result.returncode == 0, result.stdout + result.stderr
    probe = team.report(result)
    assert probe["ran"] is True and probe["ok"] is True and probe["agents"] == []
    team.quiet()
