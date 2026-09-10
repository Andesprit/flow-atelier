"""Rich rendering helpers for the CLI presentation layer."""
from __future__ import annotations

import json
import re
from collections import Counter

import yaml
from pydantic import ValidationError
from rich.console import Console
from rich.markup import escape
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from flow_atelier.cli._shared import (
    _format_clock,
    _format_clock_short,
    _format_duration_seconds,
    _format_next_fire,
    _format_usage,
)
from flow_atelier.modules.plan import ExecutionPlan, PlannedTask
from flow_atelier.schemas.log import IntermediateStep, StepKind, TaskEvent
from flow_atelier.schemas.progress import FlowStatus, Progress, TaskStatus
from flow_atelier.services.scheduler import PlannedJob

# Payload keys worth showing next to a tool name, most specific first.
# Covers the common harness tools: Bash, Read/Write/Edit, Grep/Glob, Task,
# WebFetch/WebSearch. Anything unmatched falls back to compact ``k=v`` pairs.
_TOOL_ARG_KEYS: tuple[str, ...] = (
    "command",
    "pattern",
    "query",
    "file_path",
    "path",
    "url",
    "prompt",
    "description",
)

_STEP_ARG_CHARS = 90

# Backstop on a single result panel. Task output is printed in full — it is the
# product of the run and the reader may be an agent that cannot go and fetch
# what an ellipsis dropped — but the producer is unbounded: `BashExecutor`
# accumulates stdout with no cap, so `cat big.log` would otherwise wedge the
# terminal and force Rich to word-wrap the whole thing. Set far above any real
# task output, and the store always keeps the untruncated text.
PANEL_MAX_CHARS = 100_000

# Backstop on a rendered step timeline, for the same reason as
# `PANEL_MAX_CHARS` and on the axis that actually blows up here: step *count*,
# not character count. The producer is unbounded — an agent task can emit tens
# of thousands of steps — and the timeline is the only view of a run that was
# stopped or crashed, which is precisely the long-running case. The oldest
# steps are dropped: a post-mortem reads from the end.
TIMELINE_MAX_STEPS = 500

# Values worth hiding when a tool call is echoed to the terminal. Payloads were
# always persisted, but they are now *rendered*, so a token in a `curl -H` or a
# connection string ends up on screen and in whatever the user pastes into an
# issue. Best-effort by design: this reduces casual leakage, it is not a
# guarantee, and the store still holds the unredacted text.
_SECRET_RE = re.compile(
    r"""(?xi)
    (?:
        # `Bearer <token>` / `Authorization: <scheme> <token>`
        (?<=\bbearer\ )[\w\-.~+/]{8,}=*
      | (?<=\bbasic\ )[\w\-.~+/]{8,}=*
        # Well-known key prefixes (OpenAI, Anthropic, GitHub, Slack, AWS)
      | \b(?:sk|pk|rk)-[\w\-]{12,}
      | \bsk-ant-[\w\-]{12,}
      | \bgh[pousr]_[A-Za-z0-9]{16,}
      | \bxox[baprs]-[A-Za-z0-9\-]{10,}
      | \bAKIA[0-9A-Z]{16}\b
        # `--token X` / `--password=X`. The `--` prefix makes it unambiguously
        # a flag, so a bare space is enough to delimit the value.
      | --\w*(?:token|secret|password|passwd|api[_-]?key)\w*(?:[\s=]+)\S+
        # `TOKEN=X`, `api_key: X`. No `--` to disambiguate, so require an
        # explicit `=`/`:` — otherwise "the token is missing" masks "is".
      | \b\w*(?:token|secret|password|passwd|api[_-]?key)\w*\s*[=:]\s*\S+
    )
    """
)

_REDACTED = "***"


def _redact(text: str) -> str:
    """Mask credential-shaped substrings before echoing ``text`` to a terminal.

    Best-effort: a heuristic that catches the common shapes (bearer tokens,
    vendor key prefixes, ``--password``/``TOKEN=`` style flags), not a
    guarantee that nothing sensitive gets through. Applied on the render path
    only — the store keeps the original so a post-mortem is not lossy.

    :param text: raw payload text about to be displayed.
    :returns: ``text`` with credential-shaped runs replaced by ``***``.
    """
    return _SECRET_RE.sub(_REDACTED, text)


