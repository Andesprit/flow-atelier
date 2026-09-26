"""``/ws/flows/<flow_id>`` WebSocket route: the run page's live feed."""
from __future__ import annotations

import asyncio
import logging
import secrets
from collections.abc import Callable
from typing import Any

from fastapi import APIRouter, WebSocket
from starlette.websockets import WebSocketDisconnect

from flow_atelier.services.api.base import get_atelier, origin_allowed
from flow_atelier.services.api.flow_watch import FlowWatcher

logger = logging.getLogger(__name__)
router = APIRouter()

# How often the run's files are checked; the same cadence as `atelier logs --follow`.
TICK_SECONDS = 0.25

def _log_failure(task: asyncio.Task) -> None:
    """Log why a feed's follower stopped, unless the client simply left.

    :param task: the finished follower task.
    """
    if task.cancelled():
        return
    error = task.exception()
    if error is not None and not isinstance(error, WebSocketDisconnect):
        logger.warning("run feed stopped: %r", error)


@router.websocket("/ws/flows/{flow_id}")
async def watch_flow_ws(websocket: WebSocket, flow_id: str) -> None:
    """Push one run's map, and the log of the task the page watches, as they change.

    The client sends ``{"type": "watch", "task": <name>}`` to pick a task. The
    server sends ``flow`` (the map), ``task_log`` (a task's whole log),
    ``task_update`` (lines a task gained) and ``error`` envelopes.

    :param websocket: the incoming Starlette WebSocket connection.
    :param flow_id: flow identifier from the URL path.
    """
    expected_token = getattr(websocket.app.state, "api_token", "")
    if expected_token and not secrets.compare_digest(
        websocket.query_params.get("token", ""), expected_token
    ):
        await websocket.close(code=1008, reason="invalid or missing API token")
        return
    if not origin_allowed(websocket):
        await websocket.close(code=1008, reason="origin not allowed")
        return

    await websocket.accept()
    try:
        watcher = FlowWatcher(get_atelier(websocket), flow_id)
    except FileNotFoundError as e:
        await websocket.send_json({"type": "error", "message": str(e)})
        await websocket.close()
        return
    await websocket.send_json({"type": "flow", "flow": watcher.view.model_dump(mode="json")})

    # Held across each change to the watcher and the sends that report it, so
    # an update never reaches the page ahead of the snapshot it builds on.
    lock = asyncio.Lock()

    async def _guarded(change: Callable[[], list[dict[str, Any]]]) -> None:
        """Apply ``change`` and send what it returns, one caller at a time.

        :param change: returns the envelopes to send.
        """
        async with lock:
            for envelope in change():
                await websocket.send_json(envelope)

    async def _listen() -> None:
        """Switch the watched task on each ``watch`` message."""
        while True:
            message = await websocket.receive_json()
            task = message.get("task") if isinstance(message, dict) else None
            if not isinstance(message, dict) or message.get("type") != "watch" or not task:
                await websocket.send_json(
                    {"type": "error", "message": 'expected {"type": "watch", "task": <name>}'}
                )
                continue

            def _watch(task: str = task) -> list[dict[str, Any]]:
                try:
                    log = watcher.watch(task)
                except KeyError:
                    return [{"type": "error", "message": f"task not found: {task}"}]
                return [{"type": "task_log", "log": log.model_dump(mode="json")}]

            await _guarded(_watch)

    async def _follow() -> None:
        """Send whatever changed on disk, every tick, until the flow is gone."""
        while True:
            await asyncio.sleep(TICK_SECONDS)
            try:
                await _guarded(watcher.poll)
            except FileNotFoundError as e:
                await websocket.send_json({"type": "error", "message": str(e)})
                return

    # Nothing awaits in the cleanup below: a disconnect or a server shutdown
    # can cancel this handler at any point, and it must leave promptly.
    follower = asyncio.create_task(_follow())
    follower.add_done_callback(_log_failure)
    try:
        await _listen()
    except WebSocketDisconnect:
        pass
    finally:
        follower.cancel()
