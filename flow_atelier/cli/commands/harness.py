"""`atelier harness` sub-app and `atelier list harnesses` — check, refresh and list ACP agents.

Nothing here logs into an agent; that stays with the user and the agent's
own CLI. ``install`` only puts an npx adapter the registry already pins
where launches can run it without ``npx``; the rest report what is
available and what is missing.
"""
from __future__ import annotations

import asyncio
import json
import re
import shlex
from urllib.error import URLError

import typer
from rich.markup import escape
from rich.table import Table

from flow_atelier.cli._shared import console
from flow_atelier.cli.main import harness_app, list_app
from flow_atelier.core.atelier import Atelier
from flow_atelier.schemas.conduit import split_harness_tool
from flow_atelier.schemas.harness import HARNESS_TOOL_PATTERN
from flow_atelier.services.executor.acp_registry import (
    INSTALL_DIRNAME,
    LEGACY_HARNESS_ALIASES,
    REGISTRY_URL,
    SNAPSHOT_FILENAME,
    fetch_registry,
    install_agent,
    load_registry,
    write_snapshot,
)
from flow_atelier.services.executor.harness import (
    PROBE_TIMEOUT_SECONDS,
    AcpHarnessExecutor,
)


@list_app.command("harnesses")
@harness_app.command("list", hidden=True)
def harness_list_cmd(
    ready_only: bool = typer.Option(
        False, "--ready", help="Only show harnesses that can run on this machine."
    ),
    json_mode: bool = typer.Option(
        False, "--json", help="Emit machine-readable JSON instead of a table."
    ),
) -> None:
    """List every ACP agent available as a `harness:<name>` tool.

    :param ready_only: when true, hide harnesses whose CLI is missing.
    :param json_mode: when true, emit machine-readable JSON instead of a table.
    """
    atelier = Atelier()
    rows: list[dict[str, object]] = []
    for tool, executor in sorted(atelier.executors.items()):
        if not tool.startswith("harness:"):
            continue
        name = tool.removeprefix("harness:")
        # A legacy alias carries no registry entry of its own; describe it
        # with the entry it points at rather than leaving the row blank.
        spec = atelier.registry.get(name) or atelier.registry.get(
            LEGACY_HARNESS_ALIASES.get(name, "")
        )
        ready, reason = executor.is_available()
        if ready_only and not ready:
            continue
        rows.append(
            {
                "tool": tool,
                "agent": spec.name if spec else "",
                "version": spec.version if spec else "",
                "via": spec.kind if spec else "custom",
                "launch": " ".join(executor.launch_cmd),
                "ready": ready,
                "reason": reason,
            }
        )

    if json_mode:
        console.print_json(json.dumps(rows))
        return

    table = Table(title="ACP harnesses", title_justify="left")
    table.add_column("tool", no_wrap=True)
    table.add_column("agent")
    table.add_column("version")
    # The full argv is in --json; here just name the agent's own
    # distribution: npx/uvx fetch themselves on first run, installed runs
    # the copy `atelier harness install` made, binary means the user
    # installed the CLI and we run it from PATH.
    table.add_column("via")
    table.add_column("ready", overflow="fold")
    for row in rows:
        ready = (
            "[green]yes[/green]"
            if row["ready"]
            else f"[dim]{escape(str(row['reason']))}[/dim]"
        )
        table.add_row(
            escape(str(row["tool"])),
            escape(str(row["agent"])),
            escape(str(row["version"])),
            escape(str(row["via"])),
            ready,
        )
    console.print(table)
    console.print(
        "[dim]use one as 'tool: <name>' in a conduit; "
        "'atelier harness sync' refreshes this list[/dim]"
    )


