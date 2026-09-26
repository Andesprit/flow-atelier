"""Follow one flow on disk and say what changed, for the run page's socket."""
from __future__ import annotations

from typing import Any

from flow_atelier.core.atelier import Atelier
from flow_atelier.modules.flow_view import build_task_log, task_log_update
from flow_atelier.schemas.api import FlowView, TaskLogView
from flow_atelier.schemas.log import LogEntry, StepRecord


class FlowWatcher:
    """Follow one flow's saved files and report what changed since the last look.

    A flow is written by whichever process runs it (the CLI, the dashboard,
    the scheduler), so its files are the one thing every run shares. Each look
    stats them first and reads nothing when they are unchanged. Otherwise it
    reads only what ``steps.jsonl`` gained and keeps the watched task's steps
    in memory, so a long run is never re-read whole on every look.
    """

    def __init__(self, atelier: Atelier, flow_id: str) -> None:
        """Load the flow's map.

        :param atelier: facade over the flow's store.
        :param flow_id: flow identifier.
        :raises FileNotFoundError: if no flow with that id exists
        """
        self._atelier = atelier
        self._flow_id = flow_id
        self._signature = atelier.flow_files_signature(flow_id)
        self._look_again = False
        self.view: FlowView = atelier.get_flow_view(flow_id)
        self.log: TaskLogView | None = None
        self._task: str | None = None
        self._offset = 0
        self._steps: list[StepRecord] = []
        self._entries: list[LogEntry] = []
        self._logs_read_at: tuple[int, int] | None = None

    def watch(self, task: str) -> TaskLogView:
        """Follow ``task`` from now on and return its whole log.

        :param task: task name.
        :returns: the task's log as it is now.
        :raises KeyError: if the flow has no task with that name
        """
        if not any(t.name == task for t in self.view.tasks):
            raise KeyError(task)
        self._task = task
        self._offset = 0
        self._steps = []
        self._entries = []
        self._logs_read_at = None
        self.log = self._build()
        return self.log

    def poll(self) -> list[dict[str, Any]]:
        """Return the envelopes that bring a client up to date.

        :returns: a ``flow`` envelope when the map changed, then a
            ``task_update`` (or a whole ``task_log`` when an update cannot
            describe the change) for the watched task; empty when nothing did.
        :raises FileNotFoundError: if the flow was deleted
        """
        signature = self._atelier.flow_files_signature(self._flow_id)
        if signature == self._signature and not self._look_again:
            return []
        progress_changed = signature[0] != self._signature[0]
        # A sub-run's folder appears just before the engine records which
        # task started it, so look once more on the next tick.
        self._look_again = signature[3] != self._signature[3]
        self._signature = signature

        envelopes: list[dict[str, Any]] = []
        if progress_changed:
            view = self._atelier.get_flow_view(self._flow_id)
            if view != self.view:
                self.view = view
                envelopes.append({"type": "flow", "flow": view.model_dump(mode="json")})
        if self.log is not None:
            log = self._build()
            if log != self.log:
                update = task_log_update(self.log, log)
                self.log = log
                envelopes.append(
                    {"type": "task_log", "log": log.model_dump(mode="json")}
                    if update is None
                    else {"type": "task_update", "update": update.model_dump(mode="json")}
                )
        return envelopes

    def _build(self) -> TaskLogView:
        """Read what the watched task gained and rebuild its log.

        :returns: the watched task's log.
        """
        store = self._atelier.store
        records, self._offset = store.read_steps(self._flow_id, self._offset)
        self._steps.extend(r for r in records if r.task == self._task)
        logs_at = self._signature[1]
        if logs_at is not None and logs_at != self._logs_read_at:
            self._entries = [e for e in store.read_logs(self._flow_id) if e.task == self._task]
            self._logs_read_at = logs_at
        known = next(t for t in self.view.tasks if t.name == self._task)
        tool = known.tool or (self._entries[0].tool if self._entries else "")
        progress = store.read_progress(self._flow_id).tasks.get(self._task)
        sub_runs = (
            self._atelier.task_sub_runs(self._flow_id, self._task)
            if tool == "tool:conduit"
            else None
        )
        return build_task_log(self._task, tool, progress, self._entries, self._steps, sub_runs)
