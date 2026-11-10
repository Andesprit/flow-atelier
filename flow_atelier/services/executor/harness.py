"""Harness executor — drives any ACP-speaking coding agent.

One executor covers every agent; they differ only in the argv that starts
them and, occasionally, an environment variable or two. Those come from
:mod:`flow_atelier.services.executor.acp_registry`, so ``harness:gemini``
and ``harness:claude-code`` are the same code path with different data.

Each agent runs as the user's own installed, logged-in CLI: flow-atelier
spawns the command it was given and never installs or authenticates
anything itself. :meth:`AcpHarnessExecutor.probe` reports which of those
two the user still has to do.

Non-interactive mode sends one prompt turn and returns whatever the agent
streams before ``stop_reason``. Interactive mode keeps the session open
and loops: after each turn, if the accumulated output has not contained
the done marker, the executor routes the next reply through the conduit
interaction policy and sends another ``session/prompt``. The loop terminates
when the marker appears or when :attr:`MAX_INTERACTIVE_TURNS` is reached.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import re
import shutil
import signal
import sys
import time
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

from flow_atelier.schemas.conduit import TaskDefinition
from flow_atelier.schemas.log import (
    ExecutionResult,
    IntermediateStep,
    StepKind,
    TurnUsage,
    UsageLimit,
)
from flow_atelier.services.executor.base import ExecutorBase, FlowContext, child_environ
from flow_atelier.services.executor.interaction import InteractionRouter
from flow_atelier.services.executor.prompt_sink import (
    PromptSink,
    TerminalPromptSink,
)
from flow_atelier.services.executor.usage_limit import (
    claude_reset,
    is_usage_limit,
    is_usage_limit_reply,
    parse_reset,
)

if TYPE_CHECKING:
    from acp.connection import StreamEvent
    from acp.schema import (
        RequestPermissionResponse,
        SessionModeState,
        Usage,
    )

logger = logging.getLogger(__name__)


def _resolve_cmd(cmd: str) -> str:
    """On Windows, resolve .cmd/.bat wrappers that ``create_subprocess_exec`` can't find."""
    if os.name == "nt":
        resolved = shutil.which(cmd)
        if resolved is not None:
            return resolved
    return cmd


async def _close_agent_quickly(proc: asyncio.subprocess.Process) -> None:
    """End an agent whose turn succeeded without the transport's 2 s wait.

    Closes stdin, gives the agent :data:`AGENT_CLOSE_GRACE_SECONDS` to exit,
    then sends SIGTERM. The ACP transport's own shutdown still runs after
    this and escalates to SIGKILL if the agent ignores SIGTERM.

    :param proc: the agent process.
    """
    if proc.returncode is not None or proc.stdin is None:
        return
    with contextlib.suppress(Exception):
        proc.stdin.close()
    try:
        await asyncio.wait_for(proc.wait(), AGENT_CLOSE_GRACE_SECONDS)
    except TimeoutError:
        with contextlib.suppress(ProcessLookupError):
            proc.terminate()


def _signal_agent_group(proc: asyncio.subprocess.Process, sig: int) -> None:
    """Send ``sig`` to an agent and every process it started (POSIX only).

    The agent leads its own process group (see :func:`_spawn_agent`), whose
    id is its pid, so this also reaches the tools, MCP servers and shells it
    left behind. The id is not looked up with ``os.getpgid``: once the agent
    has exited and been reaped that lookup fails while its children live on.

    :param proc: the agent process.
    :param sig: the signal to send.
    """
    if sys.platform == "win32" or proc.pid is None:
        return
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(proc.pid, sig)


@contextlib.asynccontextmanager
async def _spawn_agent(client: Any, cmd: str, *args: str, cwd: str, env: dict[str, str],
                       observers: list) -> Any:
    """Start an ACP agent in its own process group and connect to it.

    Like ``acp.spawn_agent_process``, but the agent leads a new session, so a
    stop, a timeout or a failed turn can end everything it started, not just
    the agent itself (see :func:`_signal_agent_group`). Shutdown matches the
    ACP transport's: close stdin, wait, then SIGTERM and SIGKILL, here sent to
    the whole group. Windows has no process groups; it keeps
    ``acp.spawn_agent_process``.

    :param client: the ACP client the agent talks to.
    :param cmd: the agent executable.
    :param args: its arguments.
    :param cwd: the agent's working directory.
    :param env: the agent's environment.
    :param observers: stream observers for the connection.
    :returns: an async context of ``(connection, process)``.
    """
    import acp

    proc = await asyncio.create_subprocess_exec(
        cmd, *args,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=cwd,
        env=env,
        # Codex and similar harnesses emit large JSON-RPC frames (tool
        # results, planning output) that exceed asyncio's 64 KiB line limit.
        limit=8 * 1024 * 1024,
        start_new_session=True,
    )
    conn = acp.connect_to_agent(client, proc.stdin, proc.stdout, observers=observers)
    try:
        yield conn, proc
    finally:
        with contextlib.suppress(Exception):
            await conn.close()
        if proc.stdin is not None:
            with contextlib.suppress(Exception):
                proc.stdin.close()
        for sig in (None, signal.SIGTERM, signal.SIGKILL):
            if sig is not None:
                _signal_agent_group(proc, sig)
            try:
                await asyncio.wait_for(proc.wait(), AGENT_SHUTDOWN_SECONDS)
                break
            except TimeoutError:
                continue


def _close_proc_transports(proc: asyncio.subprocess.Process) -> None:
    """Explicitly close pipe transports on Windows to prevent ``__del__`` errors."""
    for stream in (proc.stdout, proc.stderr):
        if stream is None:
            continue
        transport = getattr(stream, "_transport", None)
        if transport is not None:
            with contextlib.suppress(OSError, ValueError):
                transport.close()


DEFAULT_DONE_MARKER = "[ATELIER_DONE]"
MAX_INTERACTIVE_TURNS = 20

# Group consecutive AgentThoughtChunk updates into one IntermediateStep
# until the merged text reaches this many characters or hits a newline.
# Some agents (notably opencode) emit one thought chunk per token; without
# grouping the UI shows one rendered line per word.
THINKING_FLUSH_CHARS = 200

# Same batching for agent message chunks on non-interactive runs, so the
# agent's narration appears in readable blocks rather than per token.
MESSAGE_FLUSH_CHARS = 200

# Cap on a serialized tool payload kept on an IntermediateStep.
TOOL_PAYLOAD_CHARS = 500

# Trailing lines of agent-process stderr retained for diagnostics.
AGENT_STDERR_LINES = 50

# Grace period for the stderr drain to pick up an agent's dying words
# before the transport tears the pipe down.
STDERR_DRAIN_GRACE_SECONDS = 0.5

# After a successful turn the agent has nothing left to say. Some adapters
# (codex-acp) still take about 2 s to exit once stdin closes, which the ACP
# transport waits out before it sends SIGTERM; give them this long instead.
AGENT_CLOSE_GRACE_SECONDS = 0.5

# How long an agent gets to exit after stdin closes, and again after SIGTERM,
# before the next signal: the ACP transport's own shutdown timeout.
AGENT_SHUTDOWN_SECONDS = 2.0

# A turn with no tool calls whose whole reply is this short and reads like
# the agent's own error message failed, even though the agent ended it
# normally.
ERROR_REPLY_MAX_CHARS = 600
_AGENT_ERROR_TEXT = re.compile(
    r"not logged in|please run /login|invalid api key|authentication required"
    r"|not supported when using codex with a chatgpt account",
    re.IGNORECASE,
)

# Bound on waiting for a turn's session-update handlers to finish. Reached
# only if a notification is lost, in which case a run must not stall until
# the task timeout fires.
UPDATE_DRAIN_TIMEOUT_SECONDS = 5.0

# Budget for a connection check. Generous: the first run of an
# npx/uvx-distributed agent downloads the package before it says anything.
PROBE_TIMEOUT_SECONDS = 90.0

# What `atelier harness check --prompt` asks. Short, so the check costs a
# handful of tokens, and answerable without any tool.
PROBE_PROMPT = "Reply with the single word OK and nothing else."


@dataclass
class ProbeResult:
    """Outcome of a harness connection check.

    :param ok: whether the agent is reachable and completed the handshake.
    :param stage: how far the check got — ``path``, ``spawn``, ``initialize``,
        ``handshake`` (timed out), ``session``, ``selection`` (the session
        opened but the named model or effort was refused), ``prompt`` (the
        agent refused the test prompt), or ``ok``.
    :param detail: one-line human explanation of ``stage``.
    :param agent: the agent's self-reported name and version, when it got
        far enough to send one.
    :param protocol_version: the ACP version the agent negotiated.
    :param auth_methods: auth method ids the agent advertises. Reported only
        — logging in is the user's job, done with the agent's own CLI.
    :param modes: session mode ids the agent offers.
    :param permissive_mode: the mode a real run would switch to, if any.
    :param current_mode: the mode the agent starts a session in.
    :param models: model ids the agent offers, usable as
        ``harness:<name>:<model>``.
    :param current_model: the model the agent starts a session on.
    :param efforts: reasoning-effort ids offered *for the selected model*,
        usable as ``harness:<name>:<model>:<effort>``.
    :param current_effort: the effort that model would run on unnamed.
    :param stderr: tail of the agent's stderr, the usual place a failing
        agent explains itself.
    :param reply: what the agent answered the test prompt, when one was sent.
    """

    ok: bool
    stage: str
    detail: str
    agent: str = ""
    protocol_version: int | None = None
    auth_methods: list[str] = field(default_factory=list)
    modes: list[str] = field(default_factory=list)
    permissive_mode: str = ""
    current_mode: str = ""
    models: list[str] = field(default_factory=list)
    current_model: str = ""
    efforts: list[str] = field(default_factory=list)
    current_effort: str = ""
    stderr: str = ""
    reply: str = ""

    def guidance(self) -> list[str]:
        """Return what the user has to do about a failed check, in order.

        Derived from ``stage`` alone, never guessed: a stage with no setup
        step anyone can name (a refused model, a spawn error) yields nothing
        rather than a plausible-sounding instruction. Installing an agent and
        logging into one stay with the user; this only says which is missing.

        :returns: zero or more instruction lines; empty when the probe passed
            or nothing actionable is known.
        """
        if self.ok:
            return []
        if self.stage == "path":
            return [
                "install this agent yourself and make sure its command is on "
                "PATH, then re-run this check"
            ]
        if self.stage == "session":
            lines = []
            if self.auth_methods:
                lines.append(
                    "this agent likely needs a login — it accepts: "
                    + ", ".join(self.auth_methods)
                )
            lines.append("log in with the agent's own CLI, then re-run this check")
            return lines
        if self.stage == "prompt":
            return [
                "the agent opened a session but refused a prompt: log in again "
                "with the agent's own CLI, or check its plan's usage and model access"
            ]
        if self.stage in ("initialize", "handshake"):
            return [
                "the command started but did not speak ACP — check it is the "
                "agent's ACP entry point (some CLIs need an --acp flag)"
            ]
        return []


