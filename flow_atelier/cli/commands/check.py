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
from flow_atelier.modules.engine import (
    MAX_NESTED_CONDUIT_DEPTH,
    ConduitValidationError,
    validate_conduit,
)
from flow_atelier.schemas.conduit import Conduit, ToolType

# Errors the per-conduit job can legitimately produce; a programming error
# still crashes rather than becoming a polite failed row.
_EXPECTED = (ValidationError, OSError, UnicodeDecodeError, ConduitValidationError, ValueError)


class _NestedProblem(Exception):
    """A failure found below the root, already formatted with its call chain."""


def _diagnostic(e: Exception, where: str) -> str:
    """Render one expected failure as the text a conduit author has to act on.

    :param e: the caught failure.
    :param where: file path (or name) to blame for an I/O failure.
    :returns: a non-empty one-line-or-more diagnostic.
    """
    if isinstance(e, ValidationError):
        errors = e.errors()
        # A failure always carries text: `or type(e).__name__` is the last
        # resort, because a blank diagnostic reads like a checker bug.
        return (errors[0].get("msg") if errors else None) or str(e) or "ValidationError"
    if isinstance(e, OSError | UnicodeDecodeError):
        return f"cannot read {where} — {e}"
    return str(e) or type(e).__name__


def _chain(hops: list[tuple[str, str]], target: str | None = None) -> str:
    """Render a call chain as ``parent.task -> child.task -> grandchild``.

    :param hops: ``(conduit, calling task)`` pairs from the root downwards.
    :param target: the called conduit name to append, when there is one.
    :returns: the chain as one readable arrow-separated string.
    """
    text = " -> ".join(f"{conduit}.{task}" for conduit, task in hops)
    return f"{text} -> {target}" if target is not None else text


def _load_child(
    atelier: Atelier, name: str, chain: str, cache: dict[str, Conduit]
) -> Conduit:
    """Resolve, load and fully validate one called conduit.

    :param atelier: configured :class:`Atelier` providing the store.
    :param name: the called conduit's name.
    :param chain: the call chain that reached it, for the diagnostic.
    :param cache: invocation-local name -> validated conduit cache.
    :returns: the loaded child conduit.
    :raises _NestedProblem: it is missing, unreadable, invalid or unrunnable.
    """
    if name in cache:
        return cache[name]
    path: str | None = None
    try:
        path = str((atelier.store.conduit_dir(name) / "conduit.yaml").absolute())
        child = atelier.store.read_conduit(name)
        validate_conduit(child)
        problems = atelier.tool_readiness(child)
    except FileNotFoundError as e:
        # Name the caller and the missing name; there is no source file to
        # point at, and guessing where it "should" live would be a fiction.
        raise _NestedProblem(f"{chain} — conduit not found") from e
    except _EXPECTED as e:
        where = f" ({path})" if path else ""
        raise _NestedProblem(f"{chain}{where} — {_diagnostic(e, path or name)}") from e
    if problems:
        raise _NestedProblem(f"{chain} ({path}) — {'; '.join(problems)}")
    cache[name] = child
    return child