def _condense(text: str, limit: int = _STEP_ARG_CHARS) -> str:
    """Collapse whitespace, redact credentials, and truncate for one line.

    Every payload that reaches the terminal funnels through here, so this is
    where :func:`_redact` runs — redacting at each call site instead would
    mean a new display path silently skips it.

    :param text: raw text to flatten onto one line.
    :param limit: maximum characters to keep before eliding.
    :returns: whitespace-collapsed text, elided with ``…`` when over ``limit``.
    """
    flat = _redact(" ".join(text.split()))
    return flat if len(flat) <= limit else flat[:limit] + "…"


def _tool_arg(step: IntermediateStep) -> str:
    """Return the one detail that says *what* a tool call is doing.

    A bare ``🔧 Bash`` is unreadable; ``🔧 Bash  pytest tests/ -x`` is not.
    Prefers an ACP-reported file location, then a known payload key, then a
    compact rendering of the first payload fields.

    :param step: a ``tool_call`` step whose argument should be summarized.
    :returns: a single-line argument summary, or ``""`` when nothing useful.
    """
    if step.locations:
        return _condense(step.locations[0])
    if not step.tool_input:
        return ""
    try:
        data = json.loads(step.tool_input)
    except ValueError:
        # Truncated mid-JSON (see TOOL_PAYLOAD_CHARS) or a non-JSON payload.
        return _condense(step.tool_input)
    if not isinstance(data, dict):
        return _condense(str(data))
    for key in _TOOL_ARG_KEYS:
        value = data.get(key)
        if isinstance(value, str) and value.strip():
            return _condense(value)
    return _condense(", ".join(f"{k}={v}" for k, v in list(data.items())[:2]))


def _render_step(step: IntermediateStep, task: str = "") -> Text:
    """Render a single intermediate step as a compact Rich Text line.

    - Thinking: ``  💭 {text[:120]}...`` dim italic
    - Tool call: ``  🔧 {tool_name}  {argument}`` bold dim + dim
    - Tool result: ``     ✓ status`` or ``     ✗ status {output}`` green/red

    :param step: the intermediate step to render.
    :param task: owning task name; when given it is prefixed to the line so
        steps stay attributable while parallel tasks stream into one console.
    """
    t = Text()
    if task:
        t.append(f"  {task} ", style="cyan")
    else:
        t.append("  ")
    if step.kind == StepKind.interaction:
        t.append(step.text)
    elif step.kind == StepKind.thinking:
        truncated = step.text[:120] + ("..." if len(step.text) > 120 else "")
        t.append("💭 ", style="dim italic")
        t.append(truncated, style="dim italic")
    elif step.kind == StepKind.tool_call:
        t.append("🔧 ", style="dim")
        t.append(step.tool_name, style="bold dim")
        arg = _tool_arg(step)
        if arg:
            t.append(f"  {arg}", style="dim")
    elif step.kind == StepKind.tool_result:
        if step.tool_status == "failed":
            t.append("   ✗ ", style="red")
            t.append(step.tool_status, style="red")
            if step.tool_output:
                t.append(f"  {_condense(step.tool_output)}", style="red dim")
        else:
            t.append("   ✓ ", style="green")
            t.append(step.tool_status, style="green")
    return t


def render_agent_message(text: str, task: str = "") -> Text:
    """Render what the agent said, in full.

    Never truncated. This stream is read by other agents as well as by
    people, and a machine reader cannot recover what an ellipsis dropped —
    it would have to go find the flow's logs on disk. Tool *calls* collapse
    because which tool ran is cheap to summarize; what the agent actually
    said is the payload.

    Continuation lines are emitted verbatim, not re-indented under the
    marker. Padding them to the tag column looks tidier but rewrites the
    content: it turns a four-space code block into a nine-space one, and
    what is on screen is what a reader — human or agent — copies out. The
    text is not stripped either, so interior blank lines and leading
    indentation survive. This matters because ``ExecutionResult`` sets
    ``live_streamed`` once a message has streamed, which suppresses the
    result panel that would otherwise carry the pristine copy.

    :param text: agent message text.
    :param task: owning task name, prefixed when given.
    """
    t = Text()
    t.append(f"  {task} " if task else "  ", style="cyan")
    t.append("💬 ", style="white")
    lines = text.splitlines() or [""]
    t.append(lines[0])
    for line in lines[1:]:
        t.append(f"\n{line}")
    return t