@dataclass
class _SelectChoice:
    """How a session lets the client pick one value — a model, an effort.

    :param available: the values the agent offers.
    :param current: the value in force right now.
    :param config_id: the ``configOptions`` entry to set, or ``None`` when the
        agent only speaks the older ``session/set_model``.
    """

    available: list[str]
    current: str
    config_id: str | None


# ACP's semantic category for a reasoning-effort selector. The *id* under it
# is the agent's own — claude-agent-acp calls it ``effort``, codex-acp calls
# it ``reasoning_effort`` — so the category is what we match on and the id is
# what we send back.
# See https://agentclientprotocol.com/protocol/v1/session-config-options
EFFORT_CATEGORY = "thought_level"

# The category is optional in ACP and the spec leans on it for presentation
# only, so an agent may ship the effort selector with none at all. These are
# the ids the known adapters use; they stand in when a selector is
# uncategorized, the same way an uncategorized ``model`` id stands in for the
# model selector. A selector categorized as something *else* is never read as
# effort, and an uncategorized id outside this set stays unsupported.
EFFORT_IDS = frozenset({"effort", "reasoning_effort", "thought_level"})


def _select_values(option) -> list[str]:
    """Return every value a ``select`` config option offers.

    :param option: a ``SessionConfigOptionSelect``.
    :returns: the option's values, flattened across any groups.
    """
    values: list[str] = []
    for entry in option.options:
        # A grouped select nests its values one level down. An empty group is
        # legal and has no ``value`` of its own, so branch on whether the
        # entry *has* nested options, not on whether it has any.
        nested = getattr(entry, "options", None)
        if nested is None:
            values.append(entry.value)
        else:
            values.extend(o.value for o in nested)
    return values


def _model_choice(sess, options=None) -> _SelectChoice | None:
    """Find the model selector a session advertises.

    The stable path is a ``select`` config option in the ``model`` category
    (codex-acp and claude-agent-acp both use the id ``model``). The older
    ``models`` state with ``session/set_model`` is the fallback.

    :param sess: the ``NewSessionResponse``.
    :param options: config options to read instead of the session's own —
        pass the list a ``session/set_config_option`` response returned so a
        later lookup sees the agent's current truth, not the opening one.
    :returns: the selector, or ``None`` when the agent offers no model choice.
    """
    if options is None:
        options = getattr(sess, "config_options", None)
    for option in options or []:
        if getattr(option, "type", None) != "select":
            continue
        if option.category != "model" and option.id != "model":
            continue
        return _SelectChoice(_select_values(option), option.current_value, option.id)
    models = getattr(sess, "models", None)
    if models is not None:
        return _SelectChoice(
            [m.model_id for m in models.available_models], models.current_model_id, None
        )
    return None


def _effort_choice(options) -> _SelectChoice | None:
    """Find the reasoning-effort selector in a config-options list.

    Which efforts exist depends on the model, so this must only ever be read
    off options the agent returned *after* the model was selected.

    Matched on the ``thought_level`` category first and, only for a selector
    that carries no category at all, on the ids in :data:`EFFORT_IDS`.

    :param options: the agent's current ``configOptions``, or ``None``.
    :returns: the selector, or ``None`` when the agent offers no effort choice.
    """
    uncategorized = None
    for option in options or []:
        if getattr(option, "type", None) != "select":
            continue
        category = getattr(option, "category", None)
        # An explicit category wins wherever it sits in the list, so scan the
        # whole thing before settling for an id match.
        if category == EFFORT_CATEGORY:
            return _SelectChoice(_select_values(option), option.current_value, option.id)
        if category is None and uncategorized is None and option.id in EFFORT_IDS:
            uncategorized = option
    if uncategorized is None:
        return None
    return _SelectChoice(
        _select_values(uncategorized), uncategorized.current_value, uncategorized.id
    )


class _ProbeClient:
    """ACP client for a connection check: keeps the agent's text, allows nothing.

    The agent's message text is kept so a check that sends a test prompt can
    report the reply. Permission requests are denied rather than
    auto-approved: a check must not authorize an agent to touch anything.
    """

    def __init__(self) -> None:
        """Start with no reply text."""
        self.text: list[str] = []
        # Updates seen on the wire vs. handled, as in _BufferingClient.
        self._received = 0
        self._handled = 0
        self._idle = asyncio.Event()

    def observe(self, event: StreamEvent) -> None:
        """Count ``session/update`` notifications as they arrive off the wire.

        :param event: the raw JSON-RPC message event from the connection.
        """
        from acp.connection import StreamDirection

        if (
            event.direction is StreamDirection.INCOMING
            and event.message.get("method") == "session/update"
        ):
            self._received += 1

    async def await_pending_updates(
        self, timeout: float = UPDATE_DRAIN_TIMEOUT_SECONDS
    ) -> None:
        """Wait until every observed update has been handled.

        See :meth:`_BufferingClient.await_pending_updates`: a prompt's reply
        can resolve before the handlers of its last chunks have run.

        :param timeout: seconds to wait before giving up.
        """
        while self._handled < self._received:
            self._idle.clear()
            try:
                await asyncio.wait_for(self._idle.wait(), timeout)
            except TimeoutError:
                return

    async def session_update(self, session_id: str, update, **kwargs: Any) -> None:
        """Keep the text of an agent message chunk; ignore every other update.

        :param session_id: id of the session emitting the update (unused).
        :param update: the ACP update payload.
        :param kwargs: additional ACP fields (unused).
        """
        from acp.schema import AgentMessageChunk

        del session_id, kwargs
        try:
            if isinstance(update, AgentMessageChunk):
                text = getattr(update.content, "text", None)
                if text:
                    self.text.append(text)
        finally:
            self._handled += 1
            if self._handled >= self._received:
                self._idle.set()

    async def request_permission(
        self, *args: Any, **kwargs: Any
    ) -> RequestPermissionResponse:
        """Refuse any permission request raised during a check.

        :param args: ignored positional ACP arguments.
        :param kwargs: ignored keyword ACP arguments.
        :returns: a cancelled :class:`DeniedOutcome`.
        """
        from acp.schema import DeniedOutcome, RequestPermissionResponse

        del args, kwargs
        return RequestPermissionResponse(outcome=DeniedOutcome(outcome="cancelled"))

    def on_connect(self, conn: Any) -> None:
        """Record the connection handed over on attach.

        :param conn: the ACP connection.
        """
        self._conn = conn


async def _drain_agent_stderr(stream, sink: deque[str]) -> None:
    """Continuously read the agent subprocess's stderr into ``sink``.

    The ACP transport spawns the agent with ``stderr=PIPE`` but never reads
    it. Leaving it undrained is doubly bad: the agent's own diagnostics
    (auth failures, model errors, node tracebacks) are invisible, and once
    the OS pipe buffer fills the agent *blocks on write* and hangs until the
    task timeout fires. Draining fixes both.

    Never raises: a failure here must not take down the task it exists to
    diagnose.

    :param stream: the subprocess ``stderr`` :class:`asyncio.StreamReader`.
    :param sink: bounded deque collecting the most recent stderr lines.
    """
    with contextlib.suppress(Exception):
        while True:
            raw = await stream.readline()
            if not raw:
                return
            sink.append(raw.decode("utf-8", errors="replace").rstrip())


def _with_agent_stderr(result: ExecutionResult, captured: deque[str]) -> ExecutionResult:
    """Attach captured agent stderr to a failed ``result``.

    Only on failure: on success the agent's stderr is routine chatter
    (progress bars, deprecation notices) and would bury the real output.

    :param result: the execution result to annotate.
    :param captured: recent agent stderr lines.
    :returns: ``result``, with agent stderr appended to its ``stderr``.
    """
    if result.success or not captured:
        return result
    tail = "\n".join(captured)
    result.stderr = f"{result.stderr}\n{tail}".strip() if result.stderr else tail
    return result


def _payload_snippet(value: Any) -> str:
    """Serialize an ACP tool payload to a bounded JSON string.

    JSON (not ``repr``) so renderers can parse the payload back and pull
    out the one field worth showing — the bash command, the file path, the
    search pattern — instead of printing a Python dict repr at the user.

    :param value: the raw ``rawInput``/``rawOutput`` payload, possibly None.
    :returns: JSON text truncated to :data:`TOOL_PAYLOAD_CHARS`, or ``""``.
    """
    if not value:
        return ""
    try:
        text = json.dumps(value, default=str)
    except (TypeError, ValueError):
        text = str(value)
    return text[:TOOL_PAYLOAD_CHARS]

# Mode-id keyword tiers in descending permissiveness, for tasks with ``full``
# permissions. Each ACP agent advertises its own mode ids; we pick the most
# permissive one available so the agent rarely has to ask for tool
# permission. Keyword matched against ``id`` case-insensitively.
#
# The pinned adapters offer: claude-agent-acp 0.62.0 ``default``,
# ``acceptEdits``, ``plan``, ``dontAsk``, ``auto`` when the model supports it
# and, unless it runs as root, ``bypassPermissions``; codex-acp 1.1.7
# ``read-only``, ``agent`` and ``agent-full-access``. No tier matches a
# codex-acp mode, so Codex stays in its default ``agent`` mode (it edits the
# workspace and asks before leaving its sandbox) and the auto-approve
# backstop answers what it asks.
PERMISSIVE_MODE_KEYWORDS: tuple[str, ...] = (
    "bypass",      # claude-agent-acp's bypassPermissions
    "yolo",
    "danger",
    "full-auto",
    "full_auto",
    "accept",      # claude-agent-acp's acceptEdits (weaker, but still skips edit prompts)
    "auto",        # claude-agent-acp's auto (a model classifier decides each prompt)
)

# The mode a safer level switches to, when the agent offers one. ``edits``
# takes an accept-edits mode (claude-agent-acp's acceptEdits), else a
# read-only mode: codex-acp's read-only asks before each edit and each command
# that needs more than reading, so the edit is allowed and the command
# rejected. ``read-only`` takes a read-only mode. Both leave codex-acp's
# default mode, which edits files and runs commands without asking.
LIMITED_MODE_KEYWORDS: dict[str, tuple[str, ...]] = {
    "edits": ("edit", "read-only", "readonly"),
    "read-only": ("read-only", "readonly"),
}

