"""Build the post-mortem report behind ``atelier diagnose``.

Reads only what a run already saved — ``progress.json``, ``logs.jsonl``,
``steps.jsonl``, ``outputs.yaml`` and, when it is still installed, the recipe —
and arranges it as evidence a person or a coding agent can act on. Nothing is
written, no agent is started, and no claim is made that the store does not
support: every attribution carries where it came from, and every observation
the filesystem cannot settle is labelled uncertain rather than guessed.

The one ordering rule that matters: ``progress.json`` is read first and again
last. The related files are read in between, so a run that moves while the
report is being built is reported as a mixed read instead of a settled one.
"""
from __future__ import annotations

import shlex
from datetime import datetime
from typing import Any

import yaml
from pydantic import BaseModel, Field, ValidationError

from flow_atelier.modules.liveness import is_crashed, is_runner_alive
from flow_atelier.schemas.conduit import Conduit
from flow_atelier.schemas.flow import parse_flow_id
from flow_atelier.schemas.log import LogEntry
from flow_atelier.schemas.progress import FlowStatus, Progress, TaskStatus

# How much of a failing task's own output the report carries inline. The tail
# is what holds the error, and the full text is one printed command away, so
# this is a reading bound rather than a retention decision.
EXCERPT_MAX_CHARS = 700
EXCERPT_MAX_LINES = 12

# What an unreadable saved file raises. Only ``progress.json`` is fatal here:
# every other file is one source of evidence among several, so a corrupt one
# costs its own contribution and nothing else. ``TypeError`` belongs in the list
# because a saved file can hold the wrong *shape* as well as broken syntax — a
# legacy ``logs.json`` holding ``null`` parses fine and then cannot be iterated —
# and a report that dies on one such file hides every fact it could still give.
UNREADABLE = (yaml.YAMLError, ValidationError, ValueError, TypeError, OSError)

# The harness and the shell executor both write this phrase with this exit code
# when a task runs out of time. Together they are the timeout's own record —
# which is why a timeout is never mistaken here for a failure to start.
TIMEOUT_EXIT = 124
TIMEOUT_MARKER = "timeout after "

# An agent that will not open a session says so. Matching what it said is
# reading evidence; concluding it from an absent output is not, because a
# session that opened and then ran out of time leaves the same silence.
SESSION_REFUSALS = (
    "authentication required",
    "authentication_required",
    "not logged in",
    "not authenticated",
    "unauthenticated",
    "unauthorized",
    "please log in",
    "login required",
)


class DiagnoseError(Exception):
    """A flow cannot be diagnosed, with the text the user has to act on.

    :param message: what went wrong and what it means.
    :param hint: one command that would resolve it, kept separate so the caller
        can print it unwrapped and copyable.
    """

    def __init__(self, message: str, hint: str | None = None) -> None:
        super().__init__(message)
        self.hint = hint


class Provenance(BaseModel):
    """One fact plus where it was read from.

    ``source`` is ``log`` when the value is what actually ran, ``selection``
    when it is the choice this run recorded for a task, and ``unknown`` when
    nothing saved says. The current recipe is never a source here: it can be
    edited after the run and so cannot testify about the past.
    """

    value: str | None = None
    source: str = "unknown"


class Excerpt(BaseModel):
    """A bounded tail of what a failing task emitted."""

    channel: str
    text: str
    truncated: bool = False


class TaskReport(BaseModel):
    """What the store records about one task of the diagnosed run."""

    task: str
    status: str
    reason: str | None = None
    exit_code: int | None = None
    ran_on: Provenance = Field(default_factory=Provenance)
    current_tool: str | None = None
    #: ``log`` a completed log entry, ``steps`` live step records only,
    #: ``reason`` the saved reason only, ``none`` nothing beyond the status.
    evidence: str = "none"
    step_records: int = 0
    #: None when ``outputs.yaml`` could not be read, so absence is not a claim
    #: that this task saved nothing.
    output_saved: bool | None = False
    output_chars: int | None = None
    #: unfinished dispositions only — ``not_started`` the run never reached it,
    #: ``began`` saved records prove it was executing before it was cut off,
    #: ``unknown`` nothing saved settles whether it started.
    execution: str | None = None
    #: failures only — ``agent_session`` the agent's own words refuse a session,
    #: ``timeout`` the executor recorded its own time limit, ``unclear_stage``
    #: a harness task that saved nothing to place its failure by, ``task`` the
    #: work ran and returned nonzero.
    kind: str | None = None
    excerpt: Excerpt | None = None


