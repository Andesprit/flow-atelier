"""Read models for the run page: the task map and one task's log.

Both are built only from what a run already writes to disk (``progress.json``,
``logs.jsonl`` and ``steps.jsonl``), so the page works the same for runs
started from the CLI, the dashboard or the scheduler.
"""
from __future__ import annotations

from bisect import bisect_right
from collections import Counter
from datetime import UTC, datetime

from flow_atelier.modules.conditions import DependencyParseError, parse_dependency
from flow_atelier.schemas.api import (
    FlowTaskView,
    FlowView,
    TaskLogLine,
    TaskLogRound,
    TaskLogUpdate,
    TaskLogView,
    TaskRoundPatch,
)
from flow_atelier.schemas.conduit import Conduit
from flow_atelier.schemas.log import IntermediateStep, LogEntry, StepKind, StepRecord
from flow_atelier.schemas.progress import Progress, TaskProgress, TaskStatus

# One-line summaries (commands, prompts) are cut here; what an agent said and
# what a command printed are kept whole.
_SUMMARY_CHARS = 300
_THOUGHT_CHARS = 500
_FAILURE_CHARS = 160

# ACP tool kinds, renamed to the verbs a reader scans for.
_TOOL_VERBS = {
    "read": "read",
    "edit": "edit",
    "delete": "delete",
    "move": "move",
    "search": "search",
    "execute": "run",
    "think": "think",
    "fetch": "fetch",
}


def build_flow_view(
    flow_id: str,
    conduit_name: str,
    progress: Progress,
    conduit: Conduit | None,
    parent_flow_id: str | None = None,
) -> FlowView:
    """Return every task of a run with its dependencies and progress.

    Tasks come from the conduit's current definition, in definition order. A
    task the run recorded but the definition no longer has is appended, so an
    edited or deleted conduit still shows everything that ran.

    :param flow_id: flow identifier.
    :param conduit_name: name of the conduit the flow ran.
    :param progress: the flow's saved progress.
    :param conduit: the conduit's current definition, or ``None`` if unreadable.
    :param parent_flow_id: for a sub-run, the run that started it.
    :returns: the map's read model.
    """
    tasks: list[FlowTaskView] = []
    for t in conduit.tasks if conduit is not None else []:
        p = progress.tasks.get(t.name)
        tasks.append(
            FlowTaskView(
                name=t.name,
                tool=t.tool,
                description=t.description,
                depends_on=_dependency_names(t.depends_on),
                status=p.status.value if p else TaskStatus.pending.value,
                iteration=p.iteration if p else 1,
                of=p.of if p else t.repeat,
            )
        )
    defined = {t.name for t in tasks}
    for name, p in progress.tasks.items():
        if name not in defined:
            tasks.append(
                FlowTaskView(
                    name=name,
                    tool="",
                    status=p.status.value,
                    iteration=p.iteration,
                    of=p.of,
                )
            )
    return FlowView(
        flow_id=flow_id,
        conduit_name=conduit_name,
        status=progress.status.value,
        started_at=progress.started_at,
        finished_at=progress.finished_at,
        run_path=progress.run_path,
        parent_flow_id=parent_flow_id,
        parent_task=progress.invoking_task if parent_flow_id else None,
        current_tasks=list(progress.current_tasks),
        tasks=tasks,
    )


