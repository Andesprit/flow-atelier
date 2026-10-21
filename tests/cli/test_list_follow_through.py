"""Lists keep copyable identifiers and concise descriptions at agent width."""
from __future__ import annotations

import json

import pytest
import yaml
from typer.testing import CliRunner

from flow_atelier.cli import app
from flow_atelier.cli._shared import console
from flow_atelier.core.atelier import Atelier

NAME = "a-long-conduit-name-that-agents-must-copy-without-wrapping"
DESCRIPTION = "Useful first sentence. " + "More detail about this conduit. " * 30


@pytest.fixture
def project(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(console, "width", 80)
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
    result = CliRunner().invoke(app, ["list", "flows"])
    assert result.exit_code == 0, result.stdout
    assert fid in result.stdout
    assert NAME in result.stdout


def test_compact_table_keeps_full_description_in_json(project):
    fid = project.store.create_flow(NAME, {})
    runner = CliRunner()
    conduits = runner.invoke(app, ["list", "conduits", "--json"])
    rows = json.loads(conduits.stdout)
    assert rows[0]["description"] == DESCRIPTION
    assert set(rows[0]) == {"name", "source", "description", "tasks", "inputs", "error"}
    flows = runner.invoke(app, ["list", "flows", "--json"])
    assert json.loads(flows.stdout)[0]["flow_id"] == fid
