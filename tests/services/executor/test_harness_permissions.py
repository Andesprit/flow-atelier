"""Agent permission levels (``full``, ``edits``, ``read-only``) over real ACP.

Each test drives the fake ACP agent, which echoes the mode the harness picked
as ``[mode_set:<id>]`` and the option it answered a permission request with as
``[perm:<id>]``, so the chosen mode and the decision are read back from the
agent rather than inferred.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest

from flow_atelier.schemas.conduit import TaskDefinition
from flow_atelier.schemas.interaction import InteractionPolicy
from flow_atelier.services.executor.base import FlowContext
from flow_atelier.services.executor.harness import DEFAULT_DONE_MARKER, AcpHarnessExecutor

FAKE_AGENT = Path(__file__).resolve().parents[2] / "fixtures" / "fake_acp_agent.py"

# The modes claude-agent-acp 0.62.0 offers, and the ones codex-acp 1.1.7 does.
CLAUDE_MODES = [
    {"id": "auto", "name": "Auto"},
    {"id": "default", "name": "Manual"},
    {"id": "acceptEdits", "name": "Accept Edits"},
    {"id": "plan", "name": "Plan Mode"},
    {"id": "dontAsk", "name": "Don't Ask"},
    {"id": "bypassPermissions", "name": "Bypass Permissions"},
]
CODEX_MODES = [
    {"id": "read-only", "name": "Read Only"},
    {"id": "agent", "name": "Agent"},
    {"id": "agent-full-access", "name": "Agent (full access)"},
]
OPTIONS = [
    {"id": "allow", "label": "Allow", "kind": "allow_once"},
    {"id": "deny", "label": "Reject", "kind": "reject_once"},
]


class _Sink:
    """PromptSink double that records what a person would have been asked."""

    def __init__(self) -> None:
        self.prompts: list[str] = []
        self.shown: list[str] = []

    async def display(self, text: str) -> None:
        """Record displayed text.

        :param text: the text shown.
        """
        self.shown.append(text)

    async def request_input(self, prompt: str) -> str:
        """Record the question and allow the request.

        :param prompt: the question a person would be asked.
        """
        self.prompts.append(prompt)
        return "allow"


def _executor(sink: _Sink, *, modes: list[dict] | None = None, current: str = "default",
              kind: str = "execute", options: list[dict] | None = None,
              stop: str = "end_turn") -> AcpHarnessExecutor:
    """Build a harness whose one turn asks permission for a ``kind`` tool call.

    :param sink: the prompt sink the harness talks to.
    :param modes: session modes the agent advertises, or ``None`` for none.
    :param current: the mode the session starts in.
    :param kind: the ACP tool kind the agent asks about.
    :param options: the permission options offered.
    :param stop: the stop reason the turn ends with.
    """
    script: dict[str, Any] = {"turns": [{
        "ask_permission": {"summary": f"{kind} something", "kind": kind,
                           "options": options or OPTIONS},
        # The marker ends an interactive run after this one turn.
        "chunks": [" done " + DEFAULT_DONE_MARKER],
        "stop": stop,
    }]}
    if modes is not None:
        script["modes"] = {"current": current, "available": modes}
    argv = [sys.executable, str(FAKE_AGENT), "--script", json.dumps(script)]
    return AcpHarnessExecutor(launch_cmd=argv, sink=sink)


def _task(permissions: str | None = None) -> TaskDefinition:
    """Build an agent task, optionally naming its own permission level.

    :param permissions: the task's ``permissions`` field.
    """
    return TaskDefinition(name="h", description="d", task="x", tool="harness:claude-code",
                          permissions=permissions)


def _ctx(agent_permissions: str | None = None, **kwargs: Any) -> FlowContext:
    """Build a FlowContext carrying the global permission setting.

    :param agent_permissions: the ``ATELIER_AGENT_PERMISSIONS`` value.
    :param kwargs: other FlowContext fields.
    """
    return FlowContext(flow_id="fake", store=None, inputs={}, timeout=30,  # type: ignore[arg-type]
                       agent_permissions=agent_permissions, **kwargs)


async def test_full_is_the_default_and_keeps_todays_behaviour() -> None:
    """Unset: the most permissive mode is chosen and every request is allowed."""
    result = await _executor(_Sink(), modes=CLAUDE_MODES).execute(_task(), "x", _ctx())
    assert result.exit_code == 0, result.stderr
    assert "[mode_set:bypassPermissions]" in result.output
    assert "[perm:allow]" in result.output


async def test_full_leaves_codex_in_its_default_mode() -> None:
    """No full tier matches a codex-acp mode, as before this setting existed."""
    result = await _executor(_Sink(), modes=CODEX_MODES, current="agent").execute(
        _task(), "x", _ctx("full")
    )
    assert result.exit_code == 0, result.stderr
    assert "[mode_set:" not in result.output
    assert "[perm:allow]" in result.output


@pytest.mark.parametrize(("kind", "answer"), [
    ("edit", "allow"), ("read", "allow"), ("delete", "allow"),
    ("execute", "deny"), ("fetch", "deny"), ("other", "deny"),
])
async def test_edits_allows_file_changes_and_rejects_the_rest(kind: str, answer: str) -> None:
    """``edits`` takes acceptEdits, allows file requests and rejects commands."""
    sink = _Sink()
    result = await _executor(sink, modes=CLAUDE_MODES, kind=kind).execute(
        _task(), "x", _ctx("edits")
    )
    assert result.exit_code == 0, result.stderr
    assert "[mode_set:acceptEdits]" in result.output
    assert "bypassPermissions" not in result.output
    assert f"[perm:{answer}]" in result.output
    assert sink.prompts == []
    rejected = [e for e in result.session if e.get("source") == "permissions"]
    assert bool(rejected) == (answer == "deny")


@pytest.mark.parametrize("kind", ["read", "edit", "execute"])
async def test_read_only_rejects_every_request(kind: str) -> None:
    """``read-only`` keeps a prompting mode and answers every request with a reject."""
    result = await _executor(_Sink(), modes=CLAUDE_MODES, kind=kind).execute(
        _task(), "x", _ctx("read-only")
    )
    assert result.exit_code == 0, result.stderr
    assert "[mode_set:" not in result.output
    assert "[perm:deny]" in result.output
    assert any("permissions read-only rejected" in s.text for s in result.steps)


async def test_read_only_takes_codex_read_only_mode() -> None:
    """codex-acp's default mode edits files unasked, so read-only switches away."""
    result = await _executor(_Sink(), modes=CODEX_MODES, current="agent").execute(
        _task(), "x", _ctx("read-only")
    )
    assert result.exit_code == 0, result.stderr
    assert "[mode_set:read-only]" in result.output