def build_task_log(
    task: str,
    tool: str,
    progress: TaskProgress | None,
    entries: list[LogEntry],
    steps: list[StepRecord],
    sub_runs: list[tuple[str, str]] | None = None,
) -> TaskLogView:
    """Return one task's log, grouped into rounds, one line per action.

    :param task: task name.
    :param tool: the task's tool (``tool:bash``, ``harness:codex``, ...).
    :param progress: the task's saved progress, or ``None`` if it never started.
    :param entries: the task's finished attempts from ``logs.jsonl``.
    :param steps: the task's live steps from ``steps.jsonl``.
    :param sub_runs: ``(started_at, flow_id)`` of each sub-run a
        ``tool:conduit`` task started; each is linked to its round.
    :returns: the task log's read model.
    """
    iterations = {e.iteration for e in entries} | {s.iteration for s in steps}
    # A round that has recorded nothing yet (a sub-conduit, or a command that
    # has not printed) still shows, so the page can say it is running.
    if progress is not None and progress.status == TaskStatus.running:
        iterations.add(progress.iteration)
    rounds = [
        _build_round(
            iteration,
            tool,
            progress,
            [e for e in entries if e.iteration == iteration],
            [s.step for s in steps if s.iteration == iteration],
        )
        for iteration in sorted(iterations)
    ]
    if sub_runs:
        _link_sub_runs(rounds, entries, sub_runs)
    return TaskLogView(
        task=task,
        tool=tool,
        status=progress.status.value if progress else TaskStatus.pending.value,
        reason=progress.reason if progress else None,
        of=progress.of if progress else max((e.of for e in entries), default=1),
        rounds=rounds,
    )


def task_log_update(old: TaskLogView, new: TaskLogView) -> TaskLogUpdate | None:
    """Return what ``new`` adds to ``old``, or ``None`` if only a resend describes it.

    Rounds only grow while a task runs, so an update carries each changed
    round's state and the lines it gained. When a line the client already has
    changed (a failed tool result marks its call, or a finished round swaps
    its recorded lines for the saved output), there is no update and the
    whole log has to be sent again.

    :param old: the log the client holds.
    :param new: the log as it is now.
    :returns: the update, or ``None`` when ``new`` is not ``old`` plus lines.
    """
    if (old.task, old.tool) != (new.task, new.tool):
        return None
    before = {r.iteration: r for r in old.rounds}
    if not before.keys() <= {r.iteration for r in new.rounds}:
        return None
    patches: list[TaskRoundPatch] = []
    for now in new.rounds:
        was = before.get(now.iteration)
        seen = len(was.lines) if was else 0
        if was is not None and now.lines[:seen] != was.lines:
            return None
        if was == now:
            continue
        patches.append(
            TaskRoundPatch(
                iteration=now.iteration,
                status=now.status,
                started_at=now.started_at,
                duration_seconds=now.duration_seconds,
                exit_code=now.exit_code,
                child_flow_id=now.child_flow_id,
                append=now.lines[seen:],
            )
        )
    return TaskLogUpdate(
        task=new.task, status=new.status, reason=new.reason, of=new.of, rounds=patches
    )


def _dependency_names(deps: list[str]) -> list[str]:
    """Return the task names ``deps`` point at, without their conditions.

    :param deps: raw ``depends_on`` strings.
    :returns: dependency task names; unparsable entries are left out.
    """
    names: list[str] = []
    for dep in deps:
        try:
            names.append(parse_dependency(dep).task)
        except DependencyParseError:
            continue
    return names


def _build_round(
    iteration: int,
    tool: str,
    progress: TaskProgress | None,
    entries: list[LogEntry],
    steps: list[IntermediateStep],
) -> TaskLogRound:
    """Build one round from its finished attempts and its live steps.

    Steps are not tagged with a retry attempt, so each attempt takes the steps
    stamped before it finished. Whatever is left belongs to the attempt still
    running.

    :param iteration: 1-based round number.
    :param tool: the task's tool.
    :param progress: the task's saved progress.
    :param entries: this round's finished attempts.
    :param steps: this round's live steps, in the order they arrived.
    :returns: the round's read model.
    """
    entries = sorted(entries, key=lambda e: e.started_at)
    lines: list[TaskLogLine] = []
    remaining = list(steps)
    for entry in entries:
        end = _parse_time(entry.finished_at)
        cut = 0
        while cut < len(remaining) and _stamped_by(remaining[cut], end):
            cut += 1
        lines.extend(_entry_lines(entry, tool, remaining[:cut]))
        remaining = remaining[cut:]
    lines.extend(_step_lines(remaining))

    if not entries:
        running = progress is None or progress.status == TaskStatus.running
        status = "running" if running else progress.status.value
        return TaskLogRound(
            iteration=iteration,
            status=status,
            started_at=steps[0].timestamp if steps else None,
            lines=lines,
        )
    last = entries[-1]
    return TaskLogRound(
        iteration=iteration,
        status="completed" if last.exit_code == 0 else "failed",
        started_at=entries[0].started_at,
        duration_seconds=round(sum(e.duration_seconds for e in entries), 3),
        exit_code=last.exit_code,
        lines=lines,
    )