def render_tool_burst_start(task: str = "") -> Text:
    """Render the marker opening a run of tool calls.

    :param task: owning task name, prefixed when given.
    """
    t = Text()
    t.append(f"  {task} " if task else "  ", style="cyan")
    t.append("🔧 ", style="dim")
    t.append("using tools…", style="dim")
    return t


def render_tool_burst_summary(counts: Counter[str], task: str = "") -> Text:
    """Render the tally closing a run of tool calls.

    One line per burst instead of one per call: an agent that makes forty
    tool calls in a row buries everything else on screen otherwise. The
    per-call detail is still recorded — see ``atelier logs --show steps``.

    :param counts: tool name to number of calls in this burst.
    :param task: owning task name, prefixed when given.
    """
    total = sum(counts.values())
    breakdown = ", ".join(
        f"{name} {n}" if n > 1 else name
        for name, n in counts.most_common()
    )
    t = Text()
    t.append(f"  {task} " if task else "  ", style="cyan")
    t.append(f"   used {total} tool{'s' if total != 1 else ''}", style="dim")
    if breakdown:
        t.append(f" ({breakdown})", style="dim")
    return t


def _render_orchestration_msg(text: str) -> Text:
    """Render an orchestration lifecycle message: ``· {text}`` in dim.

    :param text: the message body to display after the dot prefix.
    """
    t = Text()
    t.append(f"· {text}", style="dim")
    return t


def render_task_start(
    task_name: str, tool: str, index: int, total: int, verb: str = "running"
) -> Text:
    """Render the banner announcing a task entering the running state.

    Deliberately louder than :func:`_render_orchestration_msg`: this is the
    heading the step lines below it belong to, so it must not look like one
    of them. Includes ``[i/total]`` so a long flow shows how far along it is.

    :param task_name: name of the task starting.
    :param tool: tool identifier executing the task.
    :param index: 1-based position among the tasks started so far.
    :param total: total tasks in the conduit, or ``0`` when unknown.
    :param verb: leading verb, e.g. ``running`` or ``resuming``.
    """
    t = Text()
    t.append("▶ ", style="bold cyan")
    if total:
        t.append(f"[{index}/{total}] ", style="dim")
    t.append(task_name, style="bold")
    t.append(f"  [{tool}]", style="dim")
    if verb != "running":
        t.append(f"  {verb}", style="dim italic")
    return t


def render_heartbeat(elapsed_by_task: dict[str, float]) -> Text:
    """Render the "still working" line shown during output silence.

    A run can go quiet for minutes with nothing wrong: an ``npx`` cold
    start before the first step, a long-running tool call, or a
    ``tool:bash`` task that emits no steps at all. Naming the tasks and
    their elapsed time distinguishes "still working" from "hung".

    :param elapsed_by_task: running task name to seconds elapsed.
    :returns: a dim single-line status, e.g. ``· still working — build 2m 14s``.
    """
    t = Text()
    t.append("· still working", style="dim")
    if elapsed_by_task:
        parts = [
            f"{name} {_format_duration_seconds(seconds)}"
            for name, seconds in elapsed_by_task.items()
        ]
        t.append(f" — {' · '.join(parts)}", style="dim")
    return t


