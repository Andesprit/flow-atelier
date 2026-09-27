"""tool:conduit executor — runs another conduit as a nested flow."""
from __future__ import annotations

from typing import Any

from flow_atelier.modules.conditions import sink_task_names
from flow_atelier.modules.templating import resolve
from flow_atelier.schemas.conduit import TaskDefinition
from flow_atelier.schemas.log import ExecutionResult
from flow_atelier.services.executor.base import ExecutorBase, FlowContext


class ConduitExecutor(ExecutorBase):
    """Invokes another conduit by name via the engine callback.

    The task's ``inputs`` map (name -> template-string-or-value) is resolved
    against the parent's inputs and task outputs before dispatch.
    """

    async def execute(
        self,
        task: TaskDefinition,
        resolved_command: str,
        context: FlowContext,
    ) -> ExecutionResult:
        """Run another conduit as a nested flow.

        :param task: task whose ``inputs`` map is forwarded to the child
        :param resolved_command: name of the child conduit
        :param context: runtime :class:`FlowContext`; ``run_nested_conduit``
            is required
        :returns: :class:`ExecutionResult` whose ``output`` joins the child's
            sink-task outputs (falling back to the last successful log entry)
        """
        if context.run_nested_conduit is None:
            raise RuntimeError("ConduitExecutor requires context.run_nested_conduit")

        child_conduit_name = resolved_command.strip()
        child_inputs: dict[str, Any] = {}
        for key, raw in task.inputs.items():
            if isinstance(raw, str):
                child_inputs[key] = resolve(
                    raw, context.inputs, context.task_outputs,
                    loop_history=context.loop_history,
                    loop_history_limit=context.loop_history_limit,
                    loop_history_entry_chars=context.loop_history_entry_chars,
                    conduit_dir=context.conduit_dir,
                    task_tools=context.task_tools,
                )
            else:
                child_inputs[key] = raw

        child_flow_id = await context.run_nested_conduit(
            child_conduit_name, child_inputs, context.flow_id
        )

        child_progress = context.store.read_progress(child_flow_id)
        logs = context.store.read_logs(child_flow_id)

        # The child's output is its sink tasks' final outputs (tasks no
        # other task depends on), in definition order — the last exit-0
        # log entry is whichever task happened to log last under
        # concurrency, not a terminal result.
        last_output = ""
        child_outputs = context.store.read_outputs(child_flow_id)
        if child_outputs:
            child_conduit = context.store.read_conduit(child_conduit_name)
            parts = [
                child_outputs[name]
                for name in sink_task_names(child_conduit)
                if child_outputs.get(name)
            ]
            last_output = "\n\n".join(parts)
        if not last_output:
            # Fallback for old flows or children that failed before
            # outputs.yaml was written.
            for entry in reversed(logs):
                if entry.exit_code == 0 and entry.output:
                    last_output = entry.output
                    break

        # Sub-task outputs feed the engine's loop-predicate evaluation for
        # `tool:conduit` tasks: the predicate fires if *any* sub-task output
        # matches (an intermediate diagnostic step, not only the sink).
        # outputs.yaml holds each completed task's *final* output keyed by name
        # (last-write-wins, prior-completed tasks seeded on resume), so it
        # carries neither failed sub-tasks nor stale earlier-iteration /
        # prior-attempt entries — unlike the raw log, which accumulates both
        # across loop iterations and resumes. Non-sink tasks are still present
        # (every completed task is keyed), so any-match-on-intermediate holds.
        if child_outputs:
            sub_outputs = [v for v in child_outputs.values() if v]
        else:
            # Fallback for old flows or children that failed before
            # outputs.yaml was written: exit-0 entries only, so a failed
            # sub-task's partial/error text can't trip an `until`.
            sub_outputs = [e.output for e in logs if e.exit_code == 0 and e.output]

        status = child_progress.status.value
        exit_code = 0 if status == "completed" else 1
        return ExecutionResult(
            exit_code=exit_code,
            stdout=last_output,
            stderr="" if exit_code == 0 else f"nested conduit {status}",
            output=last_output,
            sub_outputs=sub_outputs,
        )
