"""`atelier diagnose` command — read a finished or broken run back."""
from __future__ import annotations

import json

import typer
from rich.markup import escape
from rich.table import Table

from flow_atelier.cli._shared import (
    _format_duration_seconds,
    _resolve_flow_id,
    console,
    err_console,
)
from flow_atelier.cli.main import app
from flow_atelier.core.atelier import Atelier
from flow_atelier.modules.diagnose import (
    DiagnoseError,
    DiagnoseReport,
    TaskReport,
    build_report,
)

_STATE_STYLE = {
    "failed": "red",
    "crashed": "red",
    "running": "yellow",
    "stopped": "yellow",
    "completed": "green",
}

_EVIDENCE_TEXT = {
    "log": "log entry",
    # Never "no evidence": a killed task's live steps are the only record of
    # what it was doing, and saying nothing was recorded would throw it away.
    "steps": "live steps only (no completed log entry)",
    "reason": "saved reason only (no log entry, no live steps)",
    "none": "status only — nothing else was saved for it",
}

# What placing a failure means for the reader, and only where the run's own
# records place it. "unclear_stage" says so instead of naming a stage.
_KIND_TEXT = {
    "agent_session": (
        "the agent refused to open a session — its own words are in the tail "
        "below. Check it is installed, logged in and allowed the model; no work "
        "was asked of it"
    ),
    "agent_auth": (
        "the agent reported an authentication problem — its own words are in "
        "the tail below. Check it is installed, logged in and allowed the model. "
        "What was saved does not show it failed before starting work, so treat "
        "any change it could have made as unknown"
    ),
    "before_prompt": (
        "the agent failed before it was sent its prompt — the reason is in the "
        "tail below — so no work was asked of it. Fix that cause (install, "
        "login, model or effort), then resume"
    ),
    "timeout": (
        "this task ran out of its own time limit and was cut off part-way, so "
        "anything it had already changed on disk stays changed"
    ),
    "unclear_stage": (
        "the agent saved no output and no live step, so what was saved does not "
        "place this failure: whether the session ever opened, or the work began "
        "and was cut off, cannot be settled from here"
    ),
}

# Whether a cut-off task executed. Only its own saved records can say; where
# they cannot, the report says that rather than assuming it never started.
_EXECUTION_TEXT = {
    "began": (
        "it was executing when the run failed — anything it already changed on "
        "disk stays changed"
    ),
    "unknown": (
        "nothing saved says whether it had started, so treat any change it "
        "could have made as unknown"
    ),
    "not_started": "the run never reached it",
}


@app.command("diagnose")
def diagnose_cmd(
    flow_id: str = typer.Argument(
        ..., help="Flow id to diagnose (unique prefix or 'latest' ok)."
    ),
    json_mode: bool = typer.Option(
        False, "--json", help="Emit the whole report as one JSON object."
    ),
) -> None:
    """Explain what a run did, what it kept, and what to do next.

    Reads only what the run saved: nothing is written, no agent or task is
    started, and every printed command is a suggestion for you to run. A
    successful reading exits 0 even when the run it describes failed.

    :param flow_id: flow id (or unique prefix) to diagnose.
    :param json_mode: when true, emit one machine-readable JSON object.
    """
    out = err_console if json_mode else console
    atelier = Atelier()
    flow_id = _resolve_flow_id(atelier, flow_id)
    try:
        report = build_report(atelier, flow_id)
    except DiagnoseError as exc:
        out.print(f"[red]{escape(str(exc))}[/red]")
        if exc.hint:
            # Unwrapped: a command broken across two lines cannot be copied.
            out.print(f"[cyan]{escape(exc.hint)}[/cyan]", soft_wrap=True)
        raise typer.Exit(code=1) from exc

    if json_mode:
        typer.echo(json.dumps(report.model_dump(mode="json"), indent=2))
        return
    _render(report)


def _agent_text(task: TaskReport) -> str:
    """Render one task's tool attribution with where it was read from.

    :param task: the per-task report.
    :returns: the display string, never silently claiming the current recipe.
    """
    value, source = task.ran_on.value, task.ran_on.source
    if value is None:
        text = "[dim]unknown — nothing saved records what ran it[/dim]"
    elif source == "log":
        text = f"{escape(value)} [dim](from its log entry: what ran)[/dim]"
    else:
        text = f"{escape(value)} [dim](from this run's saved choice)[/dim]"
    if task.current_tool and task.current_tool != value:
        text += f"  [dim]· now {escape(task.current_tool)} in the recipe[/dim]"
    return text