# Mode ids a session moves to when it starts in one that skips permission
# requests, so that a person, a supervisor or a safer level gets asked.
PROMPTING_MODE_IDS: frozenset[str] = frozenset({"default", "normal", "ask", "code"})

# ACP tool-call kinds an ``edits`` task may be allowed: reading and changing
# files. Every other request (execute, fetch, switch_mode, ...) is rejected.
EDIT_TOOL_KINDS: frozenset[str] = frozenset({"read", "search", "edit", "delete", "move"})


def limited_mode(available: list[str], current: str, level: str, routed: bool = False) -> str:
    """Return the mode a run at a safer ``level`` uses; ``harness check`` asks too.

    A mode :data:`LIMITED_MODE_KEYWORDS` names for the level wins. When a
    person or a supervisor answers the requests, only a read-only mode is
    taken: an accept-edits mode would stop them being asked about edits.
    Without such a mode the session keeps ``current``, unless that mode skips
    permission requests (an agent's own settings can start it in one); then it
    moves to a mode that asks.

    :param available: the mode ids the agent offers.
    :param current: the mode id the session starts in.
    :param level: ``edits`` or ``read-only``.
    :param routed: a person or a supervisor answers the requests.
    :returns: the mode id to use, ``current`` when no switch is needed.
    :raises RuntimeError: when ``current`` skips permission requests and no
        mode to switch to is offered.
    """
    for keyword in LIMITED_MODE_KEYWORDS["read-only" if routed else level]:
        for mode in available:
            if keyword in mode.lower():
                return mode
    if not any(word in current.lower() for word in PERMISSIVE_MODE_KEYWORDS):
        return current
    for mode in available:
        if mode.lower() in PROMPTING_MODE_IDS:
            return mode
    raise RuntimeError(
        f"permissions {level}: the agent starts in mode {current!r}, which skips "
        "permission requests, and offers no mode to switch to"
    )


def mode_enforces(mode: str, level: str, routed: bool = False) -> bool:
    """Say whether a session in ``mode`` keeps to a safer ``level``.

    True for a mode named for the level, and for a mode known to ask before
    acting, so every request reaches the level's check. Any other mode is the
    agent's own, and it may act without asking.

    :param mode: the mode id the session runs in.
    :param level: ``edits`` or ``read-only``.
    :param routed: a person or a supervisor answers the requests.
    :returns: ``True`` when the level is enforced in that mode.
    """
    lowered = mode.lower()
    keywords = LIMITED_MODE_KEYWORDS["read-only" if routed else level]
    return any(k in lowered for k in keywords) or lowered in PROMPTING_MODE_IDS


# Lets a safer level run on an agent that cannot be held to it. Read from
# the process environment only, never from a project's .env file.
ALLOW_UNENFORCED_ENV = "ATELIER_ALLOW_UNENFORCED_PERMISSIONS"


def _refuse_unenforced(level: str, why: str) -> None:
    """Fail a run whose permission level the agent cannot be held to.

    :param level: ``edits`` or ``read-only``.
    :param why: what makes the level unenforceable for this agent.
    :raises RuntimeError: unless :data:`ALLOW_UNENFORCED_ENV` is set, in
        which case a warning is printed and the run goes on.
    """
    message = (
        f"permissions {level} cannot be enforced: {why}, so the agent could "
        "act without asking"
    )
    if os.environ.get(ALLOW_UNENFORCED_ENV, "").lower() in ("1", "true", "yes"):
        print(
            f"[flow-atelier] warning: {message}; running anyway because "
            f"{ALLOW_UNENFORCED_ENV} is set. Only requests the agent asks about "
            "are checked.",
            file=sys.stderr,
            flush=True,
        )
        return
    raise RuntimeError(
        f"{message}. Use an agent with a read-only or asking mode (Claude Code, "
        f"Codex), use permissions: full, or set {ALLOW_UNENFORCED_ENV}=1 to run "
        "anyway with only the requests the agent asks about checked"
    )


def _pick_permissive_mode(
    state: SessionModeState | None,
    keywords: tuple[str, ...] = PERMISSIVE_MODE_KEYWORDS,
) -> str | None:
    """Return the mode id from ``state`` that ``keywords`` want most, or ``None``.

    Walks ``keywords`` and returns the first available mode whose ``id``
    contains a keyword (case-insensitive). By default the keywords are
    :data:`PERMISSIVE_MODE_KEYWORDS`, so the most permissive mode wins.

    :param state: ACP session mode state advertised by the agent, or ``None``.
    :param keywords: keyword tiers to walk, most wanted first.
    :returns: the first matching mode id, or ``None`` if no match.
    """
    if state is None or not state.available_modes:
        return None
    available = state.available_modes
    for keyword in keywords:
        for mode in available:
            if keyword in mode.id.lower():
                return mode.id
    return None


def build_interactive_suffix(marker: str) -> str:
    """Return the trailing instruction appended to interactive prompts.

    :param marker: the done-token the agent must emit when finished.
    :returns: instruction text instructing the agent to emit ``marker`` once.
    """
    return (
        "\n\nWhen — and only when — you are completely finished answering, "
        f"output the exact token {marker} to signal completion. "
        "Do NOT echo or repeat the prompt back. Do NOT mention this "
        f"instruction in your answer. The token {marker} must appear only "
        "once, at the very end of your final response."
    )


def _usage_from_client(client: _BufferingClient) -> TurnUsage | None:
    """Build a :class:`TurnUsage` from a client's captured usage/cost.

    Returns ``None`` when the agent reported neither a per-turn token
    breakdown nor a session cost, so a step with no data carries
    ``usage=None`` rather than a fabricated all-zero record.

    :param client: buffering client whose ``usage``/``cost`` were populated
        from the agent's ACP reports.
    :returns: a :class:`TurnUsage`, or ``None`` if no usage data was seen.
    """
    if client.usage is None and client.cost is None:
        return None
    u = client.usage
    return TurnUsage(
        input_tokens=u.input_tokens if u is not None else None,
        output_tokens=u.output_tokens if u is not None else None,
        cached_read_tokens=u.cached_read_tokens if u is not None else None,
        cached_write_tokens=u.cached_write_tokens if u is not None else None,
        thought_tokens=u.thought_tokens if u is not None else None,
        total_tokens=u.total_tokens if u is not None else None,
        cost=client.cost,
    )


def _usage_limit(exc: Exception, client: _BufferingClient) -> UsageLimit | None:
    """Describe the usage limit a failed turn hit, if that is why it failed.

    :param exc: the exception the turn raised.
    :param client: the buffering client, holding the agent's output and any
        reset time it reported.
    :returns: the limit and its reset time, or ``None`` for any other failure.
    """
    data = getattr(exc, "data", None)
    detail = data.get("message", "") if isinstance(data, dict) else ""
    error_text = " ".join(part for part in (str(exc), str(detail)) if part)
    if not is_usage_limit(data, error_text):
        return None
    # Codex says when the limit resets only in the text it streams.
    text = f"{error_text} {''.join(client.buffer)[-2000:]}"
    resets_at = client.limit_resets_at or parse_reset(text, datetime.now().astimezone())
    return UsageLimit(
        resets_at=resets_at.astimezone(UTC).isoformat() if resets_at else None,
        detail=(detail or str(exc)).strip(),
    )


def _failure_in_words(
    client: _BufferingClient, output: str
) -> tuple[str, UsageLimit | None] | None:
    """Find a normally ended turn that did not really succeed.

    Only a turn with no tool calls is judged: an agent that did work may
    say anything. An empty reply failed. A short reply that is only a usage
    limit notice or a login or account error failed; a usage limit gets its
    reset time so the engine can wait and retry.

    :param client: the buffering client that drove the turn.
    :param output: the agent's whole reply.
    :returns: the failure reason and any usage limit, or ``None``.
    """
    if any(step.kind == StepKind.tool_call for step in client.steps):
        return None
    text = output.strip()
    if not text:
        if client._saw_nontext_content:
            return None
        return "agent returned no answer", None
    if client.supervisor_run or len(text) > ERROR_REPLY_MAX_CHARS:
        return None
    if is_usage_limit_reply(text):
        resets_at = client.limit_resets_at or parse_reset(
            text, datetime.now().astimezone()
        )
        limit = UsageLimit(
            resets_at=resets_at.astimezone(UTC).isoformat() if resets_at else None,
            detail=text,
        )
        return f"agent reported a usage limit: {text}", limit
    if _AGENT_ERROR_TEXT.search(text):
        return f"agent reported an error instead of an answer: {text}", None
    return None


def _timings(client: _BufferingClient, spawned_at: float) -> dict[str, float]:
    """Split a harness task's time into agent startup and wait for first output.

    :param client: the buffering client that drove the task.
    :param spawned_at: monotonic clock reading taken just before the spawn.
    :returns: ``startup_seconds`` (spawn through session ready) and
        ``first_output_seconds`` (session ready through the agent's first
        update), each present only when that point was reached.
    """
    timings: dict[str, float] = {}
    if client.ready_at is not None:
        timings["startup_seconds"] = round(client.ready_at - spawned_at, 3)
        if client.first_update_at is not None:
            timings["first_output_seconds"] = round(
                client.first_update_at - client.ready_at, 3
            )
    return timings