@pytest.mark.parametrize(("kind", "answer"), [("edit", "allow"), ("execute", "deny")])
async def test_edits_takes_codex_read_only_mode(kind: str, answer: str) -> None:
    """codex-acp has no accept-edits mode, so ``edits`` takes read-only, which asks."""
    result = await _executor(_Sink(), modes=CODEX_MODES, current="agent", kind=kind).execute(
        _task(), "x", _ctx("edits")
    )
    assert result.exit_code == 0, result.stderr
    assert "[mode_set:read-only]" in result.output
    assert f"[perm:{answer}]" in result.output


@pytest.mark.parametrize("level", ["edits", "read-only"])
async def test_safer_levels_name_themselves_when_no_mode_asks(level: str) -> None:
    """A session that starts in bypass mode with no way out fails naming the level."""
    modes = [{"id": "bypassPermissions", "name": "Bypass Permissions"}]
    result = await _executor(_Sink(), modes=modes, current="bypassPermissions").execute(
        _task(), "x", _ctx(level)
    )
    assert result.exit_code != 0
    assert f"permissions {level}: the agent starts in mode 'bypassPermissions'" in (
        result.stderr
    )


@pytest.mark.parametrize("level", ["edits", "read-only"])
async def test_safer_levels_leave_a_bypass_mode_the_agent_started_in(level: str) -> None:
    """An agent whose own settings start it in bypass mode is moved out of it."""
    modes = [m for m in CLAUDE_MODES if m["id"] != "acceptEdits"]
    result = await _executor(_Sink(), modes=modes, current="bypassPermissions").execute(
        _task(), "x", _ctx(level)
    )
    assert result.exit_code == 0, result.stderr
    assert "[mode_set:default]" in result.output
    assert "[perm:deny]" in result.output


