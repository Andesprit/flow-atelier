"""A dashboard tab may detach. The server owns its ACP connection and current turn."""

from __future__ import annotations

import asyncio
import json
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

from flow_atelier.services.api import chat_profile
from flow_atelier.services.api.chat_actions import INSTRUCTIONS, ChatActions
from flow_atelier.services.executor.harness import (
    AcpHarnessExecutor,
    _drain_agent_stderr,
    _ProbeClient,
    _resolve_cmd,
    _spawn_agent,
)


class ChatSession(_ProbeClient):
    def __init__(self, harness: str, executor: AcpHarnessExecutor, project: Path, global_dir: Path):
        super().__init__()
        self.id = uuid4().hex
        self.harness = harness
        self.actions = ChatActions(project, global_dir)
        self.jobs: set[asyncio.Task] = set()
        self.executor = executor
        self.session_id: str | None = None
        self.conn = None
        self.proc = None
        self.stack = AsyncExitStack()
        self.directory = TemporaryDirectory(prefix="atelier-chat-")
        self.workspace = str(Path(self.directory.name) / "workspace")
        Path(self.workspace).mkdir()
        self.last_seen = time.monotonic()
        self.cancel_lock = asyncio.Lock()
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
            pending=(
                {
                    "id": self.actions.pending["id"],
                    "details": json.dumps(
                        {
                            k: v
                            for k, v in self.actions.pending.items()
                            if k not in {"id", "recipe_digest"} and v is not None
                        },
                        indent=2,
                        ensure_ascii=False,
                    ),
                }
                if self.actions.pending
                else None
            ),
            executing=bool(self.jobs),
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
        argv, env, meta = self.stack.enter_context(
            chat_profile.launch(self.executor, self.directory.name)
        )
        cmd, *args = argv
        spawn = acp.spawn_agent_process if sys.platform == "win32" else _spawn_agent
        self.conn, self.proc = await self.stack.enter_async_context(
            spawn(
                self,
                _resolve_cmd(cmd),
                *args,
                cwd=self.workspace,
                env=env,
                observers=[self.observe],
            )
        )
        drain = asyncio.create_task(_drain_agent_stderr(self.proc.stderr, deque(maxlen=0)))
        self.stack.callback(drain.cancel)
        init = await self.conn.initialize(protocol_version=acp.PROTOCOL_VERSION)
        options = dict(cwd=self.workspace, mcp_servers=[], **meta)
        if self.session_id is None:
            session = await self.conn.new_session(**options)
            self.session_id = session.session_id
        else:
            caps = init.agent_capabilities
            if caps.session_capabilities and caps.session_capabilities.resume:
                session = await self.conn.resume_session(session_id=self.session_id, **options)
            elif caps.load_session:
                session = await self.conn.load_session(session_id=self.session_id, **options)
                await self.await_pending_updates()
            else:
                raise ValueError("Harness cannot restore this session")
        if env.get("INITIAL_AGENT_MODE") and (
            not session.modes or session.modes.current_mode_id != "read-only"
        ):
            raise ValueError("The adapter did not enter its read-only session mode")

    def submit(self, request_id: str, text: str, source: str | None = None):
        existing = self.turns.get(request_id)
        if existing:
            if existing["text"] != text or existing["source"] != source:
                raise ValueError("Request ID already used with different text")
            return
        if self.task or self.cancel_lock.locked() or self.error:
            raise ValueError(self.error or "A turn is still running")
        self.actions.clear()
        turn = dict(request_id=request_id, text=text, answer="", status="running", source=source)
        self.turns[request_id] = turn
        self.task = asyncio.create_task(self._reply(turn))
        self.notify()

    async def _reply(self, turn: dict):
        try:
            async with asyncio.timeout(90):
                await self.connect()
            prompt = [TextContentBlock(type="text", text=INSTRUCTIONS)]
            previous = list(self.turns.values())[:-1]
            if previous and previous[-1].get("result"):
                prompt.append(
                    TextContentBlock(
                        type="text",
                        text=json.dumps({"cli_result": previous[-1]["result"]}, ensure_ascii=False),
                    )
                )
            if turn["source"]:
                try:
                    data = json.loads(
                        await self.actions.call(
                            "history", turn["source"], "--chat-import", "--json"
                        )
                    )
                    if data.get("error"):
                        raise ValueError(data["error"])
                except (OSError, TimeoutError, ValueError) as exc:
                    turn.update(status="failed", result=str(exc))
                    return
                if warning := data.get("warning"):
                    turn["result"] = warning
                prompt.append(
                    TextContentBlock(
                        type="text",
                        text=json.dumps({"untrusted_session": data}, ensure_ascii=False),
                    )
                )
            prompt.append(TextContentBlock(type="text", text=json.dumps({"user": turn["text"]})))
            self.receiving = True
            async with asyncio.timeout(600):
                result = await self.conn.prompt(
                    session_id=self.session_id,
                    prompt=prompt,
                )
                await self.await_pending_updates()
            turn["status"] = result.stop_reason
            if result.stop_reason == "end_turn":
                try:
                    action_result = await self.actions.reply(turn["answer"])
                except (OSError, TimeoutError, ValueError) as exc:
                    action_result = str(exc)
                if action_result:
                    turn["result"] = "\n".join(filter(None, [turn.get("result"), action_result]))
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

    def confirm(self, token: str):
        if self.task or not self.actions.pending or self.actions.pending["id"] != token:
            raise ValueError("Confirmation expired. Request a new preview.")
        # Reserve synchronously before yielding to a double-click or another turn.
        action = self.actions
        self.actions = ChatActions(action.project, action.global_dir)
        turn = next(reversed(self.turns.values()))

        async def perform():
            try:
                turn["result"] = await action.confirm(token)
            except (OSError, ValueError, TimeoutError) as exc:
                turn["result"] = str(exc)
            finally:
                self.jobs.discard(asyncio.current_task())
                self.notify()

        self.jobs.add(asyncio.create_task(perform()))
        turn["result"] = "Running confirmed operation."
        self.notify()

    async def disconnect(self):
        await self.stack.aclose()
        self.conn = self.proc = None

    async def cancel(self):
        async with self.cancel_lock:
            task = self.task
            if task is None:
                return
            if self.conn and self.session_id:
                with suppress(Exception):
                    await asyncio.wait_for(self.conn.cancel(session_id=self.session_id), 1)
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
            await self.disconnect()
            next(reversed(self.turns.values()))["status"] = "cancelled"
            self.task = None
            self.notify()

    async def close(self):
        await self.cancel()
        await self.disconnect()
        for job in self.jobs.copy():
            job.cancel()
        await asyncio.gather(*self.jobs, return_exceptions=True)
        self.directory.cleanup()


def available(atelier: Any) -> dict[str, AcpHarnessExecutor]:
    return {
        name: executor
        for name, executor in atelier.executors.items()
        if isinstance(executor, AcpHarnessExecutor)
        and executor.is_available()[0]
        and chat_profile.supported(atelier, executor)
    }
