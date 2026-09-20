"""`atelier check` command — validate conduits without running them."""
from __future__ import annotations

import json
from typing import Any

import typer
from pydantic import ValidationError
from rich.markup import escape

from flow_atelier.cli._shared import _exit_unknown_conduit, console, err_console
from flow_atelier.cli.main import app
from flow_atelier.core.atelier import Atelier
from flow_atelier.modules.engine import ConduitValidationError, validate_conduit


def _check_one(atelier: Atelier, name: str, source: str) -> dict[str, Any]:
    """Validate one conduit fully without executing it.

    Combines structural validation with a readiness probe: even a
    structurally-valid conduit FAILs here if a referenced tool is unregistered
    or its harness CLI is missing from PATH, so authors learn before a run.

    The whole per-conduit job lives inside one error boundary — resolving the
    path, reading the file, loading the model, validating the DAG and probing
    the tools — so one unreadable conduit becomes a failed row rather than
    aborting the conduits queued after it. Only the failures this work can
    legitimately produce are caught; a programming error still crashes.

    :param atelier: configured :class:`Atelier` providing the store.
    :param name: conduit name to load and validate.
    :param source: ``project`` or ``global``, as reported by the store.
    :returns: one result row, the same shape ``--json`` emits.
    """
    path: str | None = None
    error: str | None = None
    required: list[str] | None = None
    try:
        path = str((atelier.store.conduit_dir(name) / "conduit.yaml").absolute())
        conduit = atelier.store.read_conduit(name)
        validate_conduit(conduit)
        problems = atelier.tool_readiness(conduit)
        if problems:
            error = "; ".join(problems)
        else:
            required = sorted(
                key for key, spec in conduit.inputs.items() if spec.default is None
            )
    except ValidationError as e:
        errors = e.errors()
        # A failure always carries text: `or type(e).__name__` is the last
        # resort, because a blank diagnostic reads like a checker bug.
        error = (errors[0].get("msg") if errors else None) or str(e) or "ValidationError"
    except (OSError, UnicodeDecodeError) as e:
        error = f"cannot read {path or name} — {e}"
    except (ConduitValidationError, ValueError) as e:
        error = str(e) or type(e).__name__
    return {
        "name": name,
        "source": source,
        "path": path,
        "ok": error is None,
        "error": error,
        "required_inputs": required,
    }


@app.command("check")
def check_cmd(
    conduit_name: str = typer.Argument(
        None, help="Conduit to check; omit to check all."
    ),
    json_mode: bool = typer.Option(
        False, "--json", help="Emit one JSON result row per checked conduit."
    ),
) -> None:
    """Validate hand-authored conduits without running any task.

    `--json` writes one array to stdout — `name`, `source`, `path`, `ok`,
    `error` and `required_inputs` per conduit — so a script or a coding agent
    can find the file to repair instead of reading terminal text. Every
    selected conduit is reported even when an earlier one is broken, and a
    broken conduit is still a result, not a crash: exit 1 with a parseable
    report. Check the exit status *and* parse stdout. A failure before any
    conduit can be selected (an unknown name, an unreadable store) leaves
    stdout empty and explains itself on stderr.

    :param conduit_name: a single conduit to check; when omitted, all
        project and global conduits are checked.
    :param json_mode: when true, emit machine-readable JSON instead of the
        labelled terminal report.
    """
    out = err_console if json_mode else console
    try:
        atelier = Atelier()
        entries = atelier.store.list_conduits_with_source()
    except OSError as e:
        # No target set could be established, so there is nothing to report:
        # an empty `[]` here would read as "checked everything, all fine".
        err_console.print(f"[red]FAIL: cannot list conduits — {escape(str(e))}[/red]")
        raise typer.Exit(code=1) from e

    if conduit_name is not None:
        sources = dict(entries)
        if conduit_name not in sources:
            _exit_unknown_conduit(conduit_name, list(sources), out=out)
        targets = [(conduit_name, sources[conduit_name])]
    else:
        targets = entries

    rows = [_check_one(atelier, name, source) for name, source in targets]

    if json_mode:
        typer.echo(json.dumps(rows, indent=2))
    elif not rows:
        console.print("[yellow]no conduits found[/yellow]")
    else:
        for row in rows:
            label = rf"{escape(row['name'])} \[{escape(row['source'])}]"
            if row["ok"]:
                console.print(f"{label} — [green]OK[/green]")
                if row["required_inputs"]:
                    keys = ", ".join(escape(k) for k in row["required_inputs"])
                    console.print(f"    [dim]requires --input: {keys}[/dim]")
            else:
                console.print(f"{label} — [red]FAIL: {escape(row['error'])}[/red]")

    if any(not row["ok"] for row in rows):
        raise typer.Exit(code=1)
