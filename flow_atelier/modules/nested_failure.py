"""Read the failing task of a nested run without exposing executor tracebacks."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from flow_atelier.schemas.flow import parse_flow_id
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
        where = f", loop iteration {self.iteration}/{self.of}" if self.of > 1 else ""
        return f"{self.calling_task} -> {self.task} [{self.tool}]{where}: {self.reason}"


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


def _recipe_tool(store, flow_id: str, task_name: str) -> str | None:
    """Name the recipe's agent for a task that failed before it logged a run."""
    try:
        conduit = store.read_conduit(parse_flow_id(flow_id)[0])
    except (OSError, ValueError):
        return None
    return next((t.tool for t in conduit.tasks if t.name == task_name), None)


def _before(finished: str | None, since: str) -> bool:
    """True only when both saved times parse and ``finished`` is earlier."""
    try:
        return datetime.fromisoformat(finished) < datetime.fromisoformat(since)
    except (TypeError, ValueError):
        return False


def latest_nested_failure(
    store, parent_flow_id: str, calling_task: str, since: str | None = None,
) -> NestedFailure | None:
    """Use the latest failed child of this call, including its own log attribution.

    ``since`` is when the calling attempt started. A child that finished before
    it belongs to an earlier attempt: a resumed call can fail before it starts
    or resumes any child, and must not be blamed on that older failure.
    """
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
        if since is not None and _before(progress.finished_at, since):
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
        tool = (
            (entry.tool if entry else progress.task_agents.get(task_name))
            or _recipe_tool(store, flow_id, task_name) or "unknown agent"
        )
        reason = brief_failure_reason((entry.stderr if entry else "") or task.reason or "")
        return NestedFailure(
            flow_id=flow_id, calling_task=calling_task, task=task_name,
            tool=tool, iteration=entry.iteration if entry else task.iteration,
            of=entry.of if entry else task.of, reason=reason,
        )
    return None