def _bounded(text: str) -> Text:
    """Return ``text`` as Rich :class:`Text`, capped at :data:`PANEL_MAX_CHARS`.

    Keeps the tail, which is where a failing task's diagnostic lives, and
    names both how much was dropped and where the whole thing still is. Real
    output is never near the cap; this only fires on a runaway producer.

    :param text: raw body text to render.
    :returns: the text, prefixed with a truncation notice when over the cap.
    """
    if len(text) <= PANEL_MAX_CHARS:
        return Text(text)
    body = Text()
    body.append(
        f"… ({len(text) - PANEL_MAX_CHARS} characters truncated — "
        f"full output in `atelier logs`)\n",
        style="dim italic",
    )
    body.append(text[-PANEL_MAX_CHARS:])
    return body


def _build_failure_body(stdout: str, stderr: str) -> Text:
    """Render a failure body that always surfaces stderr, in full.

    - Both empty → ``(empty)``.
    - Only one populated → that stream alone.
    - Both populated → labelled sections so the diagnostic stderr is
      visible alongside the stdout context.

    Nothing is elided short of :data:`PANEL_MAX_CHARS`. A failing task's
    output is the whole reason anyone is reading this panel, and the reader
    may be another agent that cannot go and fetch the dropped lines from the
    store.

    :param stdout: captured stdout text from the failed task.
    :param stderr: captured stderr text from the failed task.
    """
    has_stdout = bool(stdout)
    has_stderr = bool(stderr)
    if not has_stdout and not has_stderr:
        return Text("(empty)")
    if has_stdout and not has_stderr:
        return _bounded(stdout)
    if has_stderr and not has_stdout:
        return _bounded(stderr)
    body = Text()
    body.append("stdout:\n", style="dim bold")
    body.append(_bounded(stdout))
    body.append("\n\n")
    body.append("stderr:\n", style="bold red")
    body.append(_bounded(stderr))
    return body


def _step_summary_line(steps: list[IntermediateStep]) -> str | None:
    """Return a dim summary like ``thinking(3) tools(7)`` or None if empty.

    :param steps: intermediate steps to count by kind.
    """
    if not steps:
        return None
    thinking = sum(1 for s in steps if s.kind == StepKind.thinking)
    tools = sum(1 for s in steps if s.kind in (StepKind.tool_call, StepKind.tool_result))
    parts = []
    if thinking:
        parts.append(f"thinking({thinking})")
    if tools:
        parts.append(f"tools({tools})")
    return " ".join(parts) if parts else None


def _render_steps_timeline(steps: list[IntermediateStep]) -> Text:
    """Render each step as a compact timestamped line with short HH:MM times.

    Capped at :data:`TIMELINE_MAX_STEPS`, keeping the tail — an agent task can
    emit tens of thousands of steps, and every caller of this function renders
    into a terminal. The store keeps them all.

    :param steps: ordered intermediate steps to render as a timeline.
    """
    body = Text()
    if len(steps) > TIMELINE_MAX_STEPS:
        body.append(
            f"… ({len(steps) - TIMELINE_MAX_STEPS} earlier steps not shown)\n",
            style="dim italic",
        )
        steps = steps[-TIMELINE_MAX_STEPS:]
    for step in steps:
        if step.kind == StepKind.tool_result:
            # Tool results indent under the preceding tool call (no timestamp).
            body.append("       ", style="dim")
        else:
            ts = _format_clock_short(step.timestamp)
            body.append(f"{ts}  ", style="dim")
        body.append(_render_step(step))
        body.append("\n")
    return body


