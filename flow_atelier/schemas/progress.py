"""Flow progress schemas."""
from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, Field


class TaskStatus(str, Enum):
    pending = "pending"
    running = "running"
    completed = "completed"
    failed = "failed"
    skipped = "skipped"
    cancelled = "cancelled"


class FlowStatus(str, Enum):
    running = "running"
    completed = "completed"
    failed = "failed"
    stopped = "stopped"


class TaskProgress(BaseModel):
    status: TaskStatus = TaskStatus.pending
    iteration: int = 1
    of: int = 1
    reason: str | None = None


class Workspaces(BaseModel):
    """The separate Git checkouts a run gave some of its tasks.

    Written before any task starts, so a crash still leaves a record of where
    the work went. ``paths`` is absolute: an agent's edits have to be findable
    from anywhere, not only from the directory the run was started in.
    """

    source: str
    """Absolute path of the checkout the worktrees were cut from."""
    base: str
    """The exact commit every worktree of this run was created at."""
    paths: dict[str, str] = Field(default_factory=dict)
    """Task name to the absolute path of its own checkout."""


class Progress(BaseModel):
    status: FlowStatus = FlowStatus.running
    current_tasks: list[str] = Field(default_factory=list)
    tasks: dict[str, TaskProgress] = Field(default_factory=dict)
    started_at: str | None = None
    finished_at: str | None = None
    runner_pid: int | None = None
    runner_host: str | None = None
    run_path: str | None = None
    invoking_task: str | None = None
    stoppable: bool = False
    # Per-task agent selections in force for this run: the effective
    # ``harness:<name>[:<model>[:<effort>]]`` for every task whose agent was
    # chosen at launch rather than read from the recipe, plus any task a resume
    # kept the completed result of, recorded as the agent that actually
    # produced it. Empty for a run that took the recipe as written, and absent
    # on flows recorded before agent selection existed — both mean "the recipe
    # decides".
    task_agents: dict[str, str] = Field(default_factory=dict)
    # The separate checkouts this run created for the tasks `--worktree` named,
    # recorded before any of them started. ``None`` on a run that took the
    # ordinary shared working directory — and on every flow recorded before
    # per-task checkouts existed, which keeps reading exactly as it did.
    workspaces: Workspaces | None = None
