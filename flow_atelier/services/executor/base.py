"""Abstract executor contract and FlowContext."""
from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from flow_atelier.schemas.conduit import TaskDefinition
from flow_atelier.schemas.interaction import InteractionPolicy
from flow_atelier.schemas.log import ExecutionResult, IntermediateStep
from flow_atelier.services.store.base import StoreBase


@dataclass
class FlowContext:
    """Runtime context handed to each executor invocation.

    ``run_nested_conduit`` lets ``tool:conduit`` call back into the engine.
    """

    flow_id: str
    store: StoreBase
    inputs: dict[str, Any]
    task_outputs: dict[str, str] = field(default_factory=dict)
    timeout: int = 3600
    working_dir: Path | None = None
    """Working directory for subprocess / agent execution. When ``None``,
    executors use the process cwd."""
    show_steps: bool = True
    """Stream intermediate steps (thinking, tool calls, tool results) to the
    executor's :class:`PromptSink` as they happen. Independent of
    ``task.interactive`` (which gates raw message-chunk streaming)."""
    on_step: Callable[[IntermediateStep], Awaitable[None]] | None = None
    """Persist one intermediate step the moment it arrives. The engine only
    writes a :class:`LogEntry` once a task returns, so without this a run
    that is stopped or crashes loses its whole trace, and a running flow is
    not tailable from another terminal. ``None`` disables persistence."""
    run_nested_conduit: Callable[[str, dict[str, Any], str], Awaitable[str]] | None = None
    """Callback: (conduit_name, inputs, parent_flow_id) -> child flow_id."""
    loop_history: list[str] = field(default_factory=list)
    """This task's prior-iteration outputs, oldest first. Backs
    ``{{loop.previous}}`` / ``{{loop.history}}`` when an executor resolves
    its own templates (e.g. nested-conduit / hitl inputs). Shared by
    reference with the engine, so it reflects iterations completed so far."""
    loop_history_limit: int = 0
    """Max iterations ``{{loop.history}}`` renders, newest kept; <= 0 means
    unlimited. Mirrors the engine's configured limit."""
    loop_history_entry_chars: int = 0
    """Max characters per rendered ``{{loop.history}}`` entry; <= 0 means
    unlimited. Mirrors the engine's configured limit."""
    conduit_dir: Path | None = None
    """Absolute install dir of the running conduit, backing ``{{conduit_dir}}``
    when an executor resolves its own templates (e.g. nested-conduit inputs).
    ``None`` when the conduit is not registered in the store."""

    interaction: InteractionPolicy = field(default_factory=InteractionPolicy)
    supervisor_executor: ExecutorBase | None = None
    supervisor_run: bool = False
    interactive_harnesses: bool = False
    conduit_description: str = ""


class ExecutorBase(ABC):
    """Every tool/harness implements this interface."""

    @abstractmethod
    async def execute(
        self,
        task: TaskDefinition,
        resolved_command: str,
        context: FlowContext,
    ) -> ExecutionResult:
        """Run the task and return its ExecutionResult.

        ``resolved_command`` is the ``task`` field with templates already
        resolved by the engine.

        :param task: the task definition to execute.
        :param resolved_command: the task command with templates resolved.
        :param context: runtime :class:`FlowContext` for the execution.
        :returns: :class:`ExecutionResult` describing the outcome.
        """

    def is_available(self) -> tuple[bool, str]:
        """Report whether this executor can actually run right now.

        A preflight probe used by readiness checks (``atelier check`` and the
        top of ``atelier run``) to fail fast on an unrunnable conduit instead
        of dying mid-DAG. The default is always-ready; executors backed by an
        external CLI override this to confirm the binary is on PATH.

        :returns: ``(ready, reason)`` — ``reason`` is empty when ready, else a
            short human cause (e.g. ``"`npx` not found on PATH"``).
        """
        return (True, "")