def _walk_calls(
    atelier: Atelier,
    conduit: Conduit,
    level: int,
    ancestors: frozenset[str],
    hops: list[tuple[str, str]],
    cache: dict[str, Conduit],
    seen: set[tuple[str, int]],
) -> None:
    """Follow ``conduit``'s ``tool:conduit`` calls depth-first, in order.

    Conservative by design: a call is inspected even when a runtime condition
    might skip it, and a templated target is refused rather than guessed.

    ``seen`` is keyed by ``(name, level)``, not by name alone, so a child
    shared by a short and a long path is still walked on the long one — the
    cache must never hide a chain that would break the engine's depth guard.
    A repeated call is not a cycle; only the live ancestry is.

    :param atelier: configured :class:`Atelier` providing the store.
    :param conduit: an already-validated conduit whose calls to follow.
    :param level: ``conduit``'s nesting level; 0 for the selected root.
    :param ancestors: conduit names on the call stack, including ``conduit``.
    :param hops: ``(conduit, calling task)`` pairs above ``conduit``.
    :param cache: invocation-local name -> validated conduit cache.
    :param seen: ``(name, level)`` pairs already walked for this root.
    :raises _NestedProblem: the first failure reachable from ``conduit``.
    """
    for task in conduit.tasks:
        if task.tool != ToolType.conduit:
            continue
        # The executor strips the resolved target, so the same name that
        # would run is the one checked here.
        target = task.task.strip()
        here = [*hops, (conduit.name, task.name)]
        if "{{" in target:
            raise _NestedProblem(
                f"{_chain(here)} — cannot recursively check the dynamic conduit "
                f"target {target!r}; check its resolved target separately"
            )
        chain = _chain(here, target)
        if target in ancestors:
            raise _NestedProblem(f"{chain} — nested conduit cycle detected")
        if level + 1 >= MAX_NESTED_CONDUIT_DEPTH:
            raise _NestedProblem(
                f"{chain} — nested conduit depth exceeded {MAX_NESTED_CONDUIT_DEPTH}"
            )
        if (target, level + 1) in seen:
            continue
        seen.add((target, level + 1))
        child = _load_child(atelier, target, chain, cache)
        _walk_calls(
            atelier, child, level + 1, ancestors | {target}, here, cache, seen
        )


def _check_one(
    atelier: Atelier,
    name: str,
    source: str,
    recursive: bool = False,
    cache: dict[str, Conduit] | None = None,
) -> dict[str, Any]:
    """Validate one conduit fully without executing it.

    Combines structural validation with a readiness probe: even a
    structurally-valid conduit FAILs here if a referenced tool is unregistered
    or its harness CLI is missing from PATH, so authors learn before a run.

    The whole per-conduit job lives inside one error boundary — resolving the
    path, reading the file, loading the model, validating the DAG, probing
    the tools and, with ``recursive``, doing all of that for every conduit
    this one calls — so one unreadable conduit becomes a failed row rather
    than aborting the conduits queued after it. Only the failures this work
    can legitimately produce are caught; a programming error still crashes.

    :param atelier: configured :class:`Atelier` providing the store.
    :param name: conduit name to load and validate.
    :param source: ``project`` or ``global``, as reported by the store.
    :param recursive: also follow this conduit's ``tool:conduit`` calls.
    :param cache: invocation-local child cache shared across a batch.
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
            if recursive:
                _walk_calls(
                    atelier, conduit, 0, frozenset({name}), [],
                    {} if cache is None else cache, set(),
                )
            # Child inputs are the parent's business at runtime, not extra
            # `--input` keys: the root's own declarations are what a caller
            # has to supply.
            required = sorted(
                key for key, spec in conduit.inputs.items() if spec.default is None
            )
    except _NestedProblem as e:
        error = str(e)
    except _EXPECTED as e:
        error = _diagnostic(e, path or name)
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
    recursive: bool = typer.Option(
        False,
        "--recursive",
        help="Also check every conduit reachable through tool:conduit calls.",
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

    `--recursive` also follows `tool:conduit` steps, so a missing, invalid or
    unrunnable child fails the parent here instead of mid-run. It reports one
    row per selected conduit still; a nested failure names the calling chain
    and the child's file inside the parent's `error`. It is a static check of
    definitions: a call is inspected even if a condition would skip it, a
    templated target cannot be followed and fails the check, and passing
    proves nothing about input values or runtime success.

    :param conduit_name: a single conduit to check; when omitted, all
        project and global conduits are checked.
    :param json_mode: when true, emit machine-readable JSON instead of the
        labelled terminal report.
    :param recursive: when true, also validate conduits called with
        ``tool:conduit``, detecting missing children, call cycles and
        excessive nesting.
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

    cache: dict[str, Conduit] = {}
    rows = [
        _check_one(atelier, name, source, recursive, cache) for name, source in targets
    ]

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
