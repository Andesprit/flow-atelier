"""A dashboard tab may detach. The server owns its ACP connection and current turn."""

from __future__ import annotations

import asyncio
import sys
import time
from collections import deque
from contextlib import AsyncExitStack, suppress
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any
from uuid import uuid4

import acp
from acp.schema import TextContentBlock

from flow_atelier.services.executor.harness import (
    AcpHarnessExecutor,
    _drain_agent_stderr,
    _ProbeClient,
    _resolve_cmd,
    _spawn_agent,
)


class ChatSession(_ProbeClient):
    def __init__(self, harness: str, executor: AcpHarnessExecutor, project: Path):
        super().__init__()
        self.id = uuid4().hex
        self.harness = harness
        self.project = project
        self.executor = executor
        self.session_id: str | None = None
        self.conn = None
        self.proc = None
        self.stack = AsyncExitStack()
        self.directory = TemporaryDirectory(prefix="atelier-chat-")
        self.workspace = self.directory.name
        self.last_seen = time.monotonic()
        self.cancelling = False
        self.turns: dict[str, dict] = {}
        self.task: asyncio.Task | None = None
        self.error: str | None = None
        self.receiving = False
        self.revision = 0
        self.changed = asyncio.Event()

    def notify(self):
        self.revision += 1
        self.changed.set()

    def view(self):
        return dict(
            id=self.id,
            harness=self.harness,
            turns=list(self.turns.values()),
            revision=self.revision,
            busy=self.task is not None,
            error=self.error,
        )

    async def session_update(self, session_id, update, **kwargs):
        await super().session_update(session_id, update, **kwargs)
        if self.text:
            if self.receiving and session_id == self.session_id:
                turn = next(reversed(self.turns.values()))
                turn["answer"] += "".join(self.text)
                self.notify()
            self.text.clear()

    async def connect(self):
        if self.proc and self.proc.returncode is None:
            return
        await self.disconnect()
        cmd, *args = self.executor.launch_cmd
        spawn = acp.spawn_agent_process if sys.platform == "win32" else _spawn_agent
        self.conn, self.proc = await self.stack.enter_async_context(
            spawn(
                self,
                _resolve_cmd(cmd),
                *args,
                cwd=self.workspace,
                env=self.executor._spawn_env(),
                observers=[self.observe],
            )
        )
        drain = asyncio.create_task(_drain_agent_stderr(self.proc.stderr, deque(maxlen=0)))
        self.stack.callback(drain.cancel)
        init = await self.conn.initialize(protocol_version=acp.PROTOCOL_VERSION)
        options = dict(cwd=self.workspace, mcp_servers=[])
        if self.session_id is None:
            session = await self.conn.new_session(**options)
            self.session_id = session.session_id
        else:
            caps = init.agent_capabilities
            if caps.session_capabilities and caps.session_capabilities.resume:
                await self.conn.resume_session(session_id=self.session_id, **options)
            elif caps.load_session:
                await self.conn.load_session(session_id=self.session_id, **options)
                await self.await_pending_updates()
            else:
                raise ValueError("Harness cannot restore this session")

    def submit(self, request_id: str, text: str):
        existing = self.turns.get(request_id)
        if existing:
            if existing["text"] != text:
                raise ValueError("Request ID already used with different text")
            return
        if self.task or self.cancelling or self.error:
            raise ValueError(self.error or "A turn is still running")
        turn = dict(request_id=request_id, text=text, answer="", status="running")
        self.turns[request_id] = turn
        self.task = asyncio.create_task(self._reply(turn))
        self.notify()

    async def _reply(self, turn: dict):
        try:
            async with asyncio.timeout(90):
                await self.connect()
            self.receiving = True
            async with asyncio.timeout(600):
                result = await self.conn.prompt(
                    session_id=self.session_id,
                    prompt=[TextContentBlock(type="text", text=turn["text"])],
                )
                await self.await_pending_updates()
            turn["status"] = result.stop_reason
        except asyncio.CancelledError:
            turn["status"] = "cancelled"
            raise
        except Exception as exc:
            turn["status"] = "failed"
            self.error = f"{exc or type(exc).__name__}. Start a fresh chat."
            await self.disconnect()
        finally:
            self.receiving = False
            self.task = None
            self.notify()

    async def disconnect(self):
        await self.stack.aclose()
        self.conn = self.proc = None

    async def cancel(self):
        task = self.task
        if task:
            self.cancelling = True
            if self.conn and self.session_id:
                with suppress(Exception):
                    await asyncio.wait_for(self.conn.cancel(session_id=self.session_id), 1)
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
            await self.disconnect()
            next(reversed(self.turns.values()))["status"] = "cancelled"
            self.task = None
            self.cancelling = False
            self.notify()

    async def close(self):
        await self.cancel()
        await self.disconnect()
        self.directory.cleanup()


def available(atelier: Any) -> dict[str, AcpHarnessExecutor]:
    return {
        name: executor
        for name, executor in atelier.executors.items()
        if isinstance(executor, AcpHarnessExecutor) and executor.is_available()[0]
    }
