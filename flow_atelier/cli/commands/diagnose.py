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
            if task.kind == "agent_session":
                console.print(
                    "  [yellow]the agent session failed before the agent produced "
                    "any task output — check that it is installed, logged in and "
                    "allowed the model[/yellow]"
                )
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
            table.add_row(
                escape(task.task),
                _agent_text(task),
                f"{task.output_chars} chars" if task.output_saved else "[dim]none[/dim]",
            )
        console.print(table)
        console.print(
            "[dim]a resume does not run a completed task again; it replays this "
            "saved result[/dim]"
        )

    if report.not_run:
        console.print("\n[bold]did not run[/bold]")
        for task in report.not_run:
            why = f" — {escape(task.reason)}" if task.reason else ""
            console.print(f"  {escape(task.task)} [dim]({task.status})[/dim]{why}")
        console.print(
            "[dim]a cancelled or pending task is not a failure: it never got to "
            "run[/dim]"
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
            console.print(f"    [cyan]{escape(step.command)}[/cyan]")
        if step.side_effects:
            console.print(f"    [dim]{escape(step.side_effects)}[/dim]")