def render_task_event(event: TaskEvent, console: Console) -> None:
    """Pretty-print a :class:`TaskEvent` to ``console``.

    Success with non-empty output → green-bordered :class:`Panel`.
    Failure → red-bordered panel showing stdout *and* stderr when both
    are populated (stderr is the primary diagnostic and used to be
    hidden whenever stdout had any content).
    Success with empty output → compact single-line summary (no panel)
    to avoid visual noise for echo-style tasks.

    Bodies are printed in full. Task output is the product of the run and
    this stream is consumed by other agents as well as by people, so
    eliding it would silently drop the very thing the caller came for.
    Harness tasks whose output already streamed live are summarized to a
    single line instead, which avoids the duplication without losing data.

    :param event: the task event to render.
    :param console: Rich console to write the rendered output to.
    """
    iter_suffix = f" ({event.iteration}/{event.of})" if event.of > 1 else ""
    title_core = f"{event.task} [{event.tool}]{iter_suffix}"
    subtitle = f"exit={event.exit_code} · {event.duration_seconds}s"

    # Compact one-liners for non-running dispositions — these never had
    # a real iteration so a full panel of "(empty)" output is misleading.
    if event.status == TaskStatus.skipped:
        reason = f"  [dim italic]({event.reason})[/dim italic]" if event.reason else ""
        console.print(
            f"[yellow]⏭[/yellow] [bold]{event.task}[/bold] "
            f"[dim]\\[{event.tool}]{iter_suffix}[/dim]  "
            f"[yellow]skipped[/yellow]{reason}"
        )
        return
    if event.status == TaskStatus.cancelled:
        reason = f"  [dim italic]({event.reason})[/dim italic]" if event.reason else ""
        console.print(
            f"[red]⊘[/red] [bold]{event.task}[/bold] "
            f"[dim]\\[{event.tool}]{iter_suffix}[/dim]  "
            f"[red]cancelled[/red]{reason}"
        )
        return

    if event.success:
        border_style = "green"
        title = Text(f"✓ {title_core}", style="bold green")
        body_source = event.output
        # Compact single-line path: successful task with nothing to show.
        if not body_source.strip():
            console.print(
                f"[green]✓[/green] [bold]{event.task}[/bold] "
                f"[dim]\\[{event.tool}]{iter_suffix}[/dim]  "
                f"[dim]{subtitle}  (no output)[/dim]"
            )
            return
        # Interactive harness tasks already streamed their full
        # transcript live (incl. multi-turn user replies), so a body
        # panel here would just duplicate what scrolled by. Show a
        # compact single-line summary instead.
        if event.live_streamed:
            console.print(
                f"[green]✓[/green] [bold]{event.task}[/bold] "
                f"[dim]\\[{event.tool}]{iter_suffix}[/dim]  "
                f"[dim]{subtitle}  (streamed live above)[/dim]"
            )
            return
        summary = _step_summary_line(event.steps)
        body_text = Text()
        if summary:
            body_text.append(f"{summary}\n", style="dim")
        body_text.append(_bounded(body_source))
    else:
        border_style = "red"
        title = Text(f"✗ {title_core}", style="bold red")
        body_text = _build_failure_body(event.output or event.stdout, event.stderr)

    console.print(
        Panel(
            body_text,
            title=title,
            title_align="left",
            subtitle=subtitle,
            subtitle_align="right",
            border_style=border_style,
            padding=(0, 1),
        )
    )


_FLOW_STATUS_STYLE: dict[str, str] = {
    FlowStatus.completed.value: "green",
    FlowStatus.failed.value: "red",
    FlowStatus.running.value: "yellow",
    FlowStatus.stopped.value: "blue",
    "crashed": "magenta",
}

_TASK_STATUS_GLYPHS: list[tuple[TaskStatus, str, str]] = [
    (TaskStatus.completed, "✓", "green"),
    (TaskStatus.failed, "✗", "red"),
    (TaskStatus.skipped, "⏭", "yellow"),
    (TaskStatus.cancelled, "⊘", "red"),
    (TaskStatus.running, "⏳", "yellow"),
    (TaskStatus.pending, "·", "dim"),
]


def _task_status_summary(progress: Progress) -> Text:
    """Render `✓3 ✗1 ⏭2 ⊘0 ⏳1` with only non-zero entries.

    :param progress: flow progress whose task statuses get tallied.
    """
    counts: Counter[TaskStatus] = Counter(
        tp.status for tp in progress.tasks.values()
    )
    text = Text()
    first = True
    for status, glyph, style in _TASK_STATUS_GLYPHS:
        n = counts.get(status, 0)
        if n == 0:
            continue
        if not first:
            text.append("  ")
        text.append(f"{glyph}{n}", style=style)
        first = False
    if first:
        text.append("—", style="dim")
    return text


