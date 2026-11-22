"""Human input for runs the server owns.

A run started with ``POST /mobile/runs`` has no terminal and no open socket.
When the client asked for an interactive launch, each ``tool:hitl`` step and
each question from an interactive agent waits here until a client answers it
with ``POST /mobile/runs/{flow_id}/inputs/{request_id}``. Requests live in the
server's memory: they end with their run, and the run ends with the server.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import yaml

from flow_atelier.modules.engine import current_flow_id, current_task
from flow_atelier.schemas.api import InputAnswer, InputField, InputRequest
from flow_atelier.schemas.conduit import TaskDefinition
from flow_atelier.schemas.log import ExecutionResult
from flow_atelier.services.executor.base import ExecutorBase, FlowContext


class RunInputs:
    """The requests every server-owned run is waiting on, by request id."""

    def __init__(self) -> None:
        self._waiting: dict[str, tuple[InputRequest, asyncio.Future[Any]]] = {}

    async def ask(self, request: InputRequest) -> Any:
        """Publish ``request`` and wait for its answer.

        The request is withdrawn however the wait ends, so a late answer can
        never reach a later request.

        :param request: what the run needs.
        :returns: the answer text for a question, or the answers map for a form.
        """
        future: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        self._waiting[request.request_id] = (request, future)
        try:
            return await future
        finally:
            self._waiting.pop(request.request_id, None)

    def pending(self, flow_id: str | None = None) -> list[InputRequest]:
        """Requests still waiting, oldest first, optionally for one run."""
        requests = [r for r, _ in self._waiting.values() if flow_id in (None, r.flow_id)]
        return sorted(requests, key=lambda r: r.asked_at)

    def waiting_runs(self) -> set[str]:
        """Runs, as ``GET /flows`` lists them, with at least one request waiting."""
        return {r.flow_id for r, _ in self._waiting.values()}

    def answer(self, flow_id: str, request_id: str, payload: InputAnswer) -> None:
        """Hand an answer to the run waiting on ``request_id``.

        :raises KeyError: no such request is waiting for ``flow_id``.
        :raises ValueError: the answer does not fit the request.
        """
        entry = self._waiting.get(request_id)
        if entry is None or entry[0].flow_id != flow_id or entry[1].done():
            raise KeyError(request_id)
        request, future = entry
        if request.kind == "question":
            if payload.answer is None or not payload.answer.strip():
                raise ValueError("send a nonempty 'answer' for a question")
            future.set_result(payload.answer)
            return
        answers = payload.answers or {}
        if missing := [f.name for f in request.fields if f.name not in answers]:
            raise ValueError(f"missing answers: {', '.join(missing)}")
        future.set_result({f.name: answers[f.name] for f in request.fields})


@dataclass
class RunScope:
    """Ties one launch to the run id ``GET /flows`` will list it under."""

    inputs: RunInputs
    root: str | None = field(default=None)

    def request(self, kind: str, prompt: str, fields: list[InputField]) -> InputRequest:
        """Build a request for the task asking right now (child-aware)."""
        asking = current_flow_id(self.root or "")
        root = self.root or asking
        return InputRequest(
            request_id=uuid4().hex,
            flow_id=root,
            sub_flow_id=asking if asking and asking != root else None,
            task=current_task(""),
            kind=kind,  # type: ignore[arg-type]
            prompt=prompt,
            fields=fields,
            asked_at=datetime.now(UTC).isoformat(),
        )


class ServerPromptSink:
    """:class:`PromptSink` that turns an agent's question into an input request."""

    def __init__(self, scope: RunScope) -> None:
        self._scope = scope

    async def display(self, text: str) -> None:
        """Agent prose is saved in the run's log; nothing streams live."""

    async def start_agent_turn(self, label: str = "agent") -> None:
        """No turn markers for a run with no screen."""

    async def request_input(self, prompt: str) -> str:
        """Wait until a client answers the agent's question."""
        request = self._scope.request("question", prompt, [])
        return str(await self._scope.inputs.ask(request))


class ServerHitlExecutor(ExecutorBase):
    """``tool:hitl`` executor that waits for a client's answers.

    Collected values are saved exactly like the terminal and WebSocket
    executors save them, so later steps read them the same way.
    """

    def __init__(self, scope: RunScope) -> None:
        self._scope = scope

    async def execute(
        self, task: TaskDefinition, resolved_command: str, context: FlowContext,
    ) -> ExecutionResult:
        """Publish the step's form, wait for the answers, and save them."""
        fields = [InputField(name=n, description=d) for n, d in task.inputs.items()]
        request = self._scope.request("form", resolved_command.strip(), fields)
        answers: dict[str, Any] = await self._scope.inputs.ask(request)
        for name, value in answers.items():
            context.store.append_input(context.flow_id, name, value)
            context.inputs[name] = value
        output = yaml.safe_dump(answers, sort_keys=False).strip()
        preamble = (resolved_command.strip() + "\n") if resolved_command else ""
        return ExecutionResult(exit_code=0, stdout=preamble + output, stderr="", output=output)