class SavedRun(BaseModel):
    """The flow-level facts as the run itself wrote them."""

    status: str
    started_at: str | None = None
    finished_at: str | None = None
    duration_seconds: float | None = None
    run_path: str | None = None
    runner_pid: int | None = None
    runner_host: str | None = None
    invoking_task: str | None = None


class Observation(BaseModel):
    """What can be observed *now* about the runner, separate from the record."""

    state: str
    certainty: str
    note: str


class Snapshot(BaseModel):
    """Whether the files read for this report agreed with each other."""

    consistent: bool = True
    note: str = "progress.json was unchanged across the read"


class RecipeNote(BaseModel):
    """The conduit as it stands now — current, never historical."""

    conduit: str
    available: bool
    note: str
    added_tasks: list[str] = Field(default_factory=list)
    missing_tasks: list[str] = Field(default_factory=list)


class ChildPointer(BaseModel):
    """A nested flow this run started, named but not aggregated."""

    flow_id: str
    invoking_task: str | None = None
    status: str | None = None


class NextStep(BaseModel):
    """One suggested command. Diagnose never runs any of them."""

    what: str
    command: str | None = None
    side_effects: str | None = None


class DiagnoseReport(BaseModel):
    """The whole report, and the documented shape of ``diagnose --json``."""

    flow_id: str
    conduit: str
    saved: SavedRun
    observed: Observation
    snapshot: Snapshot
    recipe: RecipeNote
    failures: list[TaskReport] = Field(default_factory=list)
    #: Cut off when the run failed. Kept out of ``not_run`` on purpose: a task
    #: can be cancelled mid-flight, having already changed files.
    cancelled: list[TaskReport] = Field(default_factory=list)
    not_run: list[TaskReport] = Field(default_factory=list)
    running: list[TaskReport] = Field(default_factory=list)
    kept: list[TaskReport] = Field(default_factory=list)
    children: list[ChildPointer] = Field(default_factory=list)
    unavailable: list[str] = Field(default_factory=list)
    next_steps: list[NextStep] = Field(default_factory=list)


def _why(exc: Exception) -> str:
    """Collapse an exception's own words onto one line.

    A YAML parse error arrives as a small indented report of its own; folded
    into a note it breaks the layout for no gain, and the detail that matters
    survives the collapse.

    :param exc: the exception raised while reading a saved file.
    :returns: its message on a single line.
    """
    return " ".join(str(exc).split())


