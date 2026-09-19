"""`atelier install`, `update` and `uninstall` — conduit packages from a repo or path."""
from __future__ import annotations

import sys

import typer
from rich.markup import escape

from flow_atelier.cli._shared import console
from flow_atelier.cli.main import app
from flow_atelier.core.atelier import Atelier
from flow_atelier.services.package import InstallReport, PackageError, RemoveReport


def _print_report(report: InstallReport) -> None:
    """Render an install report in the spec's ``+``/``~`` format.

    :param report: the :class:`InstallReport` to render.
    """
    console.print(f"conduits → {escape(str(report.conduit_root))}")
    for name in report.conduits_installed:
        console.print(f"  [green]+[/green] {escape(name)}")
    for name in report.conduits_skipped:
        console.print(
            f"  [yellow]~[/yellow] {escape(name)}   (exists, skipped — use --force)"
        )
    n_skipped = len(report.conduits_skipped)
    summary = f"installed {len(report.conduits_installed)} conduits"
    if n_skipped:
        summary += f" ({n_skipped} skipped)"
    console.print(f"[green]{summary}[/green]")
    if report.conduits_installed:
        console.print(
            f"[dim]run: atelier run {escape(report.conduits_installed[0])}[/dim]"
        )


def _prompt_scope() -> bool:
    """Ask where to install; g/empty -> global, p -> project. Re-ask on bad input.

    :returns: ``True`` for the project store, ``False`` for global.
    """
    while True:
        ans = input(
            "Install globally (~/.atelier) or in this project (./.atelier)? [g/p] "
        ).strip().lower()
        if ans in ("", "g"):
            return False
        if ans == "p":
            return True


@app.command("install", help="Install a conduit package from a git repo or local path.")
@app.command("add", hidden=True)
def install_cmd(
    source: str = typer.Argument(
        ..., help="git URL, owner/repo, or a local path."
    ),
    force: bool = typer.Option(
        False, "--force", help="Overwrite existing conduits on collision."
    ),
    project: bool | None = typer.Option(
        None,
        "--project/--no-project",
        help="Install conduits into the project store, not global.",
    ),
    ref: str = typer.Option(
        None, "--ref", help="git branch/tag/commit to check out."
    ),
) -> None:
    """Fetch a package and install its conduits.

    :param source: git URL, ``owner/repo`` shorthand, or a local path.
    :param force: overwrite colliding conduits instead of skipping.
    :param project: install conduits into the project store rather than global;
        ``None`` resolves via an interactive prompt on a TTY, else global.
    :param ref: optional git ref to check out.
    """
    if project is None:
        project = _prompt_scope() if sys.stdin.isatty() else False
    try:
        report = Atelier().install_package(
            source, ref=ref, project=project, force=force
        )
    except PackageError as exc:
        console.print(f"[red]error:[/red] {escape(str(exc))}")
        raise typer.Exit(code=1)
    _print_report(report)


def _print_remove_report(report: RemoveReport) -> None:
    """Render a remove report.

    :param report: the :class:`RemoveReport` to render.
    """
    for name in report.conduits_removed:
        console.print(f"  [red]-[/red] {escape(name)}")
    console.print(
        f"[green]removed {len(report.conduits_removed)} conduits[/green]"
    )


@app.command("update", help="Re-fetch and re-install a package from its source.")
def update_cmd(
    name: str = typer.Argument(..., help="Installed package name."),
    force: bool = typer.Option(
        False, "--force", help="Overwrite existing conduits on collision."
    ),
) -> None:
    """Re-fetch and re-install a package from its recorded source.

    :param name: installed package name (lockfile key).
    :param force: overwrite colliding conduits instead of skipping.
    """
    try:
        report = Atelier().update_package(name, force=force)
    except PackageError as exc:
        console.print(f"[red]error:[/red] {escape(str(exc))}")
        raise typer.Exit(code=1)
    _print_report(report)


@app.command("uninstall", help="Uninstall a package's conduits.")
@app.command("remove", hidden=True)
def uninstall_cmd(
    name: str = typer.Argument(..., help="Installed package name."),
) -> None:
    """Delete exactly the conduit dirs a package installed.

    :param name: installed package name (lockfile key).
    """
    try:
        report = Atelier().remove_package(name)
    except PackageError as exc:
        console.print(f"[red]error:[/red] {escape(str(exc))}")
        raise typer.Exit(code=1)
    _print_remove_report(report)
