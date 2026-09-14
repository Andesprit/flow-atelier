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
import shutil
import sys
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import acp
from acp.connection import StreamDirection, StreamEvent
from acp.schema import (
    AgentMessageChunk,
    AgentThoughtChunk,
    AllowedOutcome,
    DeniedOutcome,
    RequestPermissionResponse,
    SessionModeState,
    TextContentBlock,
    ToolCallProgress,
    ToolCallStart,
    Usage,
    UsageUpdate,
)

from flow_atelier.schemas.conduit import TaskDefinition
from flow_atelier.schemas.log import (
    ExecutionResult,
    IntermediateStep,
    StepKind,
    TurnUsage,
)
from flow_atelier.services.executor.base import ExecutorBase, FlowContext
from flow_atelier.services.executor.interaction import InteractionRouter
from flow_atelier.services.executor.prompt_sink import (
    PromptSink,
    TerminalPromptSink,
)

logger = logging.getLogger(__name__)


def _resolve_cmd(cmd: str) -> str:
    """On Windows, resolve .cmd/.bat wrappers that ``create_subprocess_exec`` can't find."""
    if os.name == "nt":
        resolved = shutil.which(cmd)
        if resolved is not None:
            return resolved
    return cmd


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

# Bound on waiting for a turn's session-update handlers to finish. Reached
# only if a notification is lost, in which case a run must not stall until
# the task timeout fires.
UPDATE_DRAIN_TIMEOUT_SECONDS = 5.0

# Budget for a connection check. Generous: the first run of an
# npx/uvx-distributed agent downloads the package before it says anything.
PROBE_TIMEOUT_SECONDS = 90.0


@dataclass
class ProbeResult:
    """Outcome of a harness connection check.

    :param ok: whether the agent is reachable and completed the handshake.
    :param stage: how far the check got — ``path``, ``spawn``, ``initialize``,
        ``handshake`` (timed out), ``session``, or ``ok``.
    :param detail: one-line human explanation of ``stage``.
    :param agent: the agent's self-reported name and version, when it got
        far enough to send one.
    :param protocol_version: the ACP version the agent negotiated.
    :param auth_methods: auth method ids the agent advertises. Reported only
        — logging in is the user's job, done with the agent's own CLI.
    :param modes: session mode ids the agent offers.
    :param permissive_mode: the mode a real run would switch to, if any.
    :param stderr: tail of the agent's stderr, the usual place a failing
        agent explains itself.
    """

    ok: bool
    stage: str
    detail: str
    agent: str = ""
    protocol_version: int | None = None
    auth_methods: list[str] = field(default_factory=list)
    modes: list[str] = field(default_factory=list)
    permissive_mode: str = ""
    stderr: str = ""


class _ProbeClient:
    """ACP client for a connection check: accepts everything, does nothing.

    A probe never sends a prompt, so the agent has no reason to call back.
    Permission requests are denied rather than auto-approved: a check must
    not authorize an agent to touch anything.
    """

    async def session_update(self, *args: Any, **kwargs: Any) -> None:
        """Ignore a session update.

        :param args: ignored positional ACP arguments.
        :param kwargs: ignored keyword ACP arguments.
        """
        del args, kwargs
        return None

    async def request_permission(
        self, *args: Any, **kwargs: Any
    ) -> RequestPermissionResponse:
        """Refuse any permission request raised during a check.

        :param args: ignored positional ACP arguments.
        :param kwargs: ignored keyword ACP arguments.
        :returns: a cancelled :class:`DeniedOutcome`.
        """
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

# Mode-id keyword tiers in descending permissiveness. Each ACP agent
# advertises its own mode ids; we pick the most permissive one available so
# the agent never has to ask for tool permission. Keyword matched against
# ``id`` case-insensitively.
PERMISSIVE_MODE_KEYWORDS: tuple[str, ...] = (
    "bypass",      # claude-code-acp's bypassPermissions
    "yolo",
    "danger",      # codex-acp's danger-full-access family
    "full-auto",
    "full_auto",
    "accept",      # claude-code-acp's acceptEdits (weaker, but still skips edit prompts)
    "auto",
)