def _render(report: DiagnoseReport) -> None:
    """Print the report as prose and tables, evidence before advice.

    :param report: the built report.
    """
    state = report.observed.state
    style = _STATE_STYLE.get(state, "white")
    console.print(
        f"[bold]flow[/bold] {report.flow_id}  "
        f"[dim]conduit[/dim] {escape(report.conduit)}\n"
        f"saved status=[{style}]{escape(report.saved.status)}[/{style}]  "
        f"observed=[{style}]{escape(state)}[/{style}] ({report.observed.certainty})  "
        f"duration={_format_duration_seconds(report.saved.duration_seconds)}"
    )
    console.print(f"[dim]{escape(report.observed.note)}[/dim]")
    if report.saved.run_path:
        console.print(f"[dim]ran in {escape(report.saved.run_path)}[/dim]")
    if report.saved.invoking_task:
        console.print(
            f"[dim]nested: this flow was started by task "
            f"{escape(report.saved.invoking_task)} of a parent run[/dim]"
        )
    if not report.snapshot.consistent:
        console.print(f"[yellow]{escape(report.snapshot.note)}[/yellow]")

    if report.failures:
        console.print("\n[bold red]what failed[/bold red]")
        for task in report.failures:
            head = f"[bold]{escape(task.task)}[/bold]"
            if task.exit_code is not None:
                head += f"  exit={task.exit_code}"
            console.print(head)
            console.print(f"  agent: {_agent_text(task)}")
            if task.kind in _KIND_TEXT:
                console.print(f"  [yellow]{_KIND_TEXT[task.kind]}[/yellow]")
            if task.reason:
                console.print(f"  reason: {escape(task.reason)}")
            console.print(f"  evidence: {_EVIDENCE_TEXT[task.evidence]}")
            if task.step_records:
                console.print(f"  live steps recorded: {task.step_records}")
            if task.excerpt is not None:
                suffix = " [dim](truncated — see the log command below)[/dim]" if (
                    task.excerpt.truncated
                ) else ""
                console.print(f"  {task.excerpt.channel} tail:{suffix}")
                for line in task.excerpt.text.splitlines():
                    console.print(f"    [dim]{escape(line)}[/dim]")
    else:
        console.print("\n[dim]no task recorded a failure[/dim]")

    if report.running:
        console.print("\n[bold yellow]still recorded as running[/bold yellow]")
        for task in report.running:
            console.print(
                f"  {escape(task.task)}  agent: {_agent_text(task)}  "
                f"evidence: {_EVIDENCE_TEXT[task.evidence]}"
            )

    if report.kept:
        console.print("\n[bold green]kept — completed and saved[/bold green]")
        table = Table("task", "agent", "saved result")
        for task in report.kept:
            if task.output_saved is None:
                result = "[yellow]unknown (unreadable)[/yellow]"
            elif task.output_saved:
                result = f"{task.output_chars} chars"
            else:
                result = "[dim]none[/dim]"
            table.add_row(escape(task.task), _agent_text(task), result)
        console.print(table)
        console.print(
            "[dim]a resume does not run a completed task again; it replays this "
            "saved result[/dim]"
        )

    if report.cancelled:
        stopped = report.saved.status == "stopped"
        console.print(
            f"\n[bold]cut off when the run {'stopped' if stopped else 'failed'}[/bold]"
        )
        for task in report.cancelled:
            console.print(f"  {escape(task.task)} [dim](cancelled)[/dim]")
            console.print(f"    {_EXECUTION_TEXT[task.execution or 'unknown']}")
            if task.reason:
                console.print(f"    reason: {escape(task.reason)}")
            if task.step_records:
                console.print(f"    live steps recorded: {task.step_records}")
        stopped_by = "the run was stopped" if stopped else "another task failed"
        console.print(
            "[dim]a cancelled task is not the failure — it was stopped because "
            f"{stopped_by}[/dim]"
        )

    if report.not_run:
        console.print("\n[bold]did not run[/bold]")
        for task in report.not_run:
            why = f" — {escape(task.reason)}" if task.reason else ""
            console.print(f"  {escape(task.task)} [dim]({task.status})[/dim]{why}")
        console.print(
            "[dim]a skipped or pending task is not a failure: the run never "
            "reached it[/dim]"
        )

    if report.children:
        console.print("\n[bold]nested flows this run started[/bold]")
        for child in report.children:
            called_by = f" (task {escape(child.invoking_task)})" if child.invoking_task else ""
            status = escape(child.status) if child.status else "unknown"
            console.print(f"  {child.flow_id}{called_by}  status={status}")
        console.print(
            "[dim]diagnose each one on its own id for its own detail; this report "
            "does not aggregate them[/dim]"
        )

    console.print(f"\n[bold]the recipe now[/bold]  {escape(report.recipe.note)}")
    if report.recipe.added_tasks:
        console.print(
            f"  [yellow]tasks in the file this run never recorded: "
            f"{escape(', '.join(report.recipe.added_tasks))}[/yellow]"
        )
    if report.recipe.missing_tasks:
        console.print(
            f"  [yellow]tasks this run recorded that the file no longer has: "
            f"{escape(', '.join(report.recipe.missing_tasks))}[/yellow]"
        )

    if report.unavailable:
        console.print("\n[bold]not available[/bold]")
        for note in report.unavailable:
            console.print(f"  [dim]{escape(note)}[/dim]")

    console.print("\n[bold]what you can do[/bold] [dim](diagnose runs none of these)[/dim]")
    for step in report.next_steps:
        console.print(f"  {escape(step.what)}")
        if step.command:
            # Unwrapped, like the error hint: a command split across two lines
            # cannot be copied, and every one of these carries a full flow id.
            console.print(f"    [cyan]{escape(step.command)}[/cyan]", soft_wrap=True)
        if step.side_effects:
            console.print(f"    [dim]{escape(step.side_effects)}[/dim]")
