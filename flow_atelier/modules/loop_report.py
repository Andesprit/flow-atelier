"""Describe saved loop exhaustion and its observed downstream effect."""
from __future__ import annotations

from flow_atelier.schemas.progress import Progress, TaskStatus


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