def _link_sub_runs(
    rounds: list[TaskLogRound], entries: list[LogEntry], sub_runs: list[tuple[str, str]]
) -> None:
    """Give each round of a ``tool:conduit`` task the sub-run it started.

    A task's rounds run one after another and each starts (or resumes) one
    sub-run, so a sub-run belongs to the last round that began before it did.
    A round that has recorded nothing yet began when the one before it ended.

    :param rounds: the task's rounds, in order; updated in place.
    :param entries: the task's finished attempts.
    :param sub_runs: ``(started_at, flow_id)`` of each sub-run the task started.
    """
    # ponytail: matched by start time, since the parent does not record which
    # sub-run a round started. If a round ever shows the wrong one, have the
    # conduit executor record the id on the round instead.
    ended: dict[int, datetime] = {}
    for e in entries:
        at = _parse_time(e.finished_at)
        if at is not None and (e.iteration not in ended or at > ended[e.iteration]):
            ended[e.iteration] = at
    starts: list[datetime] = []
    previous_end = datetime.min.replace(tzinfo=UTC)
    for r in rounds:
        starts.append(_parse_time(r.started_at) or previous_end)
        previous_end = ended.get(r.iteration, previous_end)
    for started_at, flow_id in sorted(sub_runs):
        at = _parse_time(started_at)
        i = bisect_right(starts, at) - 1 if at is not None else -1
        if i >= 0:
            rounds[i].child_flow_id = flow_id


def _stamped_by(step: IntermediateStep, end: datetime | None) -> bool:
    """Return whether ``step`` was recorded no later than ``end``.

    :param step: a live step.
    :param end: an attempt's finish time, or ``None`` if unreadable.
    :returns: ``True`` when the step belongs before that finish.
    """
    at = _parse_time(step.timestamp)
    return end is None or at is None or at <= end


def _parse_time(value: str | None) -> datetime | None:
    """Parse an ISO-8601 timestamp, or return ``None`` if it is not one.

    :param value: timestamp text.
    :returns: the parsed time, or ``None``.
    """
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _entry_lines(
    entry: LogEntry, tool: str, steps: list[IntermediateStep]
) -> list[TaskLogLine]:
    """Return the lines for one finished attempt, its steps in between.

    :param entry: the finished attempt.
    :param tool: the task's tool.
    :param steps: the live steps this attempt recorded.
    :returns: the attempt's lines, start to finish.
    """
    from flow_atelier.cli.rendering.render import _condense, _redact

    failed = entry.exit_code != 0
    command = _condense(entry.command, _SUMMARY_CHARS)
    if tool == "tool:bash":
        start = TaskLogLine(at=entry.started_at, kind="run", text=f"$ {command}")
    elif tool == "tool:hitl":
        start = TaskLogLine(at=entry.started_at, kind="ask", text=command)
    elif tool == "tool:conduit":
        start = TaskLogLine(at=entry.started_at, kind="run", text=f"conduit {command}")
    else:
        start = TaskLogLine(at=entry.started_at, kind="prompt", text=command)
    attempt = entry.extra.get("attempt")
    of_attempts = entry.extra.get("of_attempts")
    if attempt and of_attempts:
        start.text += f"  (attempt {attempt} of {of_attempts})"

    printed = (StepKind.stdout, StepKind.stderr)
    lines = [start, *_step_lines([s for s in steps if s.kind not in printed])]
    if tool.startswith("harness:"):
        said = entry.last_turn_output or entry.output
        if said.strip():
            lines.append(TaskLogLine(kind="said", text=_redact(said.strip())))
    elif tool == "tool:hitl":
        if entry.output.strip():
            lines.append(TaskLogLine(kind="answer", text=_redact(entry.output.strip())))
    else:
        err_level = "error" if failed else "warn"
        saved = [
            *_output_lines(entry.stdout, "out", "info"),
            *_output_lines(entry.stderr, "err", err_level),
        ]
        recorded = _step_lines([s for s in steps if s.kind in printed])
        # The recorded lines carry the time each was printed, but a capped or
        # cut-short recording is missing some; the saved output never is.
        if Counter((x.kind, x.text) for x in recorded) == Counter((x.kind, x.text) for x in saved):
            for line in recorded:
                if line.kind == "err":
                    line.level = err_level
            lines.extend(recorded)
        else:
            lines.extend(saved)
    lines.append(
        TaskLogLine(
            at=entry.finished_at,
            kind="failed" if failed else "done",
            text=f"exit {entry.exit_code} · {_duration(entry.duration_seconds)}",
            level="error" if failed else "info",
        )
    )
    return lines


