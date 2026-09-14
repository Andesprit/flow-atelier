"""``/ws/run-conduit`` WebSocket route."""

from __future__ import annotations

import asyncio
import logging
import secrets
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from fastapi import APIRouter, WebSocket
from pydantic import TypeAdapter, ValidationError
from starlette.websockets import WebSocketDisconnect, WebSocketState

from flow_atelier.core.atelier import Atelier
from flow_atelier.modules.engine import (
    check_unknown_inputs,
    current_flow_id,
    current_task,
)
from flow_atelier.schemas.flow import new_flow_id, parse_flow_id
from flow_atelier.schemas.log import TaskEvent
from flow_atelier.schemas.progress import FlowStatus, TaskStatus
from flow_atelier.schemas.ws import (
    AgentInputAnswerMessage,
    CancelMessage,
    ClientMessage,
    HitlAnswerMessage,
    ResumeMessage,
    RunMessage,
)
from flow_atelier.services.api.base import get_atelier
from flow_atelier.services.api.ws_hitl import WsHitlExecutor
from flow_atelier.services.api.ws_manager import WebSocketBroker
from flow_atelier.services.api.ws_sink import WsPromptSink

logger = logging.getLogger(__name__)
router = APIRouter()
_client_message_adapter = TypeAdapter(ClientMessage)


def _step_status_for(event: TaskEvent) -> str:
    """Map a :class:`TaskEvent` to the WS step-status string.

    :param event: task event emitted by the engine.
    :returns: ``"completed"``, ``"failed"`` or the raw status value.
    """
    if event.status == TaskStatus.completed and event.success:
        return "completed"
    if event.status == TaskStatus.completed and not event.success:
        return "failed"
    return event.status.value


@router.websocket("/ws/run-conduit")
async def run_conduit_ws(websocket: WebSocket) -> None:
    """Accept a WS connection, multiplex flow runs over it.

    Builds a fresh :class:`Atelier` per connection so swapping
    ``executors["tool:hitl"]`` for a :class:`WsHitlExecutor` does not
    leak across sockets. When ``atelier.scheduler_bus`` is attached
    (i.e. running under ``atelier serve``), the socket also subscribes
    to scheduler broadcasts so scheduled fires reach the UI live.

    :param websocket: the incoming Starlette WebSocket connection.
    """
    base_atelier: Atelier = get_atelier(websocket)  # type: ignore[arg-type]

    # Browser WebSockets cannot set headers, so the bearer token (when
    # configured) is checked from the ?token= query parameter instead. The
    # check runs *before* accept() so an unauthenticated peer never completes
    # the upgrade (Starlette turns a pre-accept close into an HTTP 403 denial).
    expected_token = getattr(websocket.app.state, "api_token", "")
    if expected_token and not secrets.compare_digest(
        websocket.query_params.get("token", ""), expected_token
    ):
        await websocket.close(code=1008, reason="invalid or missing API token")
        return

    await websocket.accept()

    async def _send(payload: dict[str, Any]) -> None:
        """Send a JSON payload if the socket is still connected.

        :param payload: JSON-serializable envelope to deliver.
        """
        if websocket.application_state == WebSocketState.CONNECTED:
            await websocket.send_json(payload)
        else:
            logger.debug(
                "dropping %s envelope: socket not connected",
                payload.get("type", "?"),
            )

    broker = WebSocketBroker(send=_send)
    scheduler_bus = getattr(base_atelier, "scheduler_bus", None)
    if scheduler_bus is not None:
        scheduler_bus.subscribe(_send)

    try:
        while True:
            try:
                raw = await websocket.receive_json()
            except WebSocketDisconnect:
                break
            try:
                message = _client_message_adapter.validate_python(raw)
            except ValidationError as e:
                await _send(
                    {
                        "type": "error",
                        "message": f"invalid envelope: {e.errors()[0]['msg']}",
                    }
                )
                continue
            if isinstance(message, RunMessage):
                await _spawn_run(base_atelier, broker, message)
            elif isinstance(message, ResumeMessage):
                await _spawn_resume(base_atelier, broker, message)
            elif isinstance(message, HitlAnswerMessage):
                try:
                    await broker.deliver_hitl_answer(
                        message.flow_id, dict(message.answers)
                    )
                except KeyError:
                    await _send(
                        {
                            "type": "error",
                            "flow_id": message.flow_id,
                            "message": "no flow registered for hitl_answer",
                        }
                    )
            elif isinstance(message, AgentInputAnswerMessage):
                try:
                    await broker.deliver_agent_input_answer(
                        message.flow_id, message.request_id, message.answer
                    )
                except KeyError:
                    # Unknown id, wrong flow, or a turn that already moved on.
                    await _send(
                        {
                            "type": "error",
                            "flow_id": message.flow_id,
                            "message": (
                                "no pending agent input request "
                                f"{message.request_id!r} for this flow"
                            ),
                        }
                    )
            elif isinstance(message, CancelMessage):
                broker.cancel(message.flow_id)
    finally:
        # A client going away must not leave its runs executing detached: cancel
        # every tracked run task, which also unblocks any tool:hitl task waiting
        # on an answer that can no longer be delivered over this dead socket.
        broker.cancel_all()
        if scheduler_bus is not None:
            scheduler_bus.unsubscribe(_send)
        try:
            if websocket.application_state == WebSocketState.CONNECTED:
                await websocket.close()
        except RuntimeError:
            pass


