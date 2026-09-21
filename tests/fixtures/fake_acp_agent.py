"""Fake ACP agent used by harness executor tests.

Reads a scripted behavior from ``--script <json>`` on argv.

Script schema::

    {
        "modes": null | {
            "current": "default",
            "available": [
                {"id": "default", "name": "Default"},
                {"id": "bypassPermissions", "name": "Bypass"}
            ]
        },
        "efforts": null | {
            "id": "reasoning_effort",
            "category": "thought_level",   // null drops the category entirely
            "current": "medium",
            "available": ["low", "medium", "high"],
            "per_model": {"m2": {"current": "high", "available": ["high", "max"]}}
        },
        "turns": [
            {
                "chunks": ["text ", "more text"],
                "delay_before": 0.0,
                "stop": "end_turn",
                "ask_permission": null | {
                    "summary": "...",
                    "options": [{"id": "allow", "label": "Allow"}]
                }
            },
            ...
        ]
    }

Each call to ``prompt`` pops the next turn. If turns run out, the agent
returns ``stop_reason="end_turn"`` with no chunks.

When ``modes`` is set, ``new_session`` advertises them to the client and
``set_session_mode`` emits a ``[mode_set:<mode_id>]`` chunk so tests can
assert which mode the harness selected by inspecting the run output.

When ``efforts`` is set, the session carries a ``thought_level`` select
alongside the model one. ``per_model`` makes it model-dependent the way a
real agent's is: selecting a model listed there replaces the effort option's
values and current before ``set_config_option`` answers, so a client that
validates against the session's opening options instead of the returned ones
gets it wrong.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any

import acp
from acp.exceptions import RequestError
from acp.schema import (
    AgentCapabilities,
    AgentMessageChunk,
    AllowedOutcome,
    AuthMethodAgent,
    Cost,
    Implementation,
    InitializeResponse,
    ModelInfo,
    NewSessionResponse,
    PromptCapabilities,
    PromptResponse,
    SessionConfigOptionSelect,
    SessionConfigSelectOption,
    SessionMode,
    SessionModelState,
    SessionModeState,
    SetSessionConfigOptionResponse,
    SetSessionModelResponse,
    SetSessionModeResponse,
    TextContentBlock,
    ToolCallUpdate,
    Usage,
    UsageUpdate,
)
from acp.schema import (
    PermissionOption as AcpPermissionOption,
)


class FakeAgent:
    SESSION_ID = "fake-session-1"

    def __init__(
        self,
        turns: list[dict[str, Any]],
        modes: dict[str, Any] | None = None,
        models: dict[str, Any] | None = None,
        efforts: dict[str, Any] | None = None,
        auth_methods: list[dict[str, Any]] | None = None,
        fail_session: str | None = None,
        record_path: str | None = None,
    ) -> None:
        """Initialize the fake agent with a scripted turn list.

        :param turns: ordered list of turn specs to consume on each prompt.
        :param modes: optional session-modes spec exposed via new_session.
        :param models: optional model spec ``{"current", "available", "via"}``
            exposed via new_session; ``via`` is ``config`` (a ``model`` config
            option, the default) or ``legacy`` (the ``models`` state).
        :param efforts: optional ``thought_level`` spec ``{"id", "current",
            "available", "per_model"}``; ``per_model`` swaps the offer when
            that model is selected.
        :param auth_methods: optional auth methods advertised at initialize,
            so connection-check reporting can be exercised.
        :param fail_session: when set, new_session raises with this message —
            what a logged-out agent does.
        :param record_path: prompt log file, or an existing directory for per-process logs.
        """
        self._turns = list(turns)
        self._modes_spec = modes
        self._models_spec = models
        self._efforts_spec = efforts
        self._config_options: list[SessionConfigOptionSelect] = []
        self._auth_methods = auth_methods or []
        self._fail_session = fail_session
        self._record_path = record_path
        self._conn: acp.Client | None = None

    def on_connect(self, conn: acp.Client) -> None:
        """Store the ACP client connection used for session updates.

        :param conn: the ACP client connection bound by the runtime.
        """
        self._conn = conn

    async def initialize(
        self, protocol_version: int, client_capabilities=None, client_info=None, **kwargs
    ) -> InitializeResponse:
        """Return a canned InitializeResponse for tests.

        :param protocol_version: ACP protocol version requested by the client.
        :param client_capabilities: client-advertised capabilities (ignored).
        :param client_info: client identity info (ignored).
        :param kwargs: additional keyword arguments accepted by the protocol.
        """
        return InitializeResponse(
            protocol_version=acp.PROTOCOL_VERSION,
            agent_capabilities=AgentCapabilities(
                load_session=False,
                prompt_capabilities=PromptCapabilities(
                    audio=False, embedded_context=False, image=False
                ),
            ),
            agent_info=Implementation(name="fake-acp-agent", version="0.0.1"),
            auth_methods=[
                AuthMethodAgent(id=m["id"], name=m.get("name", m["id"]))
                for m in self._auth_methods
            ],
        )

    async def new_session(self, cwd: str, mcp_servers=None, **kwargs) -> NewSessionResponse:
        """Return a canned NewSessionResponse, optionally with session modes.

        :param cwd: working directory provided by the client (ignored).
        :param mcp_servers: MCP server specs (ignored).
        :param kwargs: additional keyword arguments accepted by the protocol.
        """
        if not Path(cwd).is_absolute():
            # Mirrors the real agents: claude-agent-acp rejects a relative cwd
            # with "`cwd` must be an absolute path".
            raise RequestError.invalid_params({"details": "`cwd` must be absolute"})
        if self._fail_session:
            raise RequestError.auth_required({"details": self._fail_session})
        modes = None
        if self._modes_spec:
            available = [
                SessionMode(id=m["id"], name=m["name"], description=m.get("description"))
                for m in self._modes_spec["available"]
            ]
            modes = SessionModeState(
                available_modes=available,
                current_mode_id=self._modes_spec["current"],
            )
        models = None
        if self._models_spec:
            spec = self._models_spec
            if spec.get("via", "config") == "legacy":
                models = SessionModelState(
                    available_models=[
                        ModelInfo(model_id=m["id"], name=m["name"]) for m in spec["available"]
                    ],
                    current_model_id=spec["current"],
                )
            else:
                self._config_options.append(
                    SessionConfigOptionSelect(
                        id="model",
                        name="Model",
                        category="model",
                        type="select",
                        current_value=spec["current"],
                        options=[
                            SessionConfigSelectOption(value=m["id"], name=m["name"])
                            for m in spec["available"]
                        ],
                    )
                )
        if self._efforts_spec:
            spec = self._efforts_spec
            self._config_options.append(
                self._effort_option(spec["current"], spec["available"])
            )
        return NewSessionResponse(
            session_id=self.SESSION_ID,
            modes=modes,
            models=models,
            config_options=list(self._config_options) or None,
        )

    def _effort_option(self, current: str, available: list[str]) -> SessionConfigOptionSelect:
        """Build the ``thought_level`` select for the efforts currently on offer.

        :param current: the effort in force.
        :param available: the effort ids offered.
        :returns: the config option the client reads the effort choice from.
        """
        return SessionConfigOptionSelect(
            id=self._efforts_spec.get("id", "reasoning_effort"),
            name="Reasoning effort",
            # ACP makes the category optional; ``"category": null`` in the
            # script drops it, leaving only the id to recognize it by.
            category=self._efforts_spec.get("category", "thought_level"),
            type="select",
            current_value=current,
            options=[SessionConfigSelectOption(value=e, name=e.title()) for e in available],
        )

    async def prompt(self, prompt, session_id: str, **kwargs) -> PromptResponse:
        """Pop the next scripted turn and emit its chunks via session_update.

        :param prompt: prompt payload from the client (ignored beyond presence).
        :param session_id: session identifier the client is interacting with.
        :param kwargs: additional keyword arguments accepted by the protocol.
        """
        if self._record_path:
            record = Path(self._record_path)
            if record.is_dir():
                record /= f"{os.getpid()}.jsonl"
            with record.open("a", encoding="utf-8") as file:
                file.write(json.dumps([p.model_dump(mode="json") for p in prompt]) + "\n")
        if not self._turns:
            return PromptResponse(stop_reason="end_turn")
        turn = self._turns.pop(0)

        delay = float(turn.get("delay_before", 0) or 0)
        if delay > 0:
            await asyncio.sleep(delay)

        assert self._conn is not None

        ask = turn.get("ask_permission")
        if ask:
            options = [
                AcpPermissionOption(
                    option_id=o["id"],
                    name=o["label"],
                    kind=o.get("kind", "allow_once"),
                )
                for o in ask["options"]
            ]
            tool_call = ToolCallUpdate(
                tool_call_id="tc-1",
                title=ask.get("summary", "permission"),
                kind="execute",
                status="pending",
                raw_input=ask.get("raw_input"),
            )
            resp = await self._conn.request_permission(
                options=options,
                session_id=session_id,
                tool_call=tool_call,
            )
            chosen_id = ""
            if isinstance(resp.outcome, AllowedOutcome):
                chosen_id = resp.outcome.option_id or ""
            await self._conn.session_update(
                session_id=session_id,
                update=AgentMessageChunk(
                    session_update="agent_message_chunk",
                    content=TextContentBlock(type="text", text=f"[perm:{chosen_id}]"),
                ),
            )

        for chunk_text in turn.get("chunks", []):
            await self._conn.session_update(
                session_id=session_id,
                update=AgentMessageChunk(
                    session_update="agent_message_chunk",
                    content=TextContentBlock(type="text", text=chunk_text),
                ),
            )

        cost = turn.get("cost")
        if cost is not None:
            await self._conn.session_update(
                session_id=session_id,
                update=UsageUpdate(
                    session_update="usage_update",
                    cost=Cost(amount=float(cost), currency="USD"),
                    size=0,
                    used=0,
                ),
            )

        usage_spec = turn.get("usage")
        usage = Usage(**usage_spec) if usage_spec is not None else None
        return PromptResponse(stop_reason=turn.get("stop", "end_turn"), usage=usage)

    # ---- unused Agent methods: stub to satisfy protocol ----
    async def authenticate(self, method_id: str, **kwargs):
        """Stub authenticate to satisfy the Agent protocol.

        :param method_id: authentication method identifier (ignored).
        :param kwargs: additional keyword arguments accepted by the protocol.
        """
        return None

    async def load_session(self, *args, **kwargs):
        """Stub load_session to satisfy the Agent protocol.

        :param args: positional arguments accepted by the protocol.
        :param kwargs: keyword arguments accepted by the protocol.
        """
        return None

    async def list_sessions(self, *args, **kwargs):
        """Stub list_sessions to satisfy the Agent protocol.

        :param args: positional arguments accepted by the protocol.
        :param kwargs: keyword arguments accepted by the protocol.
        """
        raise NotImplementedError

    async def set_session_mode(
        self, mode_id: str, session_id: str, **kwargs
    ) -> SetSessionModeResponse:
        """Emit a mode-set chunk so tests can assert the selected mode.

        :param mode_id: requested session mode identifier.
        :param session_id: session identifier the client is interacting with.
        :param kwargs: additional keyword arguments accepted by the protocol.
        """
        del kwargs
        if self._conn is not None:
            await self._conn.session_update(
                session_id=session_id,
                update=AgentMessageChunk(
                    session_update="agent_message_chunk",
                    content=TextContentBlock(
                        type="text", text=f"[mode_set:{mode_id}]"
                    ),
                ),
            )
        return SetSessionModeResponse()

    async def _echo(self, session_id: str, text: str) -> None:
        """Emit ``text`` as an agent chunk so tests can assert on it.

        :param session_id: session identifier the client is interacting with.
        :param text: the marker text to emit.
        """
        if self._conn is not None:
            await self._conn.session_update(
                session_id=session_id,
                update=AgentMessageChunk(
                    session_update="agent_message_chunk",
                    content=TextContentBlock(type="text", text=text),
                ),
            )

    async def set_session_model(self, model_id: str, session_id: str, **kwargs):
        """Echo the legacy model switch as ``[model_set:<id>]``.

        :param model_id: requested model identifier.
        :param session_id: session identifier the client is interacting with.
        :param kwargs: additional keyword arguments accepted by the protocol.
        """
        del kwargs
        await self._echo(session_id, f"[model_set:{model_id}]")
        return SetSessionModelResponse()

    async def set_config_option(self, config_id: str, session_id: str, value, **kwargs):
        """Echo a config change as ``[config_set:<id>=<value>]``.

        Answers with the *full* config, as ACP requires — including the
        effort option swapped for the newly chosen model's, when the script
        gave that model its own offer.

        :param config_id: the option being set.
        :param session_id: session identifier the client is interacting with.
        :param value: the requested value.
        :param kwargs: additional keyword arguments accepted by the protocol.
        """
        del kwargs
        await self._echo(session_id, f"[config_set:{config_id}={value}]")
        changed = next(
            (o for o in self._config_options if o.id == config_id), None
        )
        if changed is not None:
            changed.current_value = value
        per_model = (self._efforts_spec or {}).get("per_model", {}).get(value)
        if changed is not None and changed.category == "model" and per_model is not None:
            effort_id = self._efforts_spec.get("id", "reasoning_effort")
            self._config_options = [
                self._effort_option(per_model["current"], per_model["available"])
                if o.id == effort_id
                else o
                for o in self._config_options
            ]
        return SetSessionConfigOptionResponse(config_options=list(self._config_options))

    async def fork_session(self, *args, **kwargs):
        """Stub fork_session to satisfy the Agent protocol.

        :param args: positional arguments accepted by the protocol.
        :param kwargs: keyword arguments accepted by the protocol.
        """
        raise NotImplementedError

    async def resume_session(self, *args, **kwargs):
        """Stub resume_session to satisfy the Agent protocol.

        :param args: positional arguments accepted by the protocol.
        :param kwargs: keyword arguments accepted by the protocol.
        """
        raise NotImplementedError

    async def close_session(self, *args, **kwargs):
        """Stub close_session to satisfy the Agent protocol.

        :param args: positional arguments accepted by the protocol.
        :param kwargs: keyword arguments accepted by the protocol.
        """
        return None

    async def ext_method(self, method: str, params):
        """Stub ext_method to satisfy the Agent protocol.

        :param method: extension method name (ignored).
        :param params: extension method parameters (ignored).
        """
        return {}

    async def ext_notification(self, method: str, params) -> None:
        """Stub ext_notification to satisfy the Agent protocol.

        :param method: extension notification name (ignored).
        :param params: extension notification parameters (ignored).
        """
        return None


async def _main() -> None:
    """Entry point that wires the FakeAgent script onto the ACP runtime."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--script", required=True)
    args = parser.parse_args()
    script = json.loads(args.script)
    agent = FakeAgent(
        turns=script.get("turns", []),
        modes=script.get("modes"),
        models=script.get("models"),
        efforts=script.get("efforts"),
        auth_methods=script.get("auth_methods"),
        fail_session=script.get("fail_session"),
        record_path=script.get("record_path"),
    )
    # `session/set_model` is unstable in ACP; the real adapters serve it, so
    # the fake must opt in to be routed the request at all.
    await acp.run_agent(agent, use_unstable_protocol=True)


if __name__ == "__main__":
    try:
        asyncio.run(_main())
    except (KeyboardInterrupt, EOFError):
        sys.exit(0)