def _output_lines(text: str, kind: str, level: str) -> list[TaskLogLine]:
    """Split captured output into one masked line per non-blank line.

    Output is captured when the attempt ends, so these lines carry no time of
    their own.

    :param text: captured stdout or stderr.
    :param kind: ``out`` or ``err``.
    :param level: level every line gets.
    :returns: one line per non-blank line of ``text``.
    """
    from flow_atelier.cli.rendering.render import _redact

    return [
        TaskLogLine(kind=kind, text=_redact(line.rstrip()), level=level)
        for line in text.splitlines()
        if line.strip()
    ]


def _step_lines(steps: list[IntermediateStep]) -> list[TaskLogLine]:
    """Return one line per action or printed line, each tool result folded into its call.

    A result that succeeded adds nothing; one that failed turns its call into
    an error line that says why.

    :param steps: live steps, in the order they arrived.
    :returns: the actions as lines.
    """
    from flow_atelier.cli.rendering.render import _condense, _redact, _tool_arg

    lines: list[TaskLogLine] = []
    calls: dict[str, TaskLogLine] = {}
    for step in steps:
        if step.kind == StepKind.tool_call:
            verb = _TOOL_VERBS.get(step.tool_kind, "tool")
            arg = _tool_arg(step)
            name = _redact(step.tool_name)
            if verb == "tool" and name and arg:
                text = f"{name}  {arg}"
            else:
                text = arg or name or "tool call"
            line = TaskLogLine(at=step.timestamp, kind=verb, text=text)
            lines.append(line)
            if step.tool_call_id:
                calls[step.tool_call_id] = line
        elif step.kind == StepKind.tool_result:
            if step.tool_status != "failed":
                continue
            why = _condense(step.tool_output, _FAILURE_CHARS)
            call = calls.get(step.tool_call_id)
            if call is None:
                lines.append(
                    TaskLogLine(
                        at=step.timestamp,
                        kind="result",
                        text=f"failed: {why}" if why else "failed",
                        level="error",
                    )
                )
            else:
                call.text += f" → failed: {why}" if why else " → failed"
                call.level = "error"
        elif step.kind == StepKind.thinking:
            lines.append(
                TaskLogLine(
                    at=step.timestamp,
                    kind="thought",
                    text=_condense(step.text, _THOUGHT_CHARS),
                )
            )
        elif step.kind == StepKind.interaction:
            lines.append(
                TaskLogLine(at=step.timestamp, kind="ask", text=_redact(step.text.strip()))
            )
        elif step.kind in (StepKind.stdout, StepKind.stderr):
            out = step.kind == StepKind.stdout
            lines.extend(
                TaskLogLine(
                    at=step.timestamp,
                    kind="out" if out else "err",
                    text=_redact(line.rstrip()),
                    level="info" if out else "warn",
                )
                for line in step.text.splitlines()
                if line.strip()
            )
    return lines


def _duration(seconds: float) -> str:
    """Format a duration the way the run page shows it.

    :param seconds: elapsed seconds.
    :returns: ``0.3s``, ``42s`` or ``3m 03s``.
    """
    if seconds < 10:
        return f"{seconds:.1f}s"
    if seconds < 60:
        return f"{round(seconds)}s"
    minutes, rest = divmod(round(seconds), 60)
    return f"{minutes}m {rest:02d}s"