class _BufferingClient:
    """ACP :class:`acp.Client` that buffers agent output and routes user I/O.

    Agent message chunks are appended to ``buffer`` and mirrored to the
    :class:`PromptSink`. Tool permissions use the conduit interaction router
    when configured; legacy runs keep automatic approval.

    The harness capabilities default to "no filesystem, no terminal" so the
    agent should not call the file/terminal methods — if it does, they raise
    :class:`NotImplementedError`.

    :param sink: surface for permission requests and (when enabled) live
        chunk / step streaming
    :param stream_messages: when ``True`` (interactive turns), agent message
        chunks are mirrored to ``sink.display`` as they arrive so the user
        can follow the agent before deciding their next reply. When
        ``False`` (single-turn / non-interactive), chunks are only buffered
        for the result — the caller renders them once when the turn ends,
        avoiding double-rendering of the same text.
    :param stream_steps: when ``True``, intermediate steps (thinking, tool
        calls, tool results) are mirrored to ``sink.display_step`` as they
        arrive. Independent of ``stream_messages`` so non-interactive runs
        can surface tool/thinking activity live without dumping the agent's
        full prose mid-flow.
    """

    def __init__(
        self,
        sink: PromptSink,
        stream_messages: bool = False,
        stream_steps: bool = False,
        done_marker: str = DEFAULT_DONE_MARKER,
        on_step: Callable[[IntermediateStep], Awaitable[None]] | None = None,
    ) -> None:
        """Initialize the buffering client.

        :param sink: prompt sink used for permission requests and streaming.
        :param stream_messages: if ``True``, mirror agent message chunks to the sink.
        :param stream_steps: if ``True``, mirror intermediate steps to the sink.
        :param done_marker: token stripped from streamed text before display.
        :param on_step: optional persistence hook called for every step.
        """
        self._sink = sink
        self._stream_messages = stream_messages
        self._stream_steps = stream_steps
        self._done_marker = done_marker
        self._on_step = on_step
        self.buffer: list[str] = []
        self.session: list[dict[str, Any]] = []
        self.router: InteractionRouter | None = None
        self.approve_all = True
        # The task's permission level: ``full``, ``edits`` or ``read-only``.
        self.permissions = "full"
        # One line per request the level rejected, for a failed result's stderr.
        self.rejections: list[str] = []
        self.supervisor_run = False
        self.interaction_error: Exception | None = None
        # Set when an agent message chunk carried only non-text content
        # (image/resource); lets an empty-but-successful turn be flagged.
        self._saw_nontext_content = False
        self.steps: list[IntermediateStep] = []
        self._pending_thinking: list[str] = []
        self._pending_thinking_len: int = 0
        self._pending_message: list[str] = []
        self._pending_message_len: int = 0
        # Tool calls announced before their input, by id. See _dispatch_update.
        self._held_calls: dict[str, IntermediateStep] = {}
        # True once a message block reached the sink, so the caller knows a
        # result panel would duplicate what already scrolled past.
        self.streamed_message = False
        # Last per-turn token breakdown and latest cumulative session cost
        # the agent reported, if any. Both UNSTABLE/optional in ACP.
        self.usage: Usage | None = None
        self.cost: float | None = None
        # Session updates seen on the wire vs. handled, so a turn can wait
        # for its own notifications. See :meth:`await_pending_updates`.
        self._updates_received = 0
        self._updates_handled = 0
        self._updates_idle = asyncio.Event()
        # One handler at a time: notifications arrive as independent tasks.
        self._dispatch_lock = asyncio.Lock()
        # The agent's id for the open session and whether it continued an
        # earlier one. See :meth:`AcpHarnessExecutor._open_session`.
        self.agent_session_id: str | None = None
        self.session_continued = False
        # True while a reloaded session replays its history: those updates
        # are an earlier task's output, not this one's.
        self.replaying = False
        # When the agent last reported its usage limit resets, if it did.
        self.limit_resets_at: datetime | None = None
        # Monotonic clock readings: the agent's session became usable, and
        # its first update for this task arrived. See :func:`_timings`.
        self.ready_at: float | None = None
        self.first_update_at: float | None = None

    async def _record(self, step: IntermediateStep) -> None:
        """Buffer ``step``, persist it, and mirror it to the sink.

        Persistence is best-effort: a failing store must not take down the
        task whose progress it is recording.

        :param step: the step to record.
        """
        self.steps.append(step)
        if self._on_step is not None:
            try:
                await self._on_step(step)
            except Exception:  # noqa: BLE001
                logger.debug("step persistence failed", exc_info=True)
        if self._stream_steps or step.kind == StepKind.interaction:
            if hasattr(self._sink, "display_step"):
                await self._sink.display_step(step)
            elif step.kind == StepKind.interaction:
                await self._sink.display(f"\n{step.text}\n")

    async def _flush_thinking(self) -> None:
        """Emit any buffered thought chunks as one merged ``thinking`` step.

        No-op when the buffer is empty or contains only whitespace.
        """
        if not self._pending_thinking:
            return
        text = "".join(self._pending_thinking).strip()
        self._pending_thinking.clear()
        self._pending_thinking_len = 0
        if not text:
            return
        await self._record(IntermediateStep(kind=StepKind.thinking, text=text))

    async def _flush_message(self) -> None:
        """Emit any buffered agent message chunks as one block.

        The block is handed over unstripped. Chunks are flushed every
        :data:`MESSAGE_FLUSH_CHARS` or on a newline, so stripping each one
        would delete exactly the blank lines and leading indentation that
        separate an agent's prose from the code it is quoting — and a
        message that streams sets ``live_streamed``, suppressing the result
        panel that held the pristine copy. Only an all-whitespace block is
        dropped.

        No-op when the buffer is empty or contains only whitespace.
        """
        if not self._pending_message:
            return
        text = "".join(self._pending_message).replace(self._done_marker, "")
        self._pending_message.clear()
        self._pending_message_len = 0
        if not text.strip():
            return
        if hasattr(self._sink, "display_message"):
            await self._sink.display_message(text)
            self.streamed_message = True

    async def flush_pending(self) -> None:
        """Flush partial thinking/message buffers at a turn boundary.

        Called by the driver after each ``conn.prompt`` round so the last
        group of thinking, the last message block, and any open tool-call
        burst are emitted instead of being stuck pending.
        """
        held, self._held_calls = list(self._held_calls.values()), {}
        for step in held:
            await self._record(step)
        await self._flush_thinking()
        await self._flush_message()
        if hasattr(self._sink, "flush_steps"):
            await self._sink.flush_steps()

    def observe(self, event: StreamEvent) -> None:
        """Count ``session/update`` notifications as they arrive off the wire.

        Registered as an ACP stream observer, which the library invokes
        synchronously from its receive loop, in message order, before the
        notification is queued. That makes this a reliable count of the
        updates a turn produced — see :meth:`await_pending_updates`.

        :param event: the raw JSON-RPC message event from the connection.
        """
        from acp.connection import StreamDirection

        if (
            event.direction is StreamDirection.INCOMING
            and event.message.get("method") == "session/update"
        ):
            self._updates_received += 1
            if not self.replaying:
                self.session.append({"source": "worker", "update": event.message.get("params", {})})

    async def await_pending_updates(
        self, timeout: float = UPDATE_DRAIN_TIMEOUT_SECONDS
    ) -> None:
        """Wait until every observed session update has been handled.

        The agent streams ``session/update`` notifications and then the
        ``prompt`` response. The response resolves ``conn.prompt`` directly,
        while each notification is dispatched as its own background task. So
        when ``conn.prompt`` returns, the handlers for the final chunks may
        still be queued or in flight, and reading the buffer then can drop
        the last chunk.

        Because the receive loop notifies observers in order and before
        queueing, everything counted by :meth:`observe` when ``prompt``
        returns belongs to the turn that just ended; waiting for the handled
        count to catch up is therefore sufficient and needs no access to the
        library's internals. ``session_update`` counts in a ``finally``, so
        a handler that raises still advances the count.

        :param timeout: seconds to wait before giving up. A bound, not an
            expectation: it only stops a lost notification from stalling the
            run until the task timeout fires.
        """
        while self._updates_handled < self._updates_received:
            # No await between the check and the clear, so a handler cannot
            # slip in and have its set() discarded here.
            self._updates_idle.clear()
            try:
                await asyncio.wait_for(self._updates_idle.wait(), timeout)
            except TimeoutError:
                logger.debug(
                    "gave up waiting for %d session updates",
                    self._updates_received - self._updates_handled,
                )
                return

    async def session_update(self, session_id: str, update, **kwargs) -> None:
        """Handle a session update notification from the ACP agent.

        Serialized: the ACP dispatcher runs every notification as its own
        task, so without a lock two handlers interleave at their first await
        and can reorder what they emit — a thinking flush landing between a
        message chunk and its own flush, say. Handlers are started in receive
        order, so taking the lock immediately preserves that order end to end.

        Counts the update as handled on the way out, whatever happens, so
        :meth:`await_pending_updates` can never wait on a handler that has
        already given up.

        :param session_id: id of the session emitting the update (unused).
        :param update: the ACP update payload (message chunk, tool call, etc.).
        :param kwargs: additional ACP fields (unused).
        """
        try:
            if not self.replaying:
                if self.ready_at is not None and self.first_update_at is None:
                    self.first_update_at = time.monotonic()
                async with self._dispatch_lock:
                    await self._dispatch_update(update)
        finally:
            self._updates_handled += 1
            if self._updates_handled >= self._updates_received:
                self._updates_idle.set()
        del session_id, kwargs

    async def _dispatch_update(self, update) -> None:
        """Route one session update to the right buffer or step.

        :param update: the ACP update payload (message chunk, tool call, etc.).
        """
        from acp.schema import (
            AgentMessageChunk,
            AgentThoughtChunk,
            ToolCallProgress,
            ToolCallStart,
            UsageUpdate,
        )

        if isinstance(update, AgentMessageChunk):
            await self._flush_thinking()
            content = update.content
            text = getattr(content, "text", None)
            if text:
                self.buffer.append(text)
                if self._stream_messages:
                    await self._sink.display(text.replace(self._done_marker, ""))
                elif self._stream_steps:
                    # Non-interactive: batch the chunks and surface them
                    # between tool activity, so the run reads as narration
                    # rather than an unexplained sequence of tool calls.
                    self._pending_message.append(text)
                    self._pending_message_len += len(text)
                    if (
                        self._pending_message_len >= MESSAGE_FLUSH_CHARS
                        or "\n" in text
                    ):
                        await self._flush_message()
            else:
                self._saw_nontext_content = True
        elif isinstance(update, AgentThoughtChunk):
            text = getattr(update.content, "text", "") or ""
            if not text:
                return
            self._pending_thinking.append(text)
            self._pending_thinking_len += len(text)
            if self._pending_thinking_len >= THINKING_FLUSH_CHARS or "\n" in text:
                await self._flush_thinking()
        elif isinstance(update, ToolCallStart):
            await self._flush_thinking()
            step = IntermediateStep(
                kind=StepKind.tool_call,
                tool_call_id=update.tool_call_id,
                tool_name=update.title,
                tool_kind=update.kind or "",
                tool_status=update.status or "",
                tool_input=_payload_snippet(update.raw_input),
                locations=[loc.path for loc in (update.locations or [])],
            )
            if update.status == "pending" and not (step.tool_input or step.locations):
                # claude-code-acp announces every call as a bare "Terminal" or
                # "Read File" and sends what it will run in the next update.
                # A pending call has not started, so holding it hides nothing.
                self._held_calls[update.tool_call_id] = step
            else:
                await self._record(step)
        elif isinstance(update, ToolCallProgress):
            held = self._held_calls.get(update.tool_call_id)
            finished = update.status in ("completed", "failed")
            if held is not None and (update.raw_input or update.locations or finished):
                del self._held_calls[update.tool_call_id]
                if update.raw_input:
                    held.tool_input = _payload_snippet(update.raw_input)
                if update.locations:
                    held.locations = [loc.path for loc in update.locations]
                await self._flush_thinking()
                await self._record(held)
            if finished:
                await self._flush_thinking()
                await self._record(
                    IntermediateStep(
                        kind=StepKind.tool_result,
                        tool_call_id=update.tool_call_id,
                        tool_status=update.status or "",
                        tool_output=_payload_snippet(update.raw_output),
                    )
                )
        elif isinstance(update, UsageUpdate):
            # Cumulative session cost — keep the latest value seen, not a sum.
            if update.cost is not None:
                self.cost = update.cost.amount
            resets_at = claude_reset(update.field_meta)
            if resets_at is not None:
                self.limit_resets_at = resets_at

    async def request_permission(
        self, options, session_id: str, tool_call, **kwargs
    ) -> RequestPermissionResponse:
        """Route tool permissions, retaining automatic approval for legacy runs.

        The task's :attr:`permissions` level comes first: a request it does
        not allow is rejected without asking anyone, and only the rest reach
        the interaction router or the automatic approval.

        :param options: list of permission options offered by the agent.
        :param session_id: id of the requesting session (unused).
        :param tool_call: the complete pending tool call.
        :param kwargs: additional ACP fields (unused).
        :returns: an :class:`AllowedOutcome` selecting the chosen option, or
            a :class:`DeniedOutcome` if no allow option is available.
        """
        from acp.schema import AllowedOutcome, DeniedOutcome, RequestPermissionResponse

        self.session.append({
            "source": "permission_request", "tool_call": tool_call.model_dump(mode="json"),
            "options": [o.model_dump(mode="json") for o in options],
        })
        if self.supervisor_run or self.interaction_error is not None:
            return RequestPermissionResponse(outcome=DeniedOutcome(outcome="cancelled"))
        if self.permissions != "full" and not (
            self.permissions == "edits" and tool_call.kind in EDIT_TOOL_KINDS
        ):
            return await self._reject(options, tool_call)
        if not self.approve_all:
            try:
                if self.router is None:
                    raise RuntimeError("permission router unavailable")
                await self.await_pending_updates()
                await self.flush_pending()
                choice = await self.router.resolve(
                    "permission", tool_call.model_dump_json(exclude_none=True),
                    [o.model_dump(mode="json") for o in options],
                )
                if choice == "cancel":
                    return RequestPermissionResponse(outcome=DeniedOutcome(outcome="cancelled"))
                return RequestPermissionResponse(
                    outcome=AllowedOutcome(outcome="selected", option_id=choice)
                )
            except Exception as exc:
                # Some agents recover from a rejected permission callback.
                # Remember the failure so they cannot turn it into success.
                self.interaction_error = exc
                return RequestPermissionResponse(outcome=DeniedOutcome(outcome="cancelled"))
        # Backstop for any agent that still asks despite the permissive
        # session mode: silently pick the most permissive allow option.
        # Tool activity is still surfaced to the user via display_step.
        del session_id, tool_call, kwargs
        if not options:
            return RequestPermissionResponse(outcome=DeniedOutcome(outcome="cancelled"))
        chosen = (
            next((o for o in options if (o.kind or "") == "allow_always"), None)
            or next((o for o in options if (o.kind or "") == "allow_once"), None)
            or next((o for o in options if (o.kind or "").startswith("allow")), None)
        )
        if chosen is None:
            return RequestPermissionResponse(outcome=DeniedOutcome(outcome="cancelled"))
        self.session.append({"source": "approve_all", "option_id": chosen.option_id})
        return RequestPermissionResponse(
            outcome=AllowedOutcome(outcome="selected", option_id=chosen.option_id)
        )

    async def _reject(self, options, tool_call) -> RequestPermissionResponse:
        """Refuse a request the task's :attr:`permissions` level does not allow.

        A reject option is selected rather than the turn cancelled, so the
        agent hears "no" and carries on, the way it does when a person
        refuses. The refusal is shown, so a task that could not run a command
        says why.

        :param options: permission options offered by the agent.
        :param tool_call: the pending tool call being refused.
        :returns: the selected reject option, or a cancelled
            :class:`DeniedOutcome` when the agent offered none.
        """
        from acp.schema import AllowedOutcome, DeniedOutcome, RequestPermissionResponse

        chosen = (
            next((o for o in options if (o.kind or "") == "reject_once"), None)
            or next((o for o in options if (o.kind or "").startswith("reject")), None)
        )
        self.session.append({
            "source": "permissions", "level": self.permissions,
            "option_id": chosen.option_id if chosen else None,
        })
        await self.await_pending_updates()
        await self.flush_pending()
        what = tool_call.title or tool_call.kind or "tool call"
        line = f"permissions {self.permissions} rejected: {what}"
        if line not in self.rejections:
            self.rejections.append(line)
        await self._record(IntermediateStep(kind=StepKind.interaction, text=line))
        if chosen is None:
            return RequestPermissionResponse(outcome=DeniedOutcome(outcome="cancelled"))
        return RequestPermissionResponse(
            outcome=AllowedOutcome(outcome="selected", option_id=chosen.option_id)
        )

    # ---- capabilities we don't advertise: safe stubs ----
    async def write_text_file(self, *args: Any, **kwargs: Any) -> None:
        """Reject file-write requests; capability not advertised.

        :param args: ignored positional ACP arguments.
        :param kwargs: ignored keyword ACP arguments.
        """
        del args, kwargs
        raise NotImplementedError("file write not supported by atelier harness")

    async def read_text_file(self, *args: Any, **kwargs: Any) -> None:
        """Reject file-read requests; capability not advertised.

        :param args: ignored positional ACP arguments.
        :param kwargs: ignored keyword ACP arguments.
        """
        del args, kwargs
        raise NotImplementedError("file read not supported by atelier harness")

    async def create_terminal(self, *args: Any, **kwargs: Any) -> None:
        """Reject terminal-create requests; capability not advertised.

        :param args: ignored positional ACP arguments.
        :param kwargs: ignored keyword ACP arguments.
        """
        del args, kwargs
        raise NotImplementedError("terminal not supported by atelier harness")

    async def terminal_output(self, *args: Any, **kwargs: Any) -> None:
        """Reject terminal-output requests; capability not advertised.

        :param args: ignored positional ACP arguments.
        :param kwargs: ignored keyword ACP arguments.
        """
        del args, kwargs
        raise NotImplementedError

    async def release_terminal(self, *args: Any, **kwargs: Any) -> None:
        """No-op release_terminal stub.

        :param args: ignored positional ACP arguments.
        :param kwargs: ignored keyword ACP arguments.
        """
        del args, kwargs
        return None

    async def wait_for_terminal_exit(self, *args: Any, **kwargs: Any) -> None:
        """Reject wait_for_terminal_exit; capability not advertised.

        :param args: ignored positional ACP arguments.
        :param kwargs: ignored keyword ACP arguments.
        """
        del args, kwargs
        raise NotImplementedError

    async def kill_terminal(self, *args: Any, **kwargs: Any) -> None:
        """No-op kill_terminal stub.

        :param args: ignored positional ACP arguments.
        :param kwargs: ignored keyword ACP arguments.
        """
        del args, kwargs
        return None

    async def ext_method(
        self, method: str, params: dict[str, Any]
    ) -> dict[str, Any]:
        """No-op handler for unknown ACP extension methods.

        :param method: the extension method name (unused).
        :param params: the extension method params (unused).
        :returns: an empty dict.
        """
        del method, params
        return {}

    async def ext_notification(
        self, method: str, params: dict[str, Any]
    ) -> None:
        """No-op handler for unknown ACP extension notifications.

        :param method: the notification method name (unused).
        :param params: the notification params (unused).
        """
        del method, params
        return None

    def on_connect(self, conn) -> None:
        """Store the ACP connection for later use.

        :param conn: the ACP connection handed to the client on attach.
        """
        self._conn = conn


