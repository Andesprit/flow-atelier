"""Explicit migration of legacy project run trees."""

import typer

from flow_atelier.cli.main import migrate_app
from flow_atelier.core.settings import AtelierSettings
from flow_atelier.services.store.filesystem import FilesystemStore
from flow_atelier.services.store.migrate import migrate_tree


@migrate_app.command("runs")
def runs_cmd(
    dry_run: bool = typer.Option(False, "--dry-run", help="Verify without applying the migration."),
) -> None:
    """Move legacy runs to global history and archive verified sources under migrated-runs/."""
    settings = AtelierSettings()
    old = FilesystemStore(settings.atelier_dir.resolve())
    home = settings.global_atelier_dir.resolve()
    failed = False
    roots = old.list_flows()
    if not roots:
        typer.echo("No legacy runs to migrate.")
    for root in roots:
        try:
            ids = migrate_tree(old, home, root, dry_run=dry_run)
        except (OSError, ValueError) as exc:
            failed = True
            typer.echo(f"Kept {root}: {exc}", err=True)
            continue
        action = "Would migrate" if dry_run else "Migrated"
        typer.echo(f"{action} {', '.join(ids)}")
    if failed:
        raise typer.Exit(1)
