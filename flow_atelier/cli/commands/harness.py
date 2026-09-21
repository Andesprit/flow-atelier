"""`atelier harness` sub-app and `atelier list harnesses` — check, refresh and list ACP agents.

Nothing here installs an agent or logs into one; that stays with the user
and the agent's own CLI. These commands only report what is available and
what is missing.
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
    LEGACY_HARNESS_ALIASES,
    REGISTRY_URL,
    SNAPSHOT_FILENAME,
    fetch_registry,
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
    # distribution: npx/uvx fetch themselves on first run, binary means
    # the user installed the CLI and we run it from PATH.
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
        return

    console.print(f"  [red]not usable[/red] — {escape(result.detail)}")
    # Say what the user has to do, and be explicit that it is theirs to do:
    # flow-atelier never installs an agent and never logs one in.
    if result.stage == "path":
        console.print(
            "  [yellow]install this agent yourself[/yellow] and make sure its "
            "command is on PATH, then re-run this check"
        )
    elif result.stage == "session":
        if result.auth_methods:
            console.print(
                f"  [yellow]this agent likely needs a login[/yellow] — it accepts: "
                f"{escape(', '.join(result.auth_methods))}"
            )
        console.print(
            "  [yellow]log in with the agent's own CLI[/yellow], then re-run this check"
        )
    elif result.stage in ("initialize", "handshake"):
        console.print(
            "  [yellow]the command started but did not speak ACP[/yellow] — check "
            "it is the agent's ACP entry point (some CLIs need an --acp flag)"
        )
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
        PROBE_TIMEOUT_SECONDS, "--timeout", help="Seconds to allow for the handshake."
    ),
) -> None:
    """Check that an agent command is reachable and speaks ACP.

    Starts the agent, completes the ACP handshake and opens a session, then
    stops — no prompt is sent, so the check costs no tokens. A ``:<model>``
    (or ``:<model>:<effort>``) suffix is resolved the same way a run resolves
    it, so what the check reports is what a task naming that tool would get.
    Installing the agent and logging into it stay entirely yours; this only
    reports what is missing.

    :param name: a registered harness name, optionally with a model and
        effort suffix.
    :param cmd: an arbitrary command to check instead of a registered name.
    :param timeout: seconds to allow for the handshake.
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

    result = asyncio.run(executor.probe(timeout=timeout))
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