def _render_probe(tool: str, launch: list[str], result) -> None:
    """Print one connection-check report.

    :param tool: the harness tool name (or ``ad-hoc`` for a ``--cmd`` check).
    :param launch: the argv that was checked.
    :param result: the :class:`ProbeResult` to render.
    """
    console.print(f"[bold]{escape(tool)}[/bold]  [dim]{escape(' '.join(launch))}[/dim]")
    if result.ok:
        console.print(f"  [green]ok[/green] — {escape(result.detail)}")
        if result.agent:
            console.print(f"  [dim]agent:[/dim] {escape(result.agent)}")
        console.print(f"  [dim]protocol:[/dim] ACP v{result.protocol_version}")
        if result.modes:
            picked = f" (runs will use {result.permissive_mode})" if result.permissive_mode else ""
            console.print(
                f"  [dim]modes:[/dim] {escape(', '.join(result.modes))}{escape(picked)}"
            )
        if result.models:
            current = f" (current {result.current_model})" if result.current_model else ""
            console.print(
                f"  [dim]models:[/dim] {escape(', '.join(result.models))}{escape(current)}"
            )
            if tool.startswith("harness:") and tool.count(":") == 1:
                console.print(f"  [dim]pick one with[/dim] tool: {escape(tool)}:<model>")
        if result.efforts:
            current = f" (current {result.current_effort})" if result.current_effort else ""
            # Effort choices belong to the model in force, so say which one.
            model = f" for {result.current_model}" if result.current_model else ""
            console.print(
                f"  [dim]efforts{escape(model)}:[/dim] "
                f"{escape(', '.join(result.efforts))}{escape(current)}"
            )
            if tool.startswith("harness:") and tool.count(":") == 2:
                console.print(f"  [dim]pick one with[/dim] tool: {escape(tool)}:<effort>")
        if result.auth_methods:
            console.print(
                f"  [dim]auth methods advertised:[/dim] "
                f"{escape(', '.join(result.auth_methods))}"
            )
        if result.reply:
            console.print(f"  [dim]reply:[/dim] {escape(result.reply[:200])}")
        return

    console.print(f"  [red]not usable[/red] — {escape(result.detail)}")
    # Say what the user has to do, and be explicit that it is theirs to do:
    # flow-atelier never installs an agent and never logs one in.
    for line in result.guidance():
        console.print(f"  [yellow]{escape(line)}[/yellow]")
    if result.stderr:
        console.print("  [dim]agent stderr:[/dim]")
        for line in result.stderr.splitlines()[-10:]:
            console.print(f"    [dim]{escape(line)}[/dim]")


@harness_app.command("check")
def harness_check_cmd(
    name: str = typer.Argument(
        None,
        help=(
            "Harness to check, e.g. gemini, harness:gemini, or codex:<model> "
            "to see the reasoning efforts that model offers."
        ),
    ),
    cmd: str = typer.Option(
        None, "--cmd", help="Check an arbitrary agent command instead of a name."
    ),
    timeout: float = typer.Option(
        PROBE_TIMEOUT_SECONDS, "--timeout", help="Seconds to allow for the check."
    ),
    prompt: bool = typer.Option(
        False,
        "--prompt",
        help=(
            "Also send a one-line test prompt, costing a few tokens. Catches an "
            "expired login, a used-up plan or an outdated adapter."
        ),
    ),
) -> None:
    """Check that an agent command is reachable and speaks ACP.

    Starts the agent, completes the ACP handshake and opens a session, then
    stops — no prompt is sent, so the check costs no tokens. ``--prompt``
    also sends a one-line test prompt and requires an answer, which proves
    the login works for real requests before an unattended run. A ``:<model>``
    (or ``:<model>:<effort>``) suffix is resolved the same way a run resolves
    it, so what the check reports is what a task naming that tool would get.
    Installing the agent and logging into it stay entirely yours; this only
    reports what is missing.

    :param name: a registered harness name, optionally with a model and
        effort suffix.
    :param cmd: an arbitrary command to check instead of a registered name.
    :param timeout: seconds to allow for the check.
    :param prompt: also send a one-line test prompt and require a reply.
    """
    if (name is None) == (cmd is None):
        console.print("[red]pass either a harness name or --cmd, not both[/red]")
        raise typer.Exit(code=2)

    if cmd is not None:
        argv = shlex.split(cmd)
        if not argv:
            console.print("[red]--cmd is empty[/red]")
            raise typer.Exit(code=2)
        label = "ad-hoc"
        # A probe never prompts, so the sink is irrelevant here.
        executor = AcpHarnessExecutor(launch_cmd=argv)
    else:
        tool = name if name.startswith("harness:") else f"harness:{name}"
        if not re.fullmatch(HARNESS_TOOL_PATTERN, tool):
            console.print(
                f"[red]malformed harness:[/red] {escape(tool)} — expected "
                "<name>, <name>:<model> or <name>:<model>:<effort>"
            )
            raise typer.Exit(code=2)
        base, model, effort = split_harness_tool(tool)
        executor = Atelier().executors.get(base)
        if executor is None:
            console.print(
                f"[red]unknown harness:[/red] {escape(base)} "
                "— try 'atelier list harnesses'"
            )
            raise typer.Exit(code=1)
        if model is not None:
            executor = executor.with_model(model, effort)
        label = tool

    result = asyncio.run(executor.probe(timeout=timeout, prompt=prompt))
    _render_probe(label, executor.launch_cmd, result)
    if not result.ok:
        raise typer.Exit(code=1)