class _SilentSink:
    """Keep a supervisor's internal JSON response out of the worker stream."""

    async def display(self, text: str) -> None:
        """Discard internal agent output."""

    async def request_input(self, prompt: str) -> str:
        """A supervisor cannot recursively ask for input."""
        raise RuntimeError("supervisor cannot request human input")


class AcpHarnessExecutor(ExecutorBase):
    """Executor that drives an ACP agent subprocess.

    :param launch_cmd: argv to spawn the ACP agent (e.g.
        ``["npx", "-y", "@agentclientprotocol/claude-agent-acp"]``)
    :param sink: :class:`PromptSink` for user I/O and permission requests
    :param done_marker: substring that terminates an interactive loop
    :param env: extra environment variables for the agent process, layered
        over the inherited environment
    :param model: model id to select on every session, or ``None`` for the
        agent's default
    :param effort: reasoning-effort id to select after the model, or ``None``
        to leave whatever that model defaults to
    """

    def __init__(
        self,
        launch_cmd: list[str],
        sink: PromptSink | None = None,
        done_marker: str | None = None,
        env: dict[str, str] | None = None,
        model: str | None = None,
        effort: str | None = None,
    ) -> None:
        """Initialize the harness executor.

        :param launch_cmd: argv used to spawn the ACP agent subprocess.
        :param sink: optional :class:`PromptSink` for user I/O; defaults to
            :class:`TerminalPromptSink`.
        :param done_marker: optional done-token override; defaults to
            :data:`DEFAULT_DONE_MARKER`.
        :param env: optional extra environment variables for the agent.
        :param model: optional model id to select once a session is open.
        :param effort: optional reasoning-effort id to select after the model.
        """
        if not launch_cmd:
            raise ValueError("launch_cmd must not be empty")
        self.launch_cmd = list(launch_cmd)
        self.sink = sink if sink is not None else TerminalPromptSink()
        self.done_marker = done_marker or DEFAULT_DONE_MARKER
        self.env = dict(env or {})
        self.model = model
        self.effort = effort

    def with_model(self, model: str, effort: str | None = None) -> AcpHarnessExecutor:
        """Return a copy of this executor pinned to ``model`` (and ``effort``).

        A copy, never a mutation: the registry hands the same executor object
        to every task naming that harness, so pinning in place would let one
        task's model leak into another's run.

        :param model: model id as the agent lists it.
        :param effort: reasoning-effort id as the agent lists it *for that
            model*, or ``None`` to leave the model's own default alone.
        :returns: a new executor sharing the sink, marker and environment.
        """
        return AcpHarnessExecutor(
            launch_cmd=self.launch_cmd,
            sink=self.sink,
            done_marker=self.done_marker,
            env=self.env,
            model=model,
            effort=effort,
        )

    def _spawn_env(self) -> dict[str, str]:
        """Build the environment the agent subprocess runs with.

        The ACP transport does not inherit our environment: left to itself it
        hands the agent a trimmed set (HOME, PATH, SHELL, TERM, USER, LOGNAME)
        and drops everything else. That silently breaks agents behind a proxy
        or custom CA, agents authenticated by API key or cloud provider, and
        any ``git push`` the agent tries (no ``SSH_AUTH_SOCK``). Pass the real
        environment, less Atelier's own secrets, then layer the agent's own
        variables on top.

        :returns: the environment mapping for the agent process.
        """
        return {**child_environ(), **self.env}

    def is_available(self) -> tuple[bool, str]:
        """Probe whether the harness's launch binary is on PATH.

        Checks ``launch_cmd[0]`` (the ``npx``/``opencode``/``copilot`` binary
        the subprocess will spawn) with :func:`shutil.which`, the
        cross-platform PATH lookup. Presence only — no auth/version checks.

        :returns: ``(True, "")`` when found, else ``(False, reason)`` naming
            the missing binary.
        """
        binary = self.launch_cmd[0]
        if shutil.which(binary) is None:
            return (False, f"`{binary}` not found on PATH")
        return (True, "")

    async def probe(
        self,
        cwd: str | None = None,
        timeout: float = PROBE_TIMEOUT_SECONDS,
        prompt: bool = False,
    ) -> ProbeResult:
        """Check that this harness's command starts and speaks ACP.

        Spawns the agent, completes the ``initialize`` handshake and opens a
        session, then stops. By default no prompt is sent, so the check costs
        no tokens. Opening the session is the point: it is the step that fails
        when the user has not logged in to the agent's own CLI, which is
        precisely the setup flow-atelier reports but never performs.

        Some failures only show when the agent sends a request to its model:
        an expired token, a used-up plan, an adapter too old for the current
        model. ``prompt`` sends :data:`PROBE_PROMPT` to catch those, at the
        cost of a few tokens.

        :param cwd: working directory for the agent; defaults to the process cwd.
        :param timeout: seconds to allow for the whole check. Generous by
            default because an npx-distributed agent may be fetching itself.
        :param prompt: also send a one-line test prompt and require a reply.
        :returns: a :class:`ProbeResult` describing how far the check got.
        """
        ready, reason = self.is_available()
        if not ready:
            return ProbeResult(ok=False, stage="path", detail=reason)
        agent_stderr: deque[str] = deque(maxlen=AGENT_STDERR_LINES)
        target = cwd or str(Path.cwd())
        try:
            result = await asyncio.wait_for(
                self._probe(target, agent_stderr, prompt), timeout=timeout
            )
        except TimeoutError:
            result = ProbeResult(
                ok=False,
                stage="handshake",
                detail=f"no ACP handshake within {timeout:.0f}s",
            )
        except Exception as exc:  # noqa: BLE001 — any spawn failure is a result
            result = ProbeResult(
                ok=False, stage="spawn", detail=f"{type(exc).__name__}: {exc}"
            )
        result.stderr = "\n".join(agent_stderr)
        return result

    async def _probe(
        self, cwd: str, agent_stderr: deque[str], prompt: bool = False
    ) -> ProbeResult:
        """Run the handshake half of :meth:`probe`.

        :param cwd: working directory for the agent process.
        :param agent_stderr: bounded deque the agent's stderr is drained into.
        :param prompt: also send :data:`PROBE_PROMPT` and require a reply.
        :returns: a :class:`ProbeResult` for the handshake.
        """
        import acp

        cmd, *args = self.launch_cmd
        cmd = _resolve_cmd(cmd)
        client = _ProbeClient()
        async with acp.spawn_agent_process(
            client,
            cmd,
            *args,
            cwd=cwd,
            env=self._spawn_env(),
            transport_kwargs={"limit": 8 * 1024 * 1024},
            observers=[client.observe],
        ) as (conn, proc):
            drain = (
                asyncio.create_task(_drain_agent_stderr(proc.stderr, agent_stderr))
                if proc.stderr is not None
                else None
            )
            try:
                try:
                    init = await conn.initialize(protocol_version=acp.PROTOCOL_VERSION)
                except Exception as exc:  # noqa: BLE001
                    return ProbeResult(
                        ok=False,
                        stage="initialize",
                        detail=(
                            f"started, but did not complete the ACP handshake "
                            f"({type(exc).__name__}: {exc})"
                        ),
                    )
                info = getattr(init, "agent_info", None)
                partial = ProbeResult(
                    ok=False,
                    stage="session",
                    detail="",
                    agent=f"{info.name} {info.version}".strip() if info else "",
                    protocol_version=init.protocol_version,
                    auth_methods=[m.id for m in (init.auth_methods or [])],
                )
                try:
                    sess = await conn.new_session(cwd=cwd)
                except Exception as exc:  # noqa: BLE001
                    partial.detail = (
                        f"speaks ACP, but could not open a session "
                        f"({type(exc).__name__}: {exc})"
                    )
                    return partial
                modes = getattr(sess, "modes", None)
                partial.modes = [m.id for m in (modes.available_modes if modes else [])]
                partial.permissive_mode = _pick_permissive_mode(modes) or ""
                partial.current_mode = modes.current_mode_id if modes else ""
                # Resolve model and effort exactly as a run would, so a check
                # of `harness:<name>:<model>` reports the efforts *that* model
                # offers, and a value the agent refuses is reported as the
                # selection error it is rather than as a broken session.
                try:
                    options = await self._apply_selection(conn, sess)
                except Exception as exc:  # noqa: BLE001 — any refusal is a result
                    partial.stage = "selection"
                    partial.detail = str(exc)
                    return partial
                partial.ok = True
                partial.stage = "ok"
                partial.detail = "reachable and ACP-compatible"
                choice = _model_choice(sess, options)
                if choice is not None:
                    partial.models = choice.available
                    # Selection has already succeeded, so a named model *is*
                    # the current one — the legacy `session/set_model` path
                    # answers with nothing, leaving `choice.current` on the
                    # model the session merely opened with.
                    partial.current_model = self.model or choice.current
                effort = _effort_choice(options)
                if effort is not None:
                    partial.efforts = effort.available
                    partial.current_effort = effort.current
                if prompt:
                    await self._probe_prompt(conn, sess.session_id, client, partial)
                return partial
            finally:
                if drain is not None:
                    with contextlib.suppress(TimeoutError, asyncio.CancelledError):
                        await asyncio.wait_for(
                            asyncio.shield(drain), STDERR_DRAIN_GRACE_SECONDS
                        )
                    drain.cancel()
                if sys.platform == "win32":
                    _close_proc_transports(proc)

    @staticmethod
    async def _probe_prompt(
        conn, session_id: str, client: _ProbeClient, result: ProbeResult
    ) -> None:
        """Send the test prompt and record whether the agent answered.

        :param conn: the active ACP connection.
        :param session_id: the open session to prompt.
        :param client: the probe client collecting the reply text.
        :param result: the passing result so far, updated in place.
        """
        from acp.schema import TextContentBlock

        try:
            resp = await conn.prompt(
                prompt=[TextContentBlock(type="text", text=PROBE_PROMPT)],
                session_id=session_id,
            )
        except Exception as exc:  # noqa: BLE001 — any refusal is a result
            data = getattr(exc, "data", None)
            detail = data.get("message", "") if isinstance(data, dict) else ""
            result.ok = False
            result.stage = "prompt"
            result.detail = (
                f"opened a session, but refused a test prompt "
                f"({type(exc).__name__}: {detail or exc})"
            )
            return
        await client.await_pending_updates()
        result.reply = "".join(client.text).strip()
        if resp.stop_reason != "end_turn" or not result.reply:
            result.ok = False
            result.stage = "prompt"
            result.detail = (
                f"opened a session, but the test prompt stopped with "
                f"reason={resp.stop_reason} and no reply"
            )
            return
        result.detail = "reachable, ACP-compatible, and answered a test prompt"

    async def execute(
        self,
        task: TaskDefinition,
        resolved_command: str,
        context: FlowContext,
    ) -> ExecutionResult:
        """Drive an ACP agent for one task, single-turn or interactive.

        :param task: the task definition; ``task.interactive`` selects the loop.
        :param resolved_command: the prompt text with templates resolved.
        :param context: runtime :class:`FlowContext` providing timeout/step flags.
        :returns: :class:`ExecutionResult` capturing buffered output and steps.
        """
        interactive = (
            task.interactive or context.interactive_harnesses
            or context.interaction.questions != "human"
        ) and not context.supervisor_run
        prompt_text = resolved_command
        if interactive:
            prompt_text = prompt_text + build_interactive_suffix(self.done_marker)

        # Absolute: ACP agents reject a relative `cwd` outright ("`cwd` must be
        # an absolute path"), and a relative --path or schedule run_path would
        # otherwise fail the whole task at session/new.
        cwd = str(Path(context.working_dir).resolve()) if context.working_dir else str(Path.cwd())
        client = _BufferingClient(
            _SilentSink() if context.supervisor_run else self.sink,
            stream_messages=interactive,
            stream_steps=(interactive or context.show_steps),
            done_marker=self.done_marker,
            on_step=context.on_step,
        )

        client.supervisor_run = context.supervisor_run
        client.approve_all = context.interaction.permissions == "approve_all"
        client.permissions = task.permissions or context.agent_permissions or "full"
        client.router = InteractionRouter(task, context, self.sink, client.session, client._record)
        agent_stderr: deque[str] = deque(maxlen=AGENT_STDERR_LINES)

        spawned_at = time.monotonic()
        try:
            result = await asyncio.wait_for(
                self._drive_session(
                    client, prompt_text, interactive, cwd, agent_stderr,
                    context.resume_session,
                ),
                timeout=context.timeout,
            )
        except TimeoutError:
            await client.flush_pending()
            result = ExecutionResult(
                exit_code=124,
                stdout="".join(client.buffer),
                stderr=f"harness timeout after {context.timeout}s",
                output="".join(client.buffer),
                steps=client.steps,
                usage=_usage_from_client(client),
            )
        except Exception as exc:  # noqa: BLE001
            await client.flush_pending()
            result = ExecutionResult(
                exit_code=1,
                stdout="".join(client.buffer),
                stderr=f"{type(exc).__name__}: {exc}",
                output="".join(client.buffer),
                steps=client.steps,
                usage=_usage_from_client(client),
                usage_limit=_usage_limit(exc, client),
            )
        # Interactive runs stream raw chunks; non-interactive runs stream
        # batched message blocks. Either way the output is already on screen.
        result.session = client.session
        result.timings = _timings(client, spawned_at)
        result.agent_session_id = client.agent_session_id
        result.session_continued = client.session_continued
        if client.interaction_error is not None:
            result.exit_code = 1
            result.stderr = f"interaction failed: {client.interaction_error}"
        if client.rejections and result.exit_code != 0:
            note = "\n".join(client.rejections)
            result.stderr = f"{result.stderr}\n{note}".strip() if result.stderr else note
        if client.streamed_message or interactive:
            result.live_streamed = True
        return _with_agent_stderr(result, agent_stderr)

    async def _drive_session(
        self,
        client: _BufferingClient,
        initial_prompt: str,
        interactive: bool,
        cwd: str,
        agent_stderr: deque[str],
        resume_session: str | None = None,
    ) -> ExecutionResult:
        """Spawn the agent, initialize a session, and run one or many turns.

        :param client: buffering ACP client receiving session updates.
        :param initial_prompt: first prompt to send (already suffix-augmented
            for interactive runs).
        :param interactive: ``True`` to loop until the done marker arrives.
        :param cwd: working directory passed to the agent process.
        :param agent_stderr: bounded deque the agent's stderr is drained into.
        :param resume_session: the agent's id of an earlier session to
            continue, or ``None`` to open a new one.
        :returns: :class:`ExecutionResult` from single-turn or interactive run.
        """
        import acp

        cmd, *args = self.launch_cmd
        cmd = _resolve_cmd(cmd)
        # Public stream-observer hook: lets the client count the session
        # updates a turn produced so it can wait for its own handlers.
        observers = [client.observe]
        if sys.platform == "win32":
            # Raise the per-line StreamReader limit well above asyncio's 64 KiB
            # default; Codex and similar harnesses emit large JSON-RPC frames
            # (tool results, planning output) that routinely exceed it.
            spawn = acp.spawn_agent_process(
                client, cmd, *args, cwd=cwd, env=self._spawn_env(),
                transport_kwargs={"limit": 8 * 1024 * 1024}, observers=observers,
            )
        else:
            spawn = _spawn_agent(
                client, cmd, *args, cwd=cwd, env=self._spawn_env(), observers=observers
            )
        succeeded = False
        agent_proc = None
        try:
            async with spawn as (conn, proc):
                agent_proc = proc
                # Start draining before `initialize`: a harness that dies during
                # the handshake (not authenticated, wrong version) writes its
                # only explanation to stderr on the way out.
                drain = (
                    asyncio.create_task(_drain_agent_stderr(proc.stderr, agent_stderr))
                    if proc.stderr is not None
                    else None
                )
                try:
                    init = await conn.initialize(protocol_version=acp.PROTOCOL_VERSION)
                    sess = await self._open_session(conn, init, cwd, client, resume_session)
                    if client.supervisor_run:
                        await self._enter_supervisor_mode(conn, sess)
                    elif client.permissions != "full":
                        await self._enter_limited_mode(
                            conn, sess, client.permissions, routed=not client.approve_all
                        )
                    elif not client.approve_all:
                        await self._ensure_prompting_mode(conn, sess)
                    else:
                        await self._maybe_switch_to_permissive_mode(conn, sess)
                    await self._apply_selection(conn, sess)
                    client.ready_at = time.monotonic()

                    if client.supervisor_run:
                        # Startup/mode announcements are not the decision response.
                        await client.await_pending_updates()
                        await client.flush_pending()
                        client.buffer.clear()
                    if not interactive:
                        result = await self._run_single_turn(
                            conn, sess.session_id, initial_prompt, client
                        )
                    else:
                        result = await self._run_interactive(
                            conn, sess.session_id, initial_prompt, client
                        )
                    succeeded = result.success
                    return result
                except asyncio.CancelledError:
                    # Stopped or timed out: end the agent and everything it
                    # started now, not after the shutdown waits.
                    if sys.platform != "win32":
                        _signal_agent_group(proc, signal.SIGTERM)
                    raise
                finally:
                    if drain is not None:
                        if not succeeded:
                            # A dead agent closes stderr, so the drain ends on its
                            # own; the short wait lets those final lines land
                            # before the transport tears the pipe down. A healthy
                            # agent keeps stderr open, and its stderr is not
                            # reported on success, so it is not waited for.
                            with contextlib.suppress(TimeoutError, asyncio.CancelledError):
                                await asyncio.wait_for(
                                    asyncio.shield(drain), STDERR_DRAIN_GRACE_SECONDS
                                )
                        drain.cancel()
                    if succeeded and sys.platform != "win32":
                        await _close_agent_quickly(proc)
                    if sys.platform == "win32":
                        _close_proc_transports(proc)
        finally:
            if not succeeded and agent_proc is not None and sys.platform != "win32":
                # A failed, stopped or timed-out turn must not leave the
                # tools, servers or shells the agent started running.
                _signal_agent_group(agent_proc, signal.SIGKILL)

    @staticmethod
    async def _open_session(
        conn, init, cwd: str, client: _BufferingClient, resume_session: str | None
    ):
        """Continue ``resume_session`` when the agent can, else open a new session.

        ``session/resume`` is preferred: it restores the conversation without
        replaying it. ``session/load`` replays the whole history as session
        updates, which are an earlier task's output and are dropped here. An
        agent that offers neither, or refuses this id (a session it has
        pruned, or one made on another machine), gets a new session: the task
        still runs, only without the earlier context, and the fallback is
        logged.

        :param conn: the active ACP connection.
        :param init: the agent's ``initialize`` response, carrying capabilities.
        :param cwd: absolute working directory for the session.
        :param client: buffering client; records the session id and replay state.
        :param resume_session: the agent's id of the session to continue, or
            ``None`` to open a new one.
        :returns: an object with ``session_id``, ``modes``, ``models`` and
            ``config_options``, like a ``NewSessionResponse``.
        """
        if resume_session is not None:
            caps = getattr(init, "agent_capabilities", None)
            sessions = getattr(caps, "session_capabilities", None)
            try:
                if getattr(sessions, "resume", None) is not None:
                    opened = await conn.resume_session(cwd=cwd, session_id=resume_session)
                elif getattr(caps, "load_session", False):
                    client.replaying = True
                    try:
                        opened = await conn.load_session(cwd=cwd, session_id=resume_session)
                        await client.await_pending_updates()
                    finally:
                        client.replaying = False
                else:
                    raise RuntimeError("the agent cannot reload sessions")
            except Exception as exc:  # noqa: BLE001 — any refusal means start fresh
                logger.warning(
                    "could not continue agent session %s (%s); starting a new one",
                    resume_session, exc,
                )
            else:
                client.agent_session_id = resume_session
                client.session_continued = True
                return SimpleNamespace(
                    session_id=resume_session,
                    modes=getattr(opened, "modes", None),
                    models=getattr(opened, "models", None),
                    config_options=getattr(opened, "config_options", None),
                )
        sess = await conn.new_session(cwd=cwd)
        client.agent_session_id = sess.session_id
        return sess

    @staticmethod
    async def _maybe_switch_to_permissive_mode(conn, sess) -> None:
        """Switch the session into the most permissive mode the agent offers.

        ACP session modes are the upstream "skip permissions" knob (see
        https://agentclientprotocol.com/protocol/session-modes). Each agent
        advertises its own mode ids (e.g. claude-agent-acp's
        ``bypassPermissions``) under ``new_session.modes``; see
        :data:`PERMISSIVE_MODE_KEYWORDS`. We pick the most permissive one and switch
        before the first prompt so the agent never has to call
        ``session/request_permission``. A bad mode switch is non-fatal —
        the auto-approve backstop in :meth:`_BufferingClient.request_permission`
        still catches any residual prompts.

        :param conn: the ACP connection used to issue ``set_session_mode``.
        :param sess: the new-session response carrying advertised mode ids.
        """
        modes = getattr(sess, "modes", None)
        picked = _pick_permissive_mode(modes)
        if picked is None or picked == modes.current_mode_id:
            return
        try:
            await conn.set_session_mode(mode_id=picked, session_id=sess.session_id)
        except Exception as exc:  # noqa: BLE001
            logger.debug("set_session_mode(%s) failed: %s", picked, exc)

    @staticmethod
    async def _enter_limited_mode(conn, sess, level: str, routed: bool = False) -> None:
        """Switch to the mode :func:`limited_mode` picks for a safer ``level``.

        Unlike the permissive switch this is not best-effort: a session left
        in a mode that never asks would do what ``level`` forbids.

        :param conn: the ACP connection used to issue ``set_session_mode``.
        :param sess: the new-session response carrying advertised mode ids.
        :param level: ``edits`` or ``read-only``.
        :param routed: a person or a supervisor answers the requests.
        :raises RuntimeError: when the session starts in a mode that skips
            permission requests and offers none to switch to, or the agent
            cannot be held to ``level`` (see :func:`_refuse_unenforced`).
        """
        modes = getattr(sess, "modes", None)
        if modes is None:
            _refuse_unenforced(level, "the agent offers no session modes")
            return
        picked = limited_mode(
            [m.id for m in modes.available_modes], modes.current_mode_id, level, routed
        )
        if not mode_enforces(picked, level, routed):
            _refuse_unenforced(
                level,
                f"the agent would run in its own mode {picked!r}, which is not "
                "a read-only mode or one known to ask before acting",
            )
        if picked != modes.current_mode_id:
            await conn.set_session_mode(mode_id=picked, session_id=sess.session_id)

    async def _apply_selection(self, conn, sess) -> list:
        """Select :attr:`model`, then :attr:`effort`, or fail before any prompt.

        Order matters and is not cosmetic. Which reasoning efforts exist is a
        property of the *chosen* model — Claude's Opus offers a different set
        from its Haiku — and ``session/set_config_option`` answers with the
        agent's full, authoritative config afterwards. So the model goes
        first and the effort is validated against what came back, never
        against the options the session happened to open with.

        A choice the user did not name is never set: an omitted model leaves
        the agent's own default, and an omitted effort leaves that model's.

        Unlike the mode switch this is not best-effort: the user named these,
        so running on something else would be a silent substitution.

        :param conn: the ACP connection.
        :param sess: the new-session response.
        :returns: the agent's latest config options — what it returned from
            the last setter, or the session's own when nothing was set.
        :raises RuntimeError: when the agent offers no such choice, does not
            list the named value, or cannot report the efforts of a model it
            switched to over the legacy ``session/set_model``.
        """
        options = list(getattr(sess, "config_options", None) or [])
        if self.model is None:
            return options

        choice = _model_choice(sess)
        if choice is None:
            raise RuntimeError(
                f"this harness does not support model selection; drop ':{self.model}' "
                "from the tool name"
            )
        if self.model not in choice.available:
            raise RuntimeError(
                f"model {self.model!r} is not offered by this harness; "
                f"available: {', '.join(choice.available)}"
            )
        stale = False
        if self.model != choice.current:
            if choice.config_id is not None:
                response = await conn.set_config_option(
                    config_id=choice.config_id,
                    session_id=sess.session_id,
                    value=self.model,
                )
                options = list(response.config_options)
            else:
                await conn.set_session_model(
                    model_id=self.model, session_id=sess.session_id
                )
                # ``session/set_model`` answers with nothing, so the options
                # still in hand describe the model we just left — drop them
                # rather than let a caller read them as the new model's.
                options = []
                stale = True

        if self.effort is None:
            return options
        if stale:
            raise RuntimeError(
                f"this harness switches models with the legacy `session/set_model`, "
                f"which returns no configuration, so the reasoning efforts it offers "
                f"for {self.model!r} cannot be read — the ones on hand belong to "
                f"{choice.current!r}. Drop ':{self.effort}' from the tool name, or "
                f"name a harness that advertises a model config option"
            )
        return await self._apply_effort(conn, sess, options)

    async def _apply_effort(self, conn, sess, options: list) -> list:
        """Select :attr:`effort` against the options the chosen model offers.

        :param conn: the ACP connection.
        :param sess: the new-session response.
        :param options: the agent's config options as of the model selection.
        :returns: the agent's config options after the effort was set — the
            setter's own answer, which is the only place the newly selected
            effort shows as current.
        :raises RuntimeError: when the agent offers no effort choice for this
            model, or does not list :attr:`effort` among them.
        """
        choice = _effort_choice(options)
        if choice is None:
            raise RuntimeError(
                f"model {self.model!r} on this harness offers no reasoning-effort "
                f"choice; drop ':{self.effort}' from the tool name"
            )
        if self.effort not in choice.available:
            raise RuntimeError(
                f"reasoning effort {self.effort!r} is not offered for model "
                f"{self.model!r}; available: {', '.join(choice.available)}"
            )
        if self.effort == choice.current:
            return options
        response = await conn.set_config_option(
            config_id=choice.config_id, session_id=sess.session_id, value=self.effort
        )
        return list(response.config_options)

    async def _enter_supervisor_mode(self, conn, sess) -> None:
        """Run a supervisor in a read-only mode when the agent offers one.

        A supervisor only reads the worker's session and answers; its own
        permission requests are always rejected. A read-only mode also stops
        an agent from changing the workspace in a mode that does not ask
        (Codex's ``agent``). Without one, it leaves bypass modes as before.

        :param conn: the ACP connection.
        :param sess: the new-session response carrying advertised mode ids.
        """
        modes = sess.modes
        if modes is not None:
            for mode in modes.available_modes:
                lowered = mode.id.lower()
                if any(k in lowered for k in LIMITED_MODE_KEYWORDS["read-only"]):
                    if mode.id != modes.current_mode_id:
                        await conn.set_session_mode(
                            session_id=sess.session_id, mode_id=mode.id
                        )
                    return
        await self._ensure_prompting_mode(conn, sess)

    @staticmethod
    async def _ensure_prompting_mode(conn, sess) -> None:
        """Leave bypass modes before applying a permission decision policy."""
        modes = sess.modes
        if modes is None:
            return
        current = modes.current_mode_id.lower()
        if not any(word in current for word in PERMISSIVE_MODE_KEYWORDS):
            return
        for mode in modes.available_modes:
            if mode.id.lower() in PROMPTING_MODE_IDS:
                await conn.set_session_mode(session_id=sess.session_id, mode_id=mode.id)
                return
        raise RuntimeError("permission policy requires a non-bypass harness session mode")

    async def _run_single_turn(
        self,
        conn,
        session_id: str,
        prompt_text: str,
        client: _BufferingClient,
    ) -> ExecutionResult:
        """Send one prompt and return the resulting :class:`ExecutionResult`.

        :param conn: the active ACP connection.
        :param session_id: the ACP session id to address.
        :param prompt_text: the prompt content to send.
        :param client: buffering client capturing streamed output.
        :returns: :class:`ExecutionResult` built from the agent's stop_reason.
        """
        from acp.schema import TextContentBlock

        client.session.append({"source": "user", "text": prompt_text})
        resp = await conn.prompt(
            prompt=[TextContentBlock(type="text", text=prompt_text)],
            session_id=session_id,
        )
        if resp.usage is not None:
            client.usage = resp.usage
        await client.await_pending_updates()
        await client.flush_pending()
        return self._result_for_turn(client, resp.stop_reason)

    async def _run_interactive(
        self,
        conn,
        session_id: str,
        initial_prompt: str,
        client: _BufferingClient,
    ) -> ExecutionResult:
        """Loop prompts and user replies until the done marker or limit.

        :param conn: the active ACP connection.
        :param session_id: the ACP session id to address.
        :param initial_prompt: the first prompt (already suffix-augmented).
        :param client: buffering client whose buffer is checked for the marker.
        :returns: :class:`ExecutionResult` with cleaned output on success, or
            an error result on input failure or turn-limit exhaustion.
        """
        from acp.schema import TextContentBlock

        next_prompt = initial_prompt
        last_stop = "end_turn"
        # Sinks that don't render visually (e.g. API/queue transports)
        # may omit start_agent_turn; only call it if implemented.
        start_turn = getattr(self.sink, "start_agent_turn", None)
        for _ in range(MAX_INTERACTIVE_TURNS):
            if start_turn is not None:
                await start_turn()
            prev_buffer_len = len(client.buffer)
            client.session.append({"source": "user", "text": next_prompt})
            resp = await conn.prompt(
                prompt=[TextContentBlock(type="text", text=next_prompt)],
                session_id=session_id,
            )
            # Take-last: usage/cost are documented as session totals, so the
            # final non-null value wins rather than summing per-turn deltas.
            if resp.usage is not None:
                client.usage = resp.usage
            await client.await_pending_updates()
            await client.flush_pending()
            if client.interaction_error is not None:
                raise RuntimeError(str(client.interaction_error))
            last_stop = resp.stop_reason
            buffer_text = "".join(client.buffer)
            # Only this turn's chunks count: a marker echoed or quoted in an
            # earlier turn must not terminate the session with stale output.
            turn_text = "".join(client.buffer[prev_buffer_len:])
            if self.done_marker in turn_text:
                # Strip the protocol sentinel from anything we hand back to
                # the user — it's an internal coordination marker, not content.
                cleaned = buffer_text.replace(self.done_marker, "").rstrip()
                last_turn = (
                    "".join(client.buffer[prev_buffer_len:])
                    .replace(self.done_marker, "")
                    .rstrip()
                )
                return ExecutionResult(
                    exit_code=0,
                    stdout=cleaned,
                    stderr="",
                    output=cleaned,
                    last_turn_output=last_turn,
                    steps=client.steps,
                    usage=_usage_from_client(client),
                )
            if resp.stop_reason == "max_tokens":
                # The reply was cut off mid-thought; nudge the agent to keep
                # going instead of handing the truncated turn back to the human.
                next_prompt = (
                    "Your previous response was cut off before it finished. "
                    "Continue from where you stopped."
                    + build_interactive_suffix(self.done_marker)
                )
                continue
            if resp.stop_reason != "end_turn":
                break
            try:
                if client.router is None:
                    user_reply = await self.sink.request_input(turn_text)
                else:
                    user_reply = await client.router.resolve("question", turn_text)
            except (EOFError, KeyboardInterrupt) as exc:
                return ExecutionResult(
                    exit_code=1,
                    stdout=buffer_text,
                    stderr=f"interactive input unavailable: {type(exc).__name__}",
                    output=buffer_text,
                    steps=client.steps,
                    usage=_usage_from_client(client),
                )
            next_prompt = user_reply + build_interactive_suffix(self.done_marker)

        buffer_text = "".join(client.buffer)
        return ExecutionResult(
            exit_code=1,
            stdout=buffer_text,
            stderr=(
                f"interactive session ended without done marker "
                f"(last stop_reason={last_stop})"
            ),
            output=buffer_text,
            steps=client.steps,
            usage=_usage_from_client(client),
        )

    @staticmethod
    def _result_for_turn(
        client: _BufferingClient, stop_reason: str
    ) -> ExecutionResult:
        """Build an :class:`ExecutionResult` from a finished single turn.

        :param client: buffering client holding the accumulated output/steps.
        :param stop_reason: the agent's reported ``stop_reason`` for the turn.
        :returns: :class:`ExecutionResult` with exit_code 0 on normal stops.
        """
        output = "".join(client.buffer)
        usage = _usage_from_client(client)
        if stop_reason == "end_turn":
            failure = _failure_in_words(client, output)
            if failure is not None:
                reason, limit = failure
                return ExecutionResult(
                    exit_code=1, stdout=output, stderr=reason, output=output,
                    steps=client.steps, usage=usage, usage_limit=limit,
                )
            # An empty success is indistinguishable from a real one unless we
            # note that the agent's only content was non-text (image/resource).
            stderr = (
                "agent produced only non-text content"
                if not output and client._saw_nontext_content
                else ""
            )
            return ExecutionResult(
                exit_code=0, stdout=output, stderr=stderr, output=output,
                steps=client.steps, usage=usage,
            )
        if stop_reason == "max_tokens":
            # Truncated output must not flow downstream as if complete.
            stderr = "agent hit max_tokens; output truncated"
        else:
            stderr = f"agent stopped with reason={stop_reason}"
        return ExecutionResult(
            exit_code=1,
            stdout=output,
            stderr=stderr,
            output=output,
            steps=client.steps,
            usage=usage,
        )
