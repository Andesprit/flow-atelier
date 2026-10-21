"""Explain the list syntax when someone writes tasks as a mapping."""
from __future__ import annotations

import json

import pytest
from typer.testing import CliRunner

from flow_atelier.cli import app


@pytest.mark.parametrize("command", [["check", "ship"], ["check", "ship", "--json"], ["run", "ship"], ["plan", "ship"]])
def test_mapping_tasks_explains_list_syntax_before_missing_description(tmp_path, monkeypatch, command):
    monkeypatch.chdir(tmp_path)
    directory = tmp_path / ".atelier/conduits/ship"
    directory.mkdir(parents=True)
    (directory / "conduit.yaml").write_text(
        "name: ship\ntasks:\n  build:\n    description: build\n    tool: tool:bash\n    task: echo hi\n"
    )
    result = CliRunner().invoke(app, command)
    assert result.exit_code == 1, result.stdout
    message = json.loads(result.stdout)[0]["error"] if "--json" in command else result.stdout
    assert "tasks must be a list" in message
    assert "- build:" in message
    assert "Input should be a valid list" not in message
    assert message.index("tasks must be a list") < message.index("missing description")
