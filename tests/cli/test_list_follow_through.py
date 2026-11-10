"""Lists keep copyable identifiers and concise descriptions at agent width."""
from __future__ import annotations

import json

import pytest
import yaml
from typer.testing import CliRunner

from flow_atelier.cli import app
from flow_atelier.cli._shared import _format_clock, _format_duration_seconds, console
from flow_atelier.core.atelier import Atelier
from flow_atelier.schemas.progress import FlowStatus, TaskProgress, TaskStatus

NAME = "a-long-conduit-name-that-agents-must-copy-without-wrapping"
DESCRIPTION = "Useful first sentence. " + "More detail about this conduit. " * 30


@pytest.fixture
def project(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    # Patch the private field: restoring the ``width`` property would pin the
    # shared console to one width for every later test.
    monkeypatch.setattr(console, "_width", 80)
    directory = tmp_path / ".atelier/conduits" / NAME
    directory.mkdir(parents=True)
    (directory / "conduit.yaml").write_text(yaml.safe_dump({
        "name": NAME, "description": DESCRIPTION,
        "tasks": [{"greet": {"description": "greet", "tool": "tool:bash", "task": "echo hi"}}],
    }))
    return Atelier()


def test_bare_list_shows_conduits(project):
    runner = CliRunner()
    result = runner.invoke(app, ["list"])
    explicit = runner.invoke(app, ["list", "conduits"])
    assert result.exit_code == 0, result.stdout
    assert result.stdout == explicit.stdout
    assert NAME in result.stdout


def test_conduit_names_stay_intact_and_descriptions_stay_compact(project):
    result = CliRunner().invoke(app, ["list", "conduits"])
    assert result.exit_code == 0, result.stdout
    assert NAME in result.stdout
    assert len(result.stdout.splitlines()) <= 6


def test_flow_ids_stay_intact_at_80_columns(project):
    fid = project.store.create_flow(NAME, {})
    progress = project.store.read_progress(fid)
    progress.status = FlowStatus.completed
    progress.started_at = "2026-10-21T12:34:00Z"
    progress.finished_at = "2026-10-21T12:35:23Z"
    progress.tasks = {"greet": TaskProgress(status=TaskStatus.completed)}
    project.store.write_progress(fid, progress)
    result = CliRunner().invoke(app, ["list", "flows"])
    assert result.exit_code == 0, result.stdout
    row = next(line for line in result.stdout.splitlines() if fid in line)
    assert NAME in row
    assert "completed" in row
    assert _format_clock(progress.started_at) in row
    assert _format_duration_seconds(83) in row
    assert "✓1" in row
    assert "…" not in row


def test_compact_table_keeps_full_description_in_json(project):
    fid = project.store.create_flow(NAME, {})
    runner = CliRunner()
    conduits = runner.invoke(app, ["list", "conduits", "--json"])
    rows = json.loads(conduits.stdout)
    assert rows[0]["description"] == DESCRIPTION
    assert set(rows[0]) == {"name", "source", "description", "tasks", "inputs", "error"}
    flows = runner.invoke(app, ["list", "flows", "--json"])
    assert json.loads(flows.stdout)[0]["flow_id"] == fid
