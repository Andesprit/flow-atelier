"""tool:hitl executor — prompts the user for named inputs on stdin."""
from __future__ import annotations

import asyncio
import sys

import yaml

from flow_atelier.schemas.conduit import TaskDefinition
from flow_atelier.schemas.log import ExecutionResult
from flow_atelier.services.executor.base import ExecutorBase, FlowContext

# HITL tasks bypass the engine's concurrency semaphore (a human may be away),
# so nothing else serializes two ready HITL tasks. There is exactly one stdin;
# this process-wide lock keeps at most one prompt session owning the terminal.
_STDIN_LOCK = asyncio.Lock()


class HitlExecutor(ExecutorBase):
    """Presents the task's preamble then asks for each named input.

    The task's ``inputs`` map (``{name: description}``) drives the prompts.
    Each response is:
      1. Written to ``input.yaml`` as a top-level key (overwrite on collision)
      2. Added to ``context.inputs`` so downstream ``{{inputs.x}}`` works
    The executor's ``output`` is a YAML serialization of the collected map.
    """

    async def execute(
        self,
        task: TaskDefinition,
        resolved_command: str,
        context: FlowContext,
    ) -> ExecutionResult:
        """Prompt the user for each named input and persist answers.

        :param task: task whose ``inputs`` map drives the prompts
        :param resolved_command: preamble text shown before prompting
        :param context: runtime :class:`FlowContext`; inputs are written to
            ``input.yaml`` and appended to ``context.inputs``
        :returns: :class:`ExecutionResult` with a YAML dump of the collected map
        """
        collected: dict[str, str] = {}
        preamble_lines: list[str] = []

        if resolved_command.strip():
            preamble_lines.append(resolved_command.strip())
        preamble_lines.append(f"[hitl] Task '{task.name}' needs the following inputs:")
        preamble_text = "\n".join(preamble_lines) + "\n"

        from flow_atelier.cli.rendering.multiline_input import (
            SUBMIT_HINT,
            multiline_input,
        )

        async with _STDIN_LOCK:
            print(preamble_text, file=sys.stdout, flush=True)
            for name, description in task.inputs.items():
                prompt = f"  {name} ({description}): "
                try:
                    response = await multiline_input(prompt, hint=SUBMIT_HINT)
                except EOFError:
                    # Input stream ran dry (e.g. a scripted run that didn't
                    # supply enough answers). Mirror the harness's friendly
                    # report instead of dying with a bare EOFError traceback.
                    return ExecutionResult(
                        exit_code=1,
                        stdout=preamble_text,
                        stderr="interactive input unavailable: EOFError",
                        output="",
                    )
                # Piped stdin doesn't echo keystrokes nor add a newline; print
                # the consumed value so scripted transcripts read like a real
                # terminal session instead of two prompts smashed together.
                if not sys.stdin.isatty():
                    print(response, file=sys.stdout, flush=True)
                collected[name] = response
                context.store.append_input(context.flow_id, name, response)
                context.inputs[name] = response

        output = yaml.safe_dump(collected, sort_keys=False).strip()
        return ExecutionResult(
            exit_code=0,
            stdout=preamble_text + output,
            stderr="",
            output=output,
        )
