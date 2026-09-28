"""Read the failing task of a nested run without exposing executor tracebacks."""
from __future__ import annotations

from dataclasses import dataclass

from flow_atelier.schemas.progress import TaskStatus


@dataclass(frozen=True)
class NestedFailure:
    flow_id: str
    calling_task: str
    task: str
    tool: str
    iteration: int
    of: int
    reason: str

    @property
    def selector(self) -> str:
        return f"{self.calling_task}.{self.task}"

    @property
    def summary(self) -> str:
        return (
            f"{self.calling_task} -> {self.task} [{self.tool}], "
            f"loop iteration {self.iteration}/{self.of}: {self.reason}"
        )


def brief_failure_reason(text: str) -> str:
    """Keep the reported cause while dropping Python frame lines and paths."""
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines:
        return "nested task failed; inspect its logs for the cause"
    if any("Traceback (most recent call last)" in line for line in lines):
        # Python puts the exception and its message after the final frame.
        lines = [line for line in lines if not line.startswith(("File ", "^", "~"))]
        final = lines[-1] if lines else ""
        if final and not final.startswith("Traceback"):
            return final[:300]
    return lines[0][:300]


def latest_nested_failure(store, parent_flow_id: str, calling_task: str) -> NestedFailure | None:
    """Use the latest failed child of this call, including its own log attribution."""
    try:
        children = store.list_child_flows(parent_flow_id)
    except (OSError, ValueError):
        return None
    for flow_id in reversed(children):
        try:
            progress = store.read_progress(flow_id)
        except (OSError, ValueError):
            continue
        if progress.invoking_task != calling_task:
            continue
        failed = next(
            ((name, task) for name, task in progress.tasks.items()
             if task.status == TaskStatus.failed),
            None,
        )
        if failed is None:
            continue
        task_name, task = failed
        try:
            logs = store.read_logs(flow_id)
        except (OSError, ValueError):
            logs = []
        entry = next((log for log in reversed(logs) if log.task == task_name), None)
        tool = (entry.tool if entry else progress.task_agents.get(task_name)) or "unknown agent"
        reason = brief_failure_reason((entry.stderr if entry else "") or task.reason or "")
        return NestedFailure(
            flow_id=flow_id, calling_task=calling_task, task=task_name,
            tool=tool, iteration=entry.iteration if entry else task.iteration,
            of=entry.of if entry else task.of, reason=reason,
        )
    return None
