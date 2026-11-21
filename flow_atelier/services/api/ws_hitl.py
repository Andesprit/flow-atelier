"""WebSocket-driven HITL executor.

Replaces :class:`HitlExecutor` for connections served over the WebSocket
endpoint: instead of reading stdin, it pushes a ``hitl_request`` envelope
through the broker, awaits the matching ``hitl_answer``, and persists
the collected map exactly like the stdin executor.
"""
from __future__ import annotations

from typing import Any

import yaml

from flow_atelier.schemas.conduit import TaskDefinition
from flow_atelier.schemas.log import ExecutionResult
from flow_atelier.services.api.ws_manager import WebSocketBroker
from flow_atelier.services.executor.base import ExecutorBase, FlowContext


class WsHitlExecutor(ExecutorBase):
    """``tool:hitl`` executor wired to a :class:`WebSocketBroker`.

    :param broker: per-connection broker that owns the answer queue
    :param flow_id: flow this executor instance is bound to
    :param request_timeout: optional timeout (seconds) for awaiting answers
    """

    def __init__(
        self,
        broker: WebSocketBroker,
        flow_id: str,
        request_timeout: float | None = None,
    ) -> None:
        """Bind the executor to a broker and a specific flow.

        :param broker: per-connection broker that owns the answer queue
        :param flow_id: flow this executor instance is bound to
        :param request_timeout: optional timeout (seconds) for awaiting answers
        """
        self.broker = broker
        self.flow_id = flow_id
        self.request_timeout = request_timeout

    async def execute(
        self,
        task: TaskDefinition,
        resolved_command: str,
        context: FlowContext,
    ) -> ExecutionResult:
        """Send an HITL request, await the answer, and persist the collected inputs.

        :param task: task definition being executed
        :param resolved_command: command string already rendered with templating
        :param context: live flow context (store, inputs, flow id)
        """
        inputs_descriptor = [
            {"name": name, "description": description}
            for name, description in task.inputs.items()
        ]
        if inputs_descriptor and resolved_command.strip():
            first = inputs_descriptor[0]
            first["description"] = f"{resolved_command.strip()}\n\n{first['description']}"
        await self.broker.send(
            {
                "type": "hitl_request",
                "flow_id": self.flow_id,
                "task": task.name,
                "inputs": inputs_descriptor,
            }
        )
        answers: dict[str, Any] = await self.broker.await_hitl_answer(
            self.flow_id, timeout=self.request_timeout
        )

        missing = [name for name in task.inputs if name not in answers]
        if missing:
            return ExecutionResult(
                exit_code=1,
                stdout=resolved_command.strip(),
                stderr="missing HITL input(s): " + ", ".join(missing),
                output="",
            )

        collected: dict[str, Any] = {}
        for name in task.inputs:
            value = answers[name]
            collected[name] = value
            context.store.append_input(context.flow_id, name, value)
            context.inputs[name] = value

        output = yaml.safe_dump(collected, sort_keys=False).strip()
        preamble = (resolved_command.strip() + "\n") if resolved_command else ""
        return ExecutionResult(
            exit_code=0,
            stdout=preamble + output,
            stderr="",
            output=output,
        )
