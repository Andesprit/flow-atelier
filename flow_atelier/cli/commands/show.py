"""`atelier show` command — read an installed conduit without running it."""
from __future__ import annotations

import json

import typer
from pydantic import ValidationError
from rich.markup import escape

from flow_atelier.cli._shared import _exit_unknown_conduit, err_console
from flow_atelier.cli.main import app
from flow_atelier.cli.rendering.render import format_conduit_error
from flow_atelier.core.atelier import Atelier
from flow_atelier.modules.engine import accepted_input_keys


def _exit_unreadable(message: str) -> None:
    """Report a load failure on stderr and exit 1, leaving stdout empty.

    :param message: plain-text diagnostic; Rich markup in it is escaped.
    """
    err_console.print(f"[red]FAIL: {escape(message)}[/red]")
    raise typer.Exit(code=1)


@app.command("show")
def show_cmd(
    conduit_name: str = typer.Argument(..., help="Conduit to show."),
    json_mode: bool = typer.Option(
        False, "--json", help="Emit the normalized definition and input names as JSON."
    ),
) -> None:
    """Print a conduit's definition without running any task.

    By default writes the exact `conduit.yaml` of the copy that would run —
    comments, templates and multiline prompts untouched — to stdout, and its
    source and path to stderr, so the output stays pipeable. A project
    conduit shadows a global one of the same name here exactly as it does at
    run time, even when the project copy is the broken one.

    `--json` emits one object instead: `source`, `path`, the normalized
    `conduit`, and the input names it accepts and requires. Those are what
    the conduit declares, not a promise that every template resolves —
    `atelier check` still owns validation and readiness.

    Read-only: nothing runs, no harness is probed, no flow is recorded.

    :param conduit_name: the conduit to inspect.
    :param json_mode: when true, emit machine-readable JSON instead of the
        raw source text.
    """
    atelier = Atelier()
    sources = dict(atelier.store.list_conduits_with_source())
    if conduit_name not in sources:
        _exit_unknown_conduit(conduit_name, list(sources), out=err_console)

    path = (atelier.store.conduit_dir(conduit_name) / "conduit.yaml").absolute()

    if not json_mode:
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as e:
            _exit_unreadable(f"cannot read {path} — {e}")
        typer.echo(f"{sources[conduit_name]}: {path}", err=True)
        # Written verbatim: no Rich markup, wrapping or pager, so `[a]` and
        # `{{inputs.x}}` survive and the bytes can be redirected to a file.
        typer.echo(text, nl=not text.endswith("\n"))
        return

    try:
        conduit = atelier.store.read_conduit(conduit_name)
    except (OSError, UnicodeDecodeError) as e:
        _exit_unreadable(f"cannot read {path} — {e}")
    except (ValidationError, ValueError) as e:
        _exit_unreadable(format_conduit_error(e))

    typer.echo(
        json.dumps(
            {
                "source": sources[conduit_name],
                "path": str(path),
                "conduit": conduit.model_dump(mode="json", by_alias=True),
                "accepted_inputs": sorted(accepted_input_keys(conduit)),
                "required_inputs": sorted(
                    key for key, spec in conduit.inputs.items() if spec.default is None
                ),
            },
            indent=2,
        )
    )
