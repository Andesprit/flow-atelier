"""CLI tests for ``atelier ask``."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from click import unstyle
from typer.testing import CliRunner

from flow_atelier.cli import app
from flow_atelier.schemas.conduit import split_harness_tool

FAKE_AGENT = Path(__file__).resolve().parents[1] / "fixtures" / "fake_acp_agent.py"


@pytest.mark.parametrize(
    "harness",
    [None, "custom-agent:m2", "custom-agent:opus[1m]", "custom-agent:m2:max"],
)
def test_ask_runs_an_interactive_agent_session_in_path(tmp_path, monkeypatch, harness) -> None:
    """The query, path and optional model and effort reach the agent.

    :param tmp_path: pytest temporary directory fixture.
    :param monkeypatch: pytest monkeypatch fixture.
    :param harness: optional custom agent, model and effort to select.
    """
    project = tmp_path / "target-project"
    project.mkdir()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("ATELIER_ATELIER_DIR", str(tmp_path / ".atelier"))
    script = json.dumps(
        {
            "models": {"current": "m1", "available": [
                {"id": "m1", "name": "One"}, {"id": "m2", "name": "Two"},
                {"id": "opus[1m]", "name": "Opus 1M"},
            ]},
            # "max" exists only once m2 is selected, so an effort suffix is
            # only honoured if the model went first.
            "efforts": {
                "current": "medium",
                "available": ["low", "medium", "high"],
                "per_model": {"m2": {"current": "high", "available": ["high", "max"]}},
            },
            "turns": [
                {"chunks": ["Which colour? "], "stop": "end_turn"},
                {"chunks": ["Blue it is. [ATELIER_DONE]"], "stop": "end_turn"},
            ]
        }
    )
    launch = [sys.executable, str(FAKE_AGENT), "--script", script]
    if harness:
        monkeypatch.setenv("ATELIER_HARNESSES", json.dumps({"custom-agent": launch}))
    else:
        monkeypatch.setenv("ATELIER_CLAUDE_LAUNCH_CMD", json.dumps(launch))

    query = "Help me write a specification"
    result = CliRunner().invoke(
        app,
        ["ask", query, "--path", str(project)] + (["--harness", harness] if harness else []),
        input="blue\n",
    )

    assert result.exit_code == 0, result.output
    assert "Which colour?" in result.output
    assert "Blue it is." in result.output
    assert "[ATELIER_DONE]" not in result.output

    flow_dirs = list((tmp_path / ".atelier" / "flows").iterdir())
    assert len(flow_dirs) == 1
    progress = json.loads((flow_dirs[0] / "progress.json").read_text())
    assert progress["run_path"] == str(project.resolve())
    logs = [json.loads(line) for line in (flow_dirs[0] / "logs.jsonl").read_text().splitlines()]
    assert logs[-1]["command"] == query
    assert logs[-1]["task"] == "chat"
    assert logs[-1]["tool"] == f"harness:{harness or 'claude-code'}"
    assert "Blue it is." in logs[-1]["output"]
    if harness:
        _, model, effort = split_harness_tool(f"harness:{harness}")
        assert f"[config_set:model={model}]" in result.output
        if effort:
            assert f"[config_set:reasoning_effort={effort}]" in result.output


def test_ask_requires_a_path(tmp_path, monkeypatch) -> None:
    """The command refuses to start without an explicit target directory.

    :param tmp_path: pytest temporary directory fixture.
    :param monkeypatch: pytest monkeypatch fixture.
    """
    monkeypatch.chdir(tmp_path)
    result = CliRunner().invoke(app, ["ask", "hello"])
    assert result.exit_code == 2
    assert "--path" in unstyle(result.output)


@pytest.mark.parametrize(
    "harness",
    ["", "Bad Name", "codex:", "codex:m:", "codex:m:a:b", "codex::high"],
)
def test_ask_invalid_harness_is_a_usage_error(tmp_path, monkeypatch, harness) -> None:
    """Malformed harness options explain the error without starting a flow."""
    monkeypatch.chdir(tmp_path)
    result = CliRunner().invoke(
        app, ["ask", "hello", "--path", str(tmp_path), "--harness", harness],
    )
    assert result.exit_code == 2, result.output
    assert "--harness" in unstyle(result.output)
    assert "Traceback" not in result.output
    assert not (tmp_path / ".atelier" / "flows").exists()