@harness_app.command("sync")
def harness_sync_cmd() -> None:
    """Refresh the ACP agent registry snapshot from the network.

    Downloads the upstream registry and stores it in the global atelier dir,
    where it supersedes the snapshot bundled with this release. This is the
    only command that touches the network — runs always read the snapshot.
    """
    atelier = Atelier()
    before = set(atelier.registry)
    try:
        snapshot = fetch_registry()
    except (URLError, OSError, ValueError) as exc:
        console.print(f"[red]registry sync failed:[/red] {escape(str(exc))}")
        console.print(f"[dim]source: {REGISTRY_URL}[/dim]")
        raise typer.Exit(code=1)

    destination = atelier.settings.global_atelier_dir / SNAPSHOT_FILENAME
    write_snapshot(destination, snapshot)
    ids = {agent["id"] for agent in snapshot["agents"]}
    added = sorted(ids - before)
    console.print(
        f"[green]synced[/green] {len(ids)} agents → {escape(str(destination))}"
    )
    if added:
        console.print(f"[dim]new: {escape(', '.join(added))}[/dim]")
    root = atelier.settings.global_atelier_dir / INSTALL_DIRNAME
    if root.is_dir():
        # An agent installed at its old pin now falls back to npx.
        installed_before = {
            agent_id for agent_id, spec in atelier.registry.items() if spec.kind == "installed"
        }
        stale = sorted(
            agent_id
            for agent_id, spec in load_registry(destination, root).items()
            if agent_id in installed_before and spec.kind != "installed"
        )
        if stale:
            console.print(
                f"[yellow]new versions pinned; run 'atelier harness install "
                f"{escape(' '.join(stale))}' to keep skipping npx[/yellow]"
            )


@harness_app.command("install")
def harness_install_cmd(
    names: list[str] = typer.Argument(
        ..., help="Harnesses to install, e.g. claude-code codex."
    ),
) -> None:
    """Install npx adapters so each agent task starts without ``npx``.

    ``npx`` spends about a second per launch resolving a package it already
    cached. This installs the exact version the registry pins into the
    global atelier dir; every later launch runs that copy directly. Re-run
    it after ``atelier harness sync`` pins a newer version, or launches fall
    back to ``npx``. Logging into the agent stays yours.

    :param names: harness names, with or without the ``harness:`` prefix.
    """
    atelier = Atelier()
    root = atelier.settings.global_atelier_dir / INSTALL_DIRNAME
    failed = False
    for raw in names:
        name = raw.removeprefix("harness:")
        spec = atelier.registry.get(name) or atelier.registry.get(
            LEGACY_HARNESS_ALIASES.get(name, "")
        )
        if spec is None:
            console.print(
                f"[red]unknown harness:[/red] {escape(name)} — try 'atelier list harnesses'"
            )
            failed = True
            continue
        if spec.kind not in ("npx", "installed"):
            console.print(
                f"[yellow]skipped[/yellow] {escape(name)} — it runs via {escape(spec.kind)}, "
                "not npx, so there is nothing to install"
            )
            continue
        try:
            executable = install_agent(root, spec.package)
        except OSError as exc:
            console.print(f"[red]install failed:[/red] {escape(name)} — {escape(str(exc))}")
            failed = True
            continue
        console.print(
            f"[green]installed[/green] {escape(name)} ({escape(spec.package)}) "
            f"→ {escape(str(executable))}"
        )
    if failed:
        raise typer.Exit(code=1)