async def test_reject_once_is_preferred_and_no_reject_option_cancels() -> None:
    """A reject option is selected when offered; with none the request is cancelled."""
    options = [
        {"id": "always", "label": "Always", "kind": "allow_always"},
        {"id": "never", "label": "Never", "kind": "reject_always"},
        {"id": "no", "label": "No", "kind": "reject_once"},
    ]
    result = await _executor(_Sink(), options=options).execute(_task(), "x", _ctx("read-only"))
    assert "[perm:no]" in result.output
    allow_only = [{"id": "allow", "label": "Allow", "kind": "allow_once"}]
    result = await _executor(_Sink(), options=allow_only).execute(
        _task(), "x", _ctx("read-only")
    )
    assert "[perm:]" in result.output


@pytest.mark.parametrize(("task_level", "global_level", "answer"), [
    ("full", "read-only", "allow"),
    ("read-only", "full", "deny"),
    ("read-only", None, "deny"),
])
async def test_task_permissions_beat_the_global_setting(
    task_level: str, global_level: str | None, answer: str
) -> None:
    """A task's own ``permissions`` overrides ATELIER_AGENT_PERMISSIONS."""
    result = await _executor(_Sink()).execute(_task(task_level), "x", _ctx(global_level))
    assert f"[perm:{answer}]" in result.output


async def test_a_routed_request_the_level_forbids_is_never_asked() -> None:
    """With ``interaction.permissions: human`` only allowed requests reach a person."""
    policy = InteractionPolicy(questions="human", permissions="human")
    sink = _Sink()
    result = await _executor(sink, kind="execute").execute(
        _task("edits"), "x", _ctx(interaction=policy, interactive_harnesses=True)
    )
    assert "[perm:deny]" in result.output
    assert not any("execute something" in p for p in sink.prompts)

    sink = _Sink()
    result = await _executor(sink, kind="edit").execute(
        _task("edits"), "x", _ctx(interaction=policy, interactive_harnesses=True)
    )
    assert "[perm:allow]" in result.output
    assert any("edit something" in p for p in sink.prompts)


@pytest.mark.parametrize("level", ["edits", "read-only"])
@pytest.mark.parametrize("routing", ["human", "supervisor"])
@pytest.mark.parametrize("start", ["agent", "agent-full-access"])
async def test_a_routed_level_still_takes_codex_read_only_mode(
    level: str, routing: str, start: str
) -> None:
    """With requests sent to a person, Codex still leaves its unasked modes."""
    supervisor = {"supervisor": {"tool": "harness:codex"}} if routing == "supervisor" else {}
    policy = InteractionPolicy(questions="human", permissions=routing, **supervisor)
    sink = _Sink()
    result = await _executor(sink, modes=CODEX_MODES, current=start, kind="edit").execute(
        _task(level), "x", _ctx(interaction=policy, interactive_harnesses=True)
    )
    # Under routing the mode chunk reaches the person's view, not the output.
    assert "[mode_set:read-only]" in result.output + "".join(sink.shown)


async def test_routed_edits_keeps_claude_asking_about_edits() -> None:
    """acceptEdits would stop the person being asked, so Claude keeps its mode."""
    policy = InteractionPolicy(questions="human", permissions="human")
    sink = _Sink()
    result = await _executor(sink, modes=CLAUDE_MODES, kind="edit").execute(
        _task("edits"), "x", _ctx(interaction=policy, interactive_harnesses=True)
    )
    assert "[mode_set:" not in result.output + "".join(sink.shown)
    assert any("edit something" in p for p in sink.prompts)


async def test_a_failed_turn_names_what_the_level_rejected() -> None:
    """The rejection reaches stderr, and so the next loop try, only on failure."""
    result = await _executor(_Sink(), stop="refusal").execute(
        _task("read-only"), "x", _ctx()
    )
    assert result.exit_code != 0
    assert result.stderr.endswith("permissions read-only rejected: execute something")

    result = await _executor(_Sink()).execute(_task("read-only"), "x", _ctx())
    assert result.exit_code == 0, result.stderr
    assert "rejected" not in result.stderr