def render_run_footer(events: list[TaskEvent], console: Console) -> None:
    """One-line aggregate summary printed at the end of `atelier run`.

    :param events: task events collected during the run.
    :param console: Rich console to write the summary line to.
    """
    if not events:
        return
    counts: Counter[TaskStatus] = Counter(e.status for e in events)
    total_dur = sum(e.duration_seconds for e in events)
    parts: list[str] = []
    for status, glyph, style in _TASK_STATUS_GLYPHS:
        n = counts.get(status, 0)
        if n == 0:
            continue
        parts.append(f"[{style}]{glyph}{n}[/{style}]")
    summary = "  ".join(parts) if parts else "—"
    console.print(
        f"[dim]{summary}  ·  total {_format_duration_seconds(total_dur)}[/dim]"
    )


def _render_log_entry(entry, show: str, console: Console) -> None:
    """Render one LogEntry as a Rich Panel.

    ``show`` controls which body channel is displayed:
    ``output`` (default), ``stdout``, ``stderr``, or ``all`` (both
    labelled when present).

    :param entry: the log entry to render.
    :param show: which channel to display (output/stdout/stderr/steps/all).
    :param console: Rich console to write the panel to.
    """
    iter_suffix = f" ({entry.iteration}/{entry.of})" if entry.of > 1 else ""
    title_core = f"{entry.task} [{entry.tool}]{iter_suffix}"
    success = entry.exit_code == 0
    glyph = "✓" if success else "✗"
    border = "green" if success else "red"
    title = Text(f"{glyph} {title_core}", style=f"bold {border}")
    started = _format_clock(entry.started_at)
    subtitle = (
        f"{started}  ·  exit={entry.exit_code}  ·  "
        f"{entry.duration_seconds}s"
    )
    usage_line = _format_usage(getattr(entry, "usage", None))
    if usage_line:
        subtitle += f"  ·  {usage_line}"

    if show == "steps":
        steps = getattr(entry, "steps", [])
        body = _render_steps_timeline(steps) if steps else Text("(no steps)")
    elif show == "all":
        body = Text()
        steps = getattr(entry, "steps", [])
        if steps:
            body.append(_render_steps_timeline(steps))
            body.append("\n")
        if entry.stdout:
            body.append("stdout:\n", style="dim bold")
            body.append(entry.stdout)
            if entry.stderr:
                body.append("\n\n")
        if entry.stderr:
            body.append("stderr:\n", style="bold red")
            body.append(entry.stderr)
        if not steps and not entry.stdout and not entry.stderr:
            body = Text("(empty)")
    else:
        raw = {
            "output": entry.output,
            "stdout": entry.stdout,
            "stderr": entry.stderr,
        }[show]
        body = Text(raw or "(empty)")

    console.print(
        Panel(
            body,
            title=title,
            title_align="left",
            subtitle=subtitle,
            subtitle_align="right",
            border_style=border,
            padding=(0, 1),
        )
    )


def _format_error_loc(loc: tuple[object, ...]) -> str:
    """Render a pydantic error ``loc`` tuple as e.g. ``tasks[2].tool``.

    Integer elements become ``[i]`` (list indices); string elements become
    ``.key`` (field names). The leading dot is stripped.

    :param loc: the ``loc`` tuple from a pydantic error dict.
    """
    out = ""
    for part in loc:
        out += f"[{part}]" if isinstance(part, int) else f".{part}"
    return out.lstrip(".")


def format_conduit_error(exc: Exception) -> str:
    """Translate a conduit-load failure into one compact, plain-text line.

    Covers the failure modes ``read_conduit`` surfaces: pydantic
    ``ValidationError`` (rendered as ``field.path: message``, first error only),
    ``yaml.YAMLError`` (with a line number when the mark is available), and any
    other error — notably the ``ValueError`` raised for a wrapped YAML parse
    error or a conduit-name/folder mismatch. Returns plain text with no Rich
    markup; callers add color and escape it.

    :param exc: the exception raised while loading a conduit.
    """
    if isinstance(exc, ValidationError):
        errors = exc.errors()
        if not errors:
            return " ".join(str(exc).split())
        first = errors[0]
        path = _format_error_loc(tuple(first.get("loc", ())))
        msg = first.get("msg", "")
        text = f"{path}: {msg}" if path else msg
        if len(errors) > 1:
            text += f" (+{len(errors) - 1} more)"
        return " ".join(text.split())
    if isinstance(exc, yaml.YAMLError):
        problem = getattr(exc, "problem", None)
        mark = getattr(exc, "problem_mark", None)
        if problem and mark is not None:
            return f"invalid YAML: {problem} (line {mark.line + 1})"
    return " ".join(str(exc).split())


