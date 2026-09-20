"""`atelier schema` command — export the conduit authoring schema."""
from __future__ import annotations

import json

import typer

from flow_atelier.cli.main import app
from flow_atelier.schemas.authoring import conduit_json_schema


@app.command("schema")
def schema_cmd() -> None:
    """Print the JSON Schema of a conduit.yaml, as this version reads it.

    Save it beside your workflows and point your editor at it to get field
    completion and structural errors while you type:

        atelier schema > .atelier/conduit.schema.json

    then make `# yaml-language-server: $schema=../../conduit.schema.json`
    the first line of a conduit. A coding agent can read the output
    directly instead. Regenerate it after upgrading Atelier.

    Structure only: it catches a task with no `tool`, a `tasks:` that is
    not a list or `max_concurrency: 0`. It does not catch a dependency on
    a task that does not exist, a loop predicate that will not parse or an
    agent you have not installed — `atelier check <name>` still owns those.

    Reads nothing and writes nothing: no conduit, store or agent needed.
    """
    typer.echo(json.dumps(conduit_json_schema(), indent=2))