def _pick_permissive_mode(state: SessionModeState | None) -> str | None:
    """Return the most permissive mode id from ``state``, or ``None``.

    Walks :data:`PERMISSIVE_MODE_KEYWORDS` and returns the first
    available mode whose ``id`` contains a keyword (case-insensitive).

    :param state: ACP session mode state advertised by the agent, or ``None``.
    :returns: the most permissive matching mode id, or ``None`` if no match.
    """
    if state is None or not state.available_modes:
        return None
    available = state.available_modes
    for keyword in PERMISSIVE_MODE_KEYWORDS:
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
        if (
            event.direction is StreamDirection.INCOMING
            and event.message.get("method") == "session/update"
        ):
            self._updates_received += 1
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
            await self._record(
                IntermediateStep(
                    kind=StepKind.tool_call,
                    tool_call_id=update.tool_call_id,
                    tool_name=update.title,
                    tool_kind=update.kind or "",
                    tool_status=update.status or "",
                    tool_input=_payload_snippet(update.raw_input),
                    locations=[loc.path for loc in (update.locations or [])],
                )
            )
        elif isinstance(update, ToolCallProgress):
            if update.status in ("completed", "failed"):
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

    async def request_permission(
        self, options, session_id: str, tool_call, **kwargs
    ) -> RequestPermissionResponse:
        """Route tool permissions, retaining automatic approval for legacy runs.

        :param options: list of permission options offered by the agent.
        :param session_id: id of the requesting session (unused).
        :param tool_call: the complete pending tool call.
        :param kwargs: additional ACP fields (unused).
        :returns: an :class:`AllowedOutcome` selecting the chosen option, or
            a :class:`DeniedOutcome` if no allow option is available.
        """
        self.session.append({
            "source": "permission_request", "tool_call": tool_call.model_dump(mode="json"),
            "options": [o.model_dump(mode="json") for o in options],
        })
        if self.supervisor_run or self.interaction_error is not None:
            return RequestPermissionResponse(outcome=DeniedOutcome(outcome="cancelled"))
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
    """

    def __init__(
        self,
        launch_cmd: list[str],
        sink: PromptSink | None = None,
        done_marker: str | None = None,
        env: dict[str, str] | None = None,
    ) -> None:
        """Initialize the harness executor.

        :param launch_cmd: argv used to spawn the ACP agent subprocess.
        :param sink: optional :class:`PromptSink` for user I/O; defaults to
            :class:`TerminalPromptSink`.
        :param done_marker: optional done-token override; defaults to
            :data:`DEFAULT_DONE_MARKER`.
        :param env: optional extra environment variables for the agent.
        """
        if not launch_cmd:
            raise ValueError("launch_cmd must not be empty")
        self.launch_cmd = list(launch_cmd)
        self.sink = sink if sink is not None else TerminalPromptSink()
        self.done_marker = done_marker or DEFAULT_DONE_MARKER
        self.env = dict(env or {})

    def _spawn_env(self) -> dict[str, str]:
        """Build the environment the agent subprocess runs with.

        The ACP transport does not inherit our environment: left to itself it
        hands the agent a trimmed set (HOME, PATH, SHELL, TERM, USER, LOGNAME)
        and drops everything else. That silently breaks agents behind a proxy
        or custom CA, agents authenticated by API key or cloud provider, and
        any ``git push`` the agent tries (no ``SSH_AUTH_SOCK``). Pass the real
        environment, then layer the agent's own variables on top.

        :returns: the environment mapping for the agent process.
        """
        return {**os.environ, **self.env}

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
        self, cwd: str | None = None, timeout: float = PROBE_TIMEOUT_SECONDS
    ) -> ProbeResult:
        """Check that this harness's command starts and speaks ACP.

        Spawns the agent, completes the ``initialize`` handshake and opens a
        session, then stops. No prompt is sent, so the check costs no tokens.
        Opening the session is the point: it is the step that fails when the
        user has not logged in to the agent's own CLI, which is precisely the
        setup flow-atelier reports but never performs.

        :param cwd: working directory for the agent; defaults to the process cwd.
        :param timeout: seconds to allow for the whole handshake. Generous by
            default because an npx-distributed agent may be fetching itself.
        :returns: a :class:`ProbeResult` describing how far the check got.
        """
        ready, reason = self.is_available()
        if not ready:
            return ProbeResult(ok=False, stage="path", detail=reason)
        agent_stderr: deque[str] = deque(maxlen=AGENT_STDERR_LINES)
        target = cwd or str(Path.cwd())
        try:
            result = await asyncio.wait_for(
                self._probe(target, agent_stderr), timeout=timeout
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

    async def _probe(self, cwd: str, agent_stderr: deque[str]) -> ProbeResult:
        """Run the handshake half of :meth:`probe`.

        :param cwd: working directory for the agent process.
        :param agent_stderr: bounded deque the agent's stderr is drained into.
        :returns: a :class:`ProbeResult` for the handshake.
        """
        cmd, *args = self.launch_cmd
        cmd = _resolve_cmd(cmd)
        async with acp.spawn_agent_process(
            _ProbeClient(),
            cmd,
            *args,
            cwd=cwd,
            env=self._spawn_env(),
            transport_kwargs={"limit": 8 * 1024 * 1024},
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
                partial.ok = True
                partial.stage = "ok"
                partial.detail = "reachable and ACP-compatible"
                partial.modes = [m.id for m in (modes.available_modes if modes else [])]
                partial.permissive_mode = _pick_permissive_mode(modes) or ""
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
        client.router = InteractionRouter(task, context, self.sink, client.session, client._record)
        agent_stderr: deque[str] = deque(maxlen=AGENT_STDERR_LINES)

        try:
            result = await asyncio.wait_for(
                self._drive_session(
                    client, prompt_text, interactive, cwd, agent_stderr
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
            )
        # Interactive runs stream raw chunks; non-interactive runs stream
        # batched message blocks. Either way the output is already on screen.
        result.session = client.session
        if client.interaction_error is not None:
            result.exit_code = 1
            result.stderr = f"interaction failed: {client.interaction_error}"
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
    ) -> ExecutionResult:
        """Spawn the agent, initialize a session, and run one or many turns.

        :param client: buffering ACP client receiving session updates.
        :param initial_prompt: first prompt to send (already suffix-augmented
            for interactive runs).
        :param interactive: ``True`` to loop until the done marker arrives.
        :param cwd: working directory passed to the agent process.
        :param agent_stderr: bounded deque the agent's stderr is drained into.
        :returns: :class:`ExecutionResult` from single-turn or interactive run.
        """
        cmd, *args = self.launch_cmd
        cmd = _resolve_cmd(cmd)
        # Raise the per-line StreamReader limit well above asyncio's 64 KiB
        # default; Codex and similar harnesses emit large JSON-RPC frames
        # (tool results, planning output) that routinely exceed it.
        async with acp.spawn_agent_process(
            client,
            cmd,
            *args,
            cwd=cwd,
            env=self._spawn_env(),
            transport_kwargs={"limit": 8 * 1024 * 1024},
            # Public stream-observer hook: lets the client count the session
            # updates a turn produced so it can wait for its own handlers.
            observers=[client.observe],
        ) as (
            conn,
            proc,
        ):
            # Start draining before `initialize`: a harness that dies during
            # the handshake (not authenticated, wrong version) writes its
            # only explanation to stderr on the way out.
            drain = (
                asyncio.create_task(_drain_agent_stderr(proc.stderr, agent_stderr))
                if proc.stderr is not None
                else None
            )
            try:
                await conn.initialize(protocol_version=acp.PROTOCOL_VERSION)
                sess = await conn.new_session(cwd=cwd)
                if client.approve_all and not client.supervisor_run:
                    await self._maybe_switch_to_permissive_mode(conn, sess)
                else:
                    await self._ensure_prompting_mode(conn, sess)

                if client.supervisor_run:
                    # Startup/mode announcements are not the decision response.
                    await client.await_pending_updates()
                    await client.flush_pending()
                    client.buffer.clear()
                if not interactive:
                    return await self._run_single_turn(
                        conn, sess.session_id, initial_prompt, client
                    )
                return await self._run_interactive(
                    conn, sess.session_id, initial_prompt, client
                )
            finally:
                if drain is not None:
                    # A dead agent closes stderr, so the drain ends on its
                    # own; the short wait lets those final lines land before
                    # the transport tears the pipe down.
                    with contextlib.suppress(TimeoutError, asyncio.CancelledError):
                        await asyncio.wait_for(
                            asyncio.shield(drain), STDERR_DRAIN_GRACE_SECONDS
                        )
                    drain.cancel()
                if sys.platform == "win32":
                    _close_proc_transports(proc)

    @staticmethod
    async def _maybe_switch_to_permissive_mode(conn, sess) -> None:
        """Switch the session into the most permissive mode the agent offers.

        ACP session modes are the upstream "skip permissions" knob (see
        https://agentclientprotocol.com/protocol/session-modes). Each agent
        advertises its own mode ids (e.g. claude-code-acp's
        ``bypassPermissions``, codex-acp's ``danger-full-access``) under
        ``new_session.modes``. We pick the most permissive one and switch
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
    async def _ensure_prompting_mode(conn, sess) -> None:
        """Leave bypass modes before applying a permission decision policy."""
        modes = sess.modes
        if modes is None:
            return
        current = modes.current_mode_id.lower()
        if not any(word in current for word in PERMISSIVE_MODE_KEYWORDS):
            return
        for mode in modes.available_modes:
            if mode.id.lower() in {"default", "normal", "ask", "code"}:
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