# ── Shared WS helpers ─────────────────────────────────────────────────────


def _wire_atelier(
    base_atelier: Atelier,
    broker: WebSocketBroker,
    flow_id: str,
) -> tuple[Atelier, Callable[[TaskEvent], None], Callable[[str, str], None], Callable[[str], None]]:
    """Create a per-flow Atelier wired for WebSocket broadcasting.

    :param base_atelier: connection-scoped Atelier used as template.
    :param broker: WebSocketBroker for fan-out.
    :param flow_id: flow identifier for this run.
    :returns: the atelier, task-event callback, task-starting callback,
        and flow-started callback.
    """
    # Inherit the connection's settings rather than re-rooting at the global
    # dir: a run must resolve conduits from the same project+global stores that
    # produced the ``GET /conduits`` list the client picked from. The per-flow
    # Atelier exists only to own a flow-scoped prompt sink and HITL executor.
    atelier = Atelier(
        settings=base_atelier.settings,
        prompt_sink=WsPromptSink(broker, flow_id),
    )
    broker.register_flow(flow_id)
    atelier.executors["tool:hitl"] = WsHitlExecutor(
        broker=broker, flow_id=flow_id
    )

    # Hold strong references to fire-and-forget broadcast tasks: the event
    # loop keeps only a weak reference to a bare create_task, so without this
    # a send can be garbage-collected mid-flight and silently dropped.
    pending: set[asyncio.Task] = set()

    def _spawn(coro: Awaitable[None]) -> None:
        """Schedule a broadcast coroutine while retaining a strong reference.

        :param coro: the broadcast coroutine to run detached.
        """
        task = asyncio.create_task(coro)
        pending.add(task)
        task.add_done_callback(pending.discard)

    async def _on_task_event(event: TaskEvent) -> None:
        """Emit per-step and step-status envelopes for a task event.

        Uses ``event.flow_id`` so nested conduit events carry the child's
        flow identity instead of the parent's.

        :param event: task event produced by the engine.
        """
        fid = event.flow_id or flow_id
        if not event.tool.startswith("harness:"):
            for step in event.steps:
                await broker.send(
                    {
                        "type": "step",
                        "flow_id": fid,
                        "task": event.task,
                        "step": step.model_dump(mode="json"),
                    }
                )
        await broker.send(
            {
                "type": "step_status",
                "flow_id": fid,
                "step": event.task,
                "status": _step_status_for(event),
            }
        )

    def on_task_event_sync(event: TaskEvent) -> None:
        """Schedule the async task-event handler from sync engine code.

        :param event: task event produced by the engine.
        """
        _spawn(_on_task_event(event))

    def on_task_starting(name: str, tool: str) -> None:
        """Emit a step_status=running envelope when a task starts.

        Reads ``current_flow_id()`` so nested conduit tasks are attributed
        to the child flow.

        :param name: task name entering the running state.
        :param tool: tool kind string for the task.
        """
        fid = current_flow_id(flow_id)
        _spawn(
            broker.send(
                {
                    "type": "step_status",
                    "flow_id": fid,
                    "step": name,
                    "status": "running",
                }
            )
        )

    def on_flow_started(child_flow_id: str) -> None:
        """Send a ``started`` envelope for a child (sub-conduit) flow.

        The top-level flow's ``started`` is already emitted by
        :func:`_drive_lifecycle`, so this only fires for child flows, whose
        message carries ``parent_flow_id``/``parent_task`` for nested display.
        Emitting it for the top-level too produced a duplicate ``started`` that
        the frontend rendered as a spurious "flow resumed" on fresh runs.

        :param child_flow_id: the flow id of the child flow that just started.
        """
        if child_flow_id == flow_id:
            return
        broker.register_flow(child_flow_id)
        cname = ""
        try:
            cname, _, _ = parse_flow_id(child_flow_id)
        except ValueError:
            pass
        parent_task = current_task("") or None
        _spawn(
            broker.send(
                {
                    "type": "started",
                    "flow_id": child_flow_id,
                    "parent_flow_id": flow_id,
                    "parent_task": parent_task,
                    "conduit_name": cname,
                }
            )
        )

    return atelier, on_task_event_sync, on_task_starting, on_flow_started