def _render_planned_task(task: PlannedTask, console: Console) -> None:
    """Render one task line plus its edges, loop badge and gate note.

    :param task: the planned task to render.
    :param console: Rich console to write to.
    """
    head = Text("  ")
    head.append(task.name, style="bold")
    head.append(f"  [{task.tool}]", style="dim")
    if task.is_loop and task.loop_text:
        head.append(f"  ↻ {task.loop_text}", style="magenta")
    if task.is_sink:
        head.append("  ⊙ sink", style="cyan")
    if task.is_gate:
        head.append("  ⎇ gate", style="yellow")
    console.print(head)

    for e in task.plain_edges:
        line = Text("      → ", style="dim")
        line.append(e.task)
        console.print(line)
    for e in task.conditional_edges:
        line = Text("      ⇢ ", style="yellow")
        line.append(e.task)
        marker = "not_match" if e.negate else "match"
        line.append(f"  ?{marker}({e.pattern})", style="yellow")
        console.print(line)

    if task.is_gate and task.prunes:
        note = Text("      ", style="dim")
        note.append(
            f"⚠ if this output misses, it prunes {len(task.prunes)} task(s): "
            f"{', '.join(task.prunes)}",
            style="dim yellow",
        )
        console.print(note)


def render_plan(plan: ExecutionPlan, console: Console) -> None:
    """Render a static :class:`ExecutionPlan` as grouped wave blocks.

    :param plan: the execution plan to render.
    :param console: Rich console to write to.
    """
    console.print(
        f"[bold]{plan.conduit_name}[/bold]  "
        f"[dim]max_concurrency={plan.max_concurrency}[/dim]"
    )
    console.print(
        "[dim italic]static structural view — wave levels are longest-path "
        "layering, not a runtime trace; real parallelism is also bounded by "
        "max_concurrency and conditional skips.[/dim italic]"
    )
    for i, wave in enumerate(plan.waves):
        console.print(f"\n[bold]Wave {i}[/bold]")
        for task in wave:
            _render_planned_task(task, console)


def _format_last_run(record) -> str:
    """Render a schedule's most recent fire outcome for the planned table.

    :param record: the last :class:`ScheduleRunRecord`, or ``None`` when the
        schedule has no recorded fires yet.
    :returns: a colored ``ok``/``FAILED`` marker plus the flow id, or ``—``.
    """
    if record is None:
        return "[dim]—[/dim]"
    marker = (
        "[green]ok[/green]"
        if record.status == "succeeded"
        else "[red]FAILED[/red]"
    )
    if record.flow_id:
        return f"{marker} [dim]{record.flow_id}[/dim]"
    return marker


def render_planned_table(planned: list[PlannedJob]) -> Table:
    """Render planned scheduler jobs as a Rich table.

    :param planned: planned jobs with computed next-fire times.
    """
    table = Table(
        "id", "name", "conduit", "kind", "next fire", "last run", "working_dir"
    )
    for p in planned:
        kind_style = "magenta" if p.schedule_kind == "once" else "cyan"
        next_cell = _format_next_fire(p.next_fire_time)
        if p.next_fire_time is None and p.schedule_kind == "once":
            next_cell = "[dim](already fired)[/dim]"
        table.add_row(
            escape(p.id),
            escape(p.name),
            escape(p.conduit_name),
            f"[{kind_style}]{p.schedule_kind}[/{kind_style}]",
            next_cell,
            _format_last_run(p.last_run),
            escape(str(p.working_dir)),
        )
    return table