def _bound(text: str) -> Excerpt | None:
    """Keep both ends of ``text`` and say how much was dropped between them.

    Both ends, because which one matters depends on what failed: a harness
    announces its verdict on the first line of stderr, while a shell script's
    reason for dying is usually on the last. Cutting only one end would hide
    exactly one of the two.

    :param text: the raw channel contents.
    :returns: the bounded excerpt, or None when there is nothing to show.
    """
    stripped = text.strip()
    if not stripped:
        return None
    lines = stripped.splitlines()
    truncated = False
    if len(lines) > EXCERPT_MAX_LINES:
        head = lines[: EXCERPT_MAX_LINES // 3]
        tail = lines[-(EXCERPT_MAX_LINES - len(head) - 1) :]
        omitted = len(lines) - len(head) - len(tail)
        lines = [*head, f"... {omitted} lines omitted ...", *tail]
        truncated = True
    kept = "\n".join(lines)
    if len(kept) > EXCERPT_MAX_CHARS:
        head_chars = EXCERPT_MAX_CHARS // 3
        kept = (
            kept[:head_chars]
            + "\n... trimmed ...\n"
            + kept[-(EXCERPT_MAX_CHARS - head_chars) :]
        )
        truncated = True
    return Excerpt(channel="", text=kept, truncated=truncated)


def _excerpt_of(entry: LogEntry) -> Excerpt | None:
    """Pick the channel that explains a failure and bound it.

    :param entry: the failing task's last log entry.
    :returns: a bounded excerpt naming its channel, or None when all are empty.
    """
    for channel in ("stderr", "output", "stdout"):
        found = _bound(getattr(entry, channel))
        if found is not None:
            return found.model_copy(update={"channel": channel})
    return None


def _observe(progress: Progress) -> Observation:
    """Classify the runner's present state without ever guessing.

    ``failed``, ``stopped`` and ``completed`` are the run's own verdict on
    itself. A saved ``running`` is only called ``crashed`` when the local pid is
    provably gone, and only ``running`` when it is provably alive; everything in
    between — another host, no pid, an unprobeable pid — stays uncertain.

    :param progress: the flow's progress snapshot.
    :returns: the state, how certain it is, and the reason in words.
    """
    status = progress.status
    if status == FlowStatus.failed:
        return Observation(
            state="failed",
            certainty="proven",
            note="the run recorded its own failure and its runner has exited",
        )
    if status == FlowStatus.stopped:
        return Observation(
            state="stopped",
            certainty="proven",
            note="the run was stopped on purpose, so it recorded no failure",
        )
    if status == FlowStatus.completed:
        return Observation(
            state="completed",
            certainty="proven",
            note="every task the run executed reached a terminal state and it finished",
        )
    if is_crashed(progress):
        return Observation(
            state="crashed",
            certainty="proven",
            note=(
                f"saved status is still running, but runner pid {progress.runner_pid} "
                "is gone on this host — the process died without recording a failure"
            ),
        )
    if is_runner_alive(progress):
        return Observation(
            state="running",
            certainty="proven",
            note=(
                f"runner pid {progress.runner_pid} is alive on this host; this run "
                "is still going and what follows is a mid-flight reading"
            ),
        )
    if progress.runner_pid is None:
        why = "no runner pid was recorded, so nothing can be probed"
    elif progress.runner_host and progress.runner_host != "":
        why = (
            f"the runner was recorded on host {progress.runner_host!r}, which cannot "
            "be probed from here"
        )
    else:
        why = f"runner pid {progress.runner_pid} could not be probed cleanly"
    return Observation(
        state="running",
        certainty="uncertain",
        note=f"saved status is running and {why}",
    )


def _read_recipe(atelier: Any, conduit_name: str, progress: Progress) -> tuple[
    Conduit | None, RecipeNote
]:
    """Load the conduit as it stands now and say how it relates to the run.

    A missing or broken recipe is a note, never a failure: the saved
    diagnostics are what a post-mortem needs and they do not depend on it.

    :param atelier: the facade, for its store.
    :param conduit_name: the conduit named by the flow id.
    :param progress: the run's progress, for the task names it recorded.
    :returns: the conduit when readable, plus the note to print either way.
    """
    try:
        conduit = atelier.store.read_conduit(conduit_name)
    except FileNotFoundError:
        return None, RecipeNote(
            conduit=conduit_name,
            available=False,
            note=(
                "the conduit is not installed here any more, so no current "
                "definition can be shown; the saved evidence below is unaffected, "
                "but a resume needs the recipe back"
            ),
        )
    except (ValueError, OSError, UnicodeDecodeError, ValidationError) as exc:
        return None, RecipeNote(
            conduit=conduit_name,
            available=False,
            note=(
                f"the current definition cannot be read ({exc}); the saved evidence "
                "below is unaffected, but a resume needs it readable"
            ),
        )
    current = [t.name for t in conduit.tasks]
    recorded = set(progress.tasks)
    added = [name for name in current if name not in recorded]
    missing = sorted(recorded - set(current))
    note = (
        "the conduit as it stands now; a resume reads this file, not the run's "
        "recorded shape"
    )
    if added or missing:
        note = (
            "the current definition no longer matches what this run recorded, and a "
            "resume reads the current one"
        )
    return conduit, RecipeNote(
        conduit=conduit_name,
        available=True,
        note=note,
        added_tasks=added,
        missing_tasks=missing,
    )


def _ran_on(task: str, entries: list[LogEntry], progress: Progress) -> Provenance:
    """Attribute a task to the tool that actually served it.

    The last log entry wins because it is the only record of what ran. A saved
    per-run selection is the next best thing — it says what this run was told
    to use. The current recipe is deliberately not consulted.

    :param task: the task name.
    :param entries: the run's log entries.
    :param progress: the run's progress, for its recorded selections.
    :returns: the tool and where it was read from.
    """
    for entry in reversed(entries):
        if entry.task == task:
            return Provenance(value=entry.tool, source="log")
    chosen = progress.task_agents.get(task)
    if chosen:
        return Provenance(value=chosen, source="selection")
    return Provenance(value=None, source="unknown")


def _failure_kind(report: TaskReport, last: LogEntry | None) -> str:
    """Place a failure by what the run recorded, never by what it omitted.

    The two affirmative records are the executor's own timeout marker and the
    agent's own refusal to open a session. Without either, a harness task that
    saved no output and no step could have died at any point between spawning
    and its last token, so the stage is reported as unsettled rather than
    guessed — an agent that timed out mid-prompt leaves exactly the silence a
    logged-out one does, and advising a login for it sends the user nowhere.

    A session refusal needs more than the words: only an agent has a session to
    open, and only a task that recorded nothing of its own can be said to have
    done no work. A shell task that ran, changed files and then printed an HTTP
    401 of its own is an ordinary task failure with an authentication message in
    it, so it is reported as one.

    :param report: the task report so far, for its attribution and exit code.
    :param last: the task's last log entry, when it has one.
    :returns: ``timeout``, ``agent_session``, ``unclear_stage`` or ``task``.
    """
    # stderr and the saved reason only: those are where an executor and a
    # harness explain themselves. A task's own output can discuss a login
    # without that being what happened to it.
    said = " ".join(
        filter(None, [report.reason or "", last.stderr if last is not None else ""])
    ).lower()
    if report.exit_code == TIMEOUT_EXIT and TIMEOUT_MARKER in said:
        return "timeout"
    on_harness = (report.ran_on.value or "").startswith("harness:")
    began = bool(report.step_records) or (last is not None and bool(last.output))
    if on_harness and not began:
        if any(marker in said for marker in SESSION_REFUSALS):
            return "agent_session"
        return "unclear_stage"
    return "task"


def _task_report(
    task: str,
    progress: Progress,
    entries: list[LogEntry],
    step_counts: dict[str, int],
    outputs: dict[str, Any] | None,
    current_tools: dict[str, str],
) -> TaskReport:
    """Assemble every saved fact about one task.

    :param task: the task name.
    :param progress: the run's progress snapshot.
    :param entries: the run's log entries.
    :param step_counts: live step-record counts per task.
    :param outputs: the saved ``outputs.yaml`` map, or None when it could not
        be read — which is not the same as a run that saved nothing.
    :param current_tools: task name to tool in the recipe as it stands now.
    :returns: the per-task report.
    """
    saved = progress.tasks[task]
    mine = [e for e in entries if e.task == task]
    last = mine[-1] if mine else None
    steps = step_counts.get(task, 0)
    if last is not None:
        evidence = "log"
    elif steps:
        evidence = "steps"
    elif saved.reason:
        evidence = "reason"
    else:
        evidence = "none"
    value = outputs.get(task) if outputs is not None else None
    report = TaskReport(
        task=task,
        status=saved.status.value,
        reason=saved.reason,
        exit_code=last.exit_code if last is not None else None,
        ran_on=_ran_on(task, entries, progress),
        current_tool=current_tools.get(task),
        evidence=evidence,
        step_records=steps,
        output_saved=None if outputs is None else value is not None,
        output_chars=len(str(value)) if value is not None else None,
    )
    if saved.status in (TaskStatus.skipped, TaskStatus.pending):
        report.execution = "not_started"
    elif saved.status == TaskStatus.cancelled:
        # A cancelled task may have been executing when the run failed. Its own
        # log entry or live steps are the only proof either way, and without
        # them non-execution is an assumption, not a reading.
        report.execution = "began" if evidence in ("log", "steps") else "unknown"
    if saved.status == TaskStatus.failed:
        report.excerpt = _excerpt_of(last) if last is not None else None
        report.kind = _failure_kind(report, last)
    return report


def _children(atelier: Any, flow_id: str) -> tuple[list[ChildPointer], list[str]]:
    """Name the nested flows this run started, without aggregating them.

    :param atelier: the facade, for its store.
    :param flow_id: the diagnosed flow id.
    :returns: the child pointers, plus any note about unreadable children.
    """
    pointers: list[ChildPointer] = []
    notes: list[str] = []
    try:
        child_ids = atelier.store.list_child_flows(flow_id)
    except (FileNotFoundError, OSError):
        return pointers, notes
    for child in child_ids:
        try:
            child_progress = atelier.store.read_progress(child)
        except (FileNotFoundError, OSError, ValueError, ValidationError):
            pointers.append(ChildPointer(flow_id=child))
            notes.append(
                f"nested flow {child}: its saved progress cannot be read, so its "
                "status is unknown here"
            )
            continue
        pointers.append(
            ChildPointer(
                flow_id=child,
                invoking_task=child_progress.invoking_task,
                status=child_progress.status.value,
            )
        )
    return pointers, notes


def _unfinished_count(
    progress: Progress, conduit: Conduit | None, reports: list[TaskReport]
) -> int:
    """Count the work a resume would still have to do.

    Taken from the rule resume itself applies — a task of the recipe it reads
    whose saved status is not ``completed``. With no readable recipe there is no
    such list, so the run's own record is the only thing left to count.

    :param progress: the run's progress snapshot.
    :param conduit: the recipe as it stands now, when readable.
    :param reports: the per-task reports, for the no-recipe fallback.
    :returns: how many tasks are not completed.
    """
    if conduit is not None:
        return sum(
            1
            for task in conduit.tasks
            if progress.tasks.get(task.name) is None
            or progress.tasks[task.name].status != TaskStatus.completed
        )
    return sum(1 for r in reports if r.status != TaskStatus.completed.value)


def _next_steps(
    flow_id: str,
    observed: Observation,
    failures: list[TaskReport],
    kept: list[TaskReport],
    unfinished: int,
    recipe: RecipeNote,
) -> list[NextStep]:
    """Build the suggested commands, keyed off what was actually observed.

    Resume is only offered where the engine supports it *and* the runner is
    not possibly still alive. Every suggestion is text: diagnose runs nothing.

    :param flow_id: the resolved full flow id, used verbatim in every command.
    :param observed: the runner observation.
    :param failures: the failed tasks.
    :param kept: the completed tasks.
    :param unfinished: how many recorded tasks did not complete.
    :param recipe: the current-definition note.
    :returns: the ordered suggestions.
    """
    quoted = shlex.quote(flow_id)
    steps: list[NextStep] = []
    for failure in failures[:3]:
        steps.append(
            NextStep(
                what=f"read everything task {failure.task!r} recorded",
                command=(
                    f"atelier logs {quoted} --task {shlex.quote(failure.task)} --show all"
                ),
            )
        )
    steps.append(
        NextStep(
            what="read the whole run, every channel, untruncated",
            command=f"atelier logs {quoted} --show all",
        )
    )
    if any(k.output_saved for k in kept):
        steps.append(
            NextStep(
                what="read the results this run already saved",
                command=f"atelier outputs {quoted}",
            )
        )
    if observed.state in ("failed", "crashed"):
        if not recipe.available:
            steps.append(
                NextStep(
                    what=(
                        "a resume needs the recipe readable again — restore it, then "
                        "resume"
                    ),
                    command=f"atelier run --resume {quoted}",
                    side_effects=(
                        "runs the tasks this run did not complete, under the recipe as "
                        "it reads then; agent tasks can spend tokens again"
                    ),
                )
            )
        elif unfinished:
            steps.append(
                NextStep(
                    what="continue the run, keeping what already completed",
                    command=f"atelier run --resume {quoted}",
                    side_effects=(
                        "runs the tasks this run did not complete, under the recipe as "
                        "it reads then; agent tasks can spend tokens and touch your "
                        "files again"
                    ),
                )
            )
        else:
            steps.append(
                NextStep(
                    what=(
                        "nothing recorded is left to finish — start a fresh run if you "
                        "want the work done again"
                    ),
                    command=f"atelier run --again {quoted}",
                    side_effects="starts a new flow and runs every task from the start",
                )
            )
    elif observed.state == "running" and observed.certainty == "proven":
        steps.append(
            NextStep(
                what=(
                    "do not resume: this run's runner is alive. Watch it, or stop it "
                    "first"
                ),
                command=f"atelier logs {quoted} --follow",
            )
        )
        steps.append(
            NextStep(
                what="stop it yourself, if that is what you want",
                command=f"atelier stop {quoted}",
                side_effects="cancels the tasks in flight; the run is recorded stopped",
            )
        )
    elif observed.state == "running":
        steps.append(
            NextStep(
                what=(
                    "resume is possible but not recommended until you have confirmed "
                    "the recorded runner is really gone — from here it cannot be "
                    "probed, and a resume would then double-run the work"
                ),
                command=f"atelier run --resume {quoted}",
                side_effects=(
                    "runs the tasks this run did not complete; if the original runner "
                    "is in fact alive, both work at once — a double-run"
                ),
            )
        )
    elif observed.state == "stopped":
        steps.append(
            NextStep(
                what=(
                    "a stopped run cannot be resumed — start a fresh run of the same "
                    "recipe and inputs"
                ),
                command=f"atelier run --again {quoted}",
                side_effects="starts a new flow and runs every task from the start",
            )
        )
    else:
        steps.append(
            NextStep(
                what="this run finished; there is nothing to recover",
                command=None,
            )
        )
    return steps


def build_report(atelier: Any, flow_id: str) -> DiagnoseReport:
    """Read everything a run saved and arrange it as a post-mortem.

    :param atelier: the :class:`~flow_atelier.core.atelier.Atelier` facade.
    :param flow_id: the already-resolved full flow id.
    :returns: the report.
    :raises DiagnoseError: the run's own progress record cannot be read, which
        is the one file a report cannot be built without.
    """
    try:
        progress = atelier.store.read_progress(flow_id)
    except FileNotFoundError as exc:
        raise DiagnoseError(f"unknown flow: {flow_id}") from exc
    except (ValidationError, ValueError, OSError) as exc:
        raise DiagnoseError(
            f"cannot read the saved progress of {flow_id}: {exc}. The flow directory "
            "is there but its progress.json is not a valid record, so nothing can be "
            "said about what ran. Inspect that file directly, or drop the flow:",
            hint=f"atelier rm flow {shlex.quote(flow_id)}",
        ) from exc

    try:
        conduit_name, _, _ = parse_flow_id(flow_id)
    except ValueError as exc:
        raise DiagnoseError(f"not a flow id: {flow_id} ({exc})") from exc

    # Everything below progress.json is optional evidence. A corrupt one is
    # reported as unavailable and costs only itself: losing the whole report —
    # the saved statuses, the reasons, the recovery commands — because one
    # auxiliary file will not parse is the opposite of a post-mortem.
    unavailable: list[str] = []
    try:
        entries = atelier.store.read_logs(flow_id)
    except UNREADABLE as exc:
        entries = []
        unavailable.append(
            f"log entries — the saved log cannot be read ({_why(exc)}), so no task's "
            "own output is shown below; the saved statuses and reasons are unaffected. "
            f"Read logs.jsonl or logs.json directly in the flow directory of {flow_id}"
        )
    else:
        if not entries:
            unavailable.append(
                "log entries — none recorded, so no task's own output can be shown"
            )
    step_counts: dict[str, int] = {}
    try:
        records, _ = atelier.store.read_steps(flow_id)
    except UNREADABLE as exc:
        records = []
        unavailable.append(
            f"live step records — steps.jsonl cannot be read ({_why(exc)}); a task cut "
            "off mid-flight may have left steps that cannot be shown here"
        )
    else:
        if not records:
            unavailable.append("live step records — none recorded for this run")
    for record in records:
        step_counts[record.task] = step_counts.get(record.task, 0) + 1
    outputs: dict[str, Any] | None
    try:
        outputs = atelier.store.read_outputs(flow_id)
    except UNREADABLE as exc:
        # None, not {}: an unreadable file must not be reported as a run that
        # kept nothing, because a resume reads the same file and would then
        # have to run the completed work again.
        outputs = None
        unavailable.append(
            f"saved outputs — outputs.yaml cannot be read ({_why(exc)}), so whether each "
            "completed task still has a result is unknown, and a resume will hit "
            f"the same file. Repair or remove it in the flow directory of {flow_id}"
        )
    else:
        if not outputs:
            unavailable.append(
                "saved outputs — none recorded, so no result can be reread"
            )
    conduit, recipe = _read_recipe(atelier, conduit_name, progress)
    current_tools = (
        {t.name: t.tool for t in conduit.tasks} if conduit is not None else {}
    )
    children, child_notes = _children(atelier, flow_id)
    unavailable.extend(child_notes)

    reports = [
        _task_report(name, progress, entries, step_counts, outputs, current_tools)
        for name in progress.tasks
    ]
    if not reports:
        unavailable.append(
            "per-task records — the run saved none, so it died before its first task"
        )
    failures = [r for r in reports if r.status == TaskStatus.failed.value]
    kept = [r for r in reports if r.status == TaskStatus.completed.value]
    running = [r for r in reports if r.status == TaskStatus.running.value]
    cancelled = [r for r in reports if r.status == TaskStatus.cancelled.value]
    not_run = [
        r
        for r in reports
        if r.status in (TaskStatus.skipped.value, TaskStatus.pending.value)
    ]
    observed = _observe(progress)

    # Last read, and deliberately the same file as the first: anything the run
    # changed while the files above were being read makes this a mixed reading
    # rather than one settled snapshot.
    snapshot = Snapshot()
    try:
        after = atelier.store.read_progress(flow_id)
    except (FileNotFoundError, ValidationError, ValueError, OSError):
        snapshot = Snapshot(
            consistent=False,
            note=(
                "progress.json could not be read a second time, so this report mixes "
                "readings and is not a settled snapshot"
            ),
        )
    else:
        before_shape = (
            progress.status,
            {n: (t.status, t.iteration) for n, t in progress.tasks.items()},
        )
        after_shape = (
            after.status,
            {n: (t.status, t.iteration) for n, t in after.tasks.items()},
        )
        if before_shape != after_shape:
            snapshot = Snapshot(
                consistent=False,
                note=(
                    f"the run moved while this report was read (status "
                    f"{progress.status.value} -> {after.status.value}); it mixes "
                    "readings, so read it again for a settled view"
                ),
            )

    return DiagnoseReport(
        flow_id=flow_id,
        conduit=conduit_name,
        saved=SavedRun(
            status=progress.status.value,
            started_at=progress.started_at,
            finished_at=progress.finished_at,
            duration_seconds=_duration(progress),
            run_path=progress.run_path,
            runner_pid=progress.runner_pid,
            runner_host=progress.runner_host,
            invoking_task=progress.invoking_task,
        ),
        observed=observed,
        snapshot=snapshot,
        recipe=recipe,
        failures=failures,
        cancelled=cancelled,
        not_run=not_run,
        running=running,
        kept=kept,
        children=children,
        unavailable=unavailable,
        next_steps=_next_steps(
            flow_id,
            observed,
            failures,
            kept,
            _unfinished_count(progress, conduit, reports),
            recipe,
        ),
    )


def _duration(progress: Progress) -> float | None:
    """Return the run's wall-clock duration, or None while it is unfinished.

    :param progress: the flow's progress snapshot.
    :returns: seconds between start and finish, or None.
    """
    start = _iso(progress.started_at)
    end = _iso(progress.finished_at)
    if start is None or end is None:
        return None
    return (end - start).total_seconds()


def _iso(ts: str | None) -> datetime | None:
    """Parse one of the engine's Z-suffixed ISO timestamps.

    :param ts: the timestamp, or None.
    :returns: the datetime, or None when missing or unparseable.
    """
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None