async def _send_children_lifecycle(
    atelier: Atelier,
    broker: WebSocketBroker,
    flow_id: str,
) -> None:
    """Recursively send logs + lifecycle events for child flows (deepest first).

    Gracefully handles flows that were never persisted to disk (e.g. when
    the conduit was not found).

    :param atelier: per-flow Atelier instance.
    :param broker: WebSocketBroker for fan-out.
    :param flow_id: parent flow whose children to process.
    """
    try:
        child_ids = atelier.store.list_child_flows(flow_id)
    except FileNotFoundError:
        return
    for child_id in child_ids:
        await _send_children_lifecycle(atelier, broker, child_id)
        for entry in atelier.store.read_logs(child_id):
            await broker.send(
                {
                    "type": "log",
                    "flow_id": child_id,
                    "entry": entry.model_dump(mode="json"),
                }
            )
        try:
            progress = atelier.store.read_progress(child_id)
            if progress.status == FlowStatus.completed:
                await broker.send({"type": "flow_complete", "flow_id": child_id})
            else:
                await broker.send(
                    {"type": "flow_failed", "flow_id": child_id, "error": "child flow failed"}
                )
        except (FileNotFoundError, ValueError):
            await broker.send(
                {"type": "flow_failed", "flow_id": child_id, "error": "status unknown"}
            )
        broker.unregister_flow(child_id)


async def _drive_lifecycle(
    atelier: Atelier,
    broker: WebSocketBroker,
    flow_id: str,
    coro: Awaitable[None],
) -> None:
    """Drive a flow to completion and broadcast lifecycle envelopes.

    Sends ``started``, awaits *coro*, then streams logs and sends
    ``flow_complete``.  On cancellation or error sends ``flow_failed``.
    Child flow logs and lifecycle are sent before the parent's own.
    Unregisters the flow from the broker on completion.

    :param atelier: per-flow Atelier instance.
    :param broker: WebSocketBroker for fan-out.
    :param flow_id: flow identifier for this run.
    :param coro: the engine run or resume coroutine.
    """
    try:
        await broker.send({"type": "started", "flow_id": flow_id})
        try:
            await coro
        except asyncio.CancelledError:
            await _send_children_lifecycle(atelier, broker, flow_id)
            await broker.send(
                {"type": "flow_failed", "flow_id": flow_id, "error": "cancelled"}
            )
            raise
        except Exception as e:  # noqa: BLE001
            await _send_children_lifecycle(atelier, broker, flow_id)
            await broker.send(
                {"type": "flow_failed", "flow_id": flow_id, "error": str(e)}
            )
            return

        await _send_children_lifecycle(atelier, broker, flow_id)

        for entry in atelier.store.read_logs(flow_id):
            await broker.send(
                {
                    "type": "log",
                    "flow_id": flow_id,
                    "entry": entry.model_dump(mode="json"),
                }
            )
        await broker.send({"type": "flow_complete", "flow_id": flow_id})
    finally:
        broker.unregister_flow(flow_id)


# ── Spawn entry-points ────────────────────────────────────────────────────


async def _spawn_run(
    base_atelier: Atelier,
    broker: WebSocketBroker,
    message: RunMessage,
) -> None:
    """Wire a per-flow Atelier and start the run task.

    Trust model: ``message.run_path`` becomes the execution ``working_dir``
    verbatim, so a caller who can post a ``RunMessage`` chooses where bash and
    harness tasks run. This is the same boundary as ``tool:bash`` and
    ``open-path`` — anyone who can reach this endpoint can already run shell —
    so it grants no new privilege, but the cwd is intentionally unconstrained.

    :param base_atelier: connection-scoped :class:`Atelier` used as template.
    :param broker: :class:`WebSocketBroker` that fans envelopes out.
    :param message: :class:`RunMessage` describing what to run.
    """
    flow_id = new_flow_id(message.conduit_name)
    atelier, on_event, on_starting, on_started = _wire_atelier(base_atelier, broker, flow_id)

    async def _run() -> None:
        conduit = atelier.store.read_conduit(message.conduit_name)
        # Before the engine, which would drop the key: the client gets
        # flow_failed naming it instead of "missing" or a silent default.
        check_unknown_inputs(
            conduit, message.inputs, subject=f"run of {message.conduit_name!r}"
        )
        await atelier.engine.run(
            conduit,
            dict(message.inputs),
            on_task_event=on_event,
            on_task_starting=on_starting,
            on_flow_started=on_started,
            working_dir=Path(message.run_path) if message.run_path else None,
            flow_id=flow_id,
        )

    task = asyncio.create_task(_drive_lifecycle(atelier, broker, flow_id, _run()))
    broker.track_run(flow_id, task)


async def _spawn_resume(
    base_atelier: Atelier,
    broker: WebSocketBroker,
    message: ResumeMessage,
) -> None:
    """Wire a per-flow Atelier and resume a failed run.

    :param base_atelier: connection-scoped :class:`Atelier` used as template.
    :param broker: :class:`WebSocketBroker` that fans envelopes out.
    :param message: :class:`ResumeMessage` with the flow id to resume.
    """
    flow_id = message.flow_id
    atelier, on_event, on_starting, on_started = _wire_atelier(base_atelier, broker, flow_id)

    async def _resume() -> None:
        await atelier.resume_flow(
            flow_id,
            on_task_event=on_event,
            on_task_starting=on_starting,
            on_flow_started=on_started,
        )

    task = asyncio.create_task(_drive_lifecycle(atelier, broker, flow_id, _resume()))
    broker.track_run(flow_id, task)
