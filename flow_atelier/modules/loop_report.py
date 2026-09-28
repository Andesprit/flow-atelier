"""Describe saved loop exhaustion and its observed downstream effect."""
from __future__ import annotations

from collections.abc import Sequence

from pydantic import BaseModel, Field

from flow_atelier.schemas.log import LogEntry
from flow_atelier.schemas.progress import Progress, TaskStatus


class LoopPass(BaseModel):
    """One saved pass of a repeated nested task, with its child's actual agents."""

    task: str
    iteration: int
    of: int
    agents: list[str] = Field(default_factory=list)
    result: str = ""
    condition_met: bool | None = None
    child_flow_id: str | None = None


def loop_passes(store, flow_id: str, progress: Progress,
                entries: Sequence[LogEntry]) -> list[LoopPass]:
    """Join parent pass logs to child logs without reading the current recipe."""
    # Older runs predate the explicit child id on parent log entries. The
    # child's saved start time must fall inside that parent's execution span;
    # if no unique match exists, leave attribution unknown rather than guess.
    older_children = []
    if any(entry.of > 1 and entry.tool == "tool:conduit"
           and not entry.extra.get("child_flow_id") for entry in entries):
        try:
            for child_id in store.list_child_flows(flow_id):
                child_progress = store.read_progress(child_id)
                older_children.append((child_id, child_progress))
        except (OSError, ValueError):
            older_children = []
    last_by_pass = {
        (entry.task, entry.iteration): entry
        for entry in entries if entry.of > 1 and entry.tool == "tool:conduit"
    }
    passes = []
    for (task, iteration), entry in last_by_pass.items():
        saved = progress.tasks.get(task)
        outcome = saved.loop_outcome if saved else None
        met = (
            outcome.met and iteration == saved.iteration
            if outcome is not None else None
        )
        if outcome is None and saved is not None and (
            saved.status == TaskStatus.completed and saved.iteration < saved.of
        ):
            # Before met outcomes were persisted, an early completed repeat
            # could only have stopped because its predicate fired.
            met = iteration == saved.iteration
        child_id = entry.extra.get("child_flow_id")
        if not isinstance(child_id, str):
            candidates = [
                child for child, child_progress in older_children
                if child_progress.invoking_task == task
                and child_progress.started_at is not None
                and entry.started_at <= child_progress.started_at <= entry.finished_at
            ]
            child_id = candidates[0] if len(candidates) == 1 else None
        agents = []
        if isinstance(child_id, str):
            try:
                child_logs = store.read_logs(child_id)
            except (OSError, ValueError):
                child_logs = []
            actual = {log.task: log.tool for log in child_logs
                      if log.tool.startswith("harness:")}
            agents = [f"{name} [{tool}]" for name, tool in actual.items()]
        result_text = entry.output or entry.stderr
        result = next((line.strip() for line in reversed(result_text.splitlines())
                       if line.strip()), "")
        passes.append(LoopPass(
            task=task, iteration=iteration, of=entry.of, agents=agents,
            result=result, condition_met=met,
            child_flow_id=child_id if isinstance(child_id, str) else None,
        ))
    return passes


def loop_pass_lines(passes: Sequence[LoopPass], *, limit: int = 8) -> list[str]:
    """Render bounded pass lines, naming the number of hidden middle passes."""
    lines = []
    by_task: dict[str, list[LoopPass]] = {}
    for item in passes:
        by_task.setdefault(item.task, []).append(item)
    for task, task_passes in by_task.items():
        first = limit // 2
        visible = task_passes if len(task_passes) <= limit else [
            *task_passes[:first], None, *task_passes[-(limit - first):]
        ]
        for item in visible:
            if item is None:
                lines.append(
                    f"{task}: … {len(task_passes) - limit} passes hidden; "
                    "use --json for all"
                )
                continue
            agents = ", ".join(item.agents) if item.agents else "agent unrecorded"
            state = (
                "condition met" if item.condition_met is True else
                "condition not met" if item.condition_met is False else
                "condition unknown"
            )
            result = item.result or "no result output"
            lines.append(
                f"{item.task} pass {item.iteration}/{item.of}: "
                f"{agents} · {result} · {state}"
            )
    return lines


def unmet_loop_messages(progress: Progress) -> list[str]:
    """Return user-facing facts from a run's saved loop outcomes."""
    messages = []
    for name, task in progress.tasks.items():
        outcome = task.loop_outcome
        if outcome is None or outcome.met:
            continue
        if outcome.condition.startswith("while: "):
            message = (
                f"{name} stopped after {task.iteration}/{task.of} iterations; "
                f"{outcome.condition} stayed true, so its stop condition was not met"
            )
        else:
            message = (
                f"{name} stopped after {task.iteration}/{task.of} iterations "
                f"without meeting {outcome.condition}"
            )
        ran = [
            dependent for dependent in outcome.dependents
            if dependent in progress.tasks
            and progress.tasks[dependent].status == TaskStatus.completed
        ]
        if ran:
            message += f"; dependent tasks {', '.join(ran)} ran on its last output"
        messages.append(message)
    return messages
