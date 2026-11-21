"""CLI tests for ``atelier ask``."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest
from click import unstyle
from typer.testing import CliRunner

from flow_atelier.cli import app
from flow_atelier.schemas.conduit import split_harness_tool
from flow_atelier.services.store.filesystem import FilesystemStore

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


def test_ask_permissions_flag_sets_the_level(tmp_path, monkeypatch) -> None:
    """``--permissions read-only`` reaches the agent; a bad level is a usage error.

    :param tmp_path: pytest temporary directory fixture.
    :param monkeypatch: pytest monkeypatch fixture.
    """
    project = tmp_path / "target-project"
    project.mkdir()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("ATELIER_ATELIER_DIR", str(tmp_path / ".atelier"))
    monkeypatch.delenv("ATELIER_AGENT_PERMISSIONS", raising=False)
    script = json.dumps({
        "modes": {"current": "agent", "available": [
            {"id": "read-only", "name": "Read Only"}, {"id": "agent", "name": "Agent"},
        ]},
        "turns": [{"chunks": ["Looked only. [ATELIER_DONE]"]}],
    })
    launch = [sys.executable, str(FAKE_AGENT), "--script", script]
    monkeypatch.setenv("ATELIER_HARNESSES", json.dumps({"custom-agent": launch}))

    result = CliRunner().invoke(app, [
        "ask", "Review it", "--path", str(project), "--harness", "custom-agent",
        "--permissions", "read-only",
    ])
    assert result.exit_code == 0, result.output
    flow_dir = next((tmp_path / ".atelier" / "flows").iterdir())
    logs = [json.loads(line) for line in (flow_dir / "logs.jsonl").read_text().splitlines()]
    assert "[mode_set:read-only]" in logs[-1]["output"]

    result = CliRunner().invoke(app, [
        "ask", "Review it", "--path", str(project), "--permissions", "none",
    ])
    assert result.exit_code == 2
    assert "full, edits, read-only" in unstyle(result.output)


@pytest.fixture
def machine_ask(tmp_path):
    """Run each turn in a fresh CLI process with stdin closed and a scripted ACP adapter."""
    def invoke(*args, script=None, success=True):
        env = {
            **os.environ,
            "ATELIER_ATELIER_DIR": str(tmp_path / ".atelier"),
            "ATELIER_HARNESSES": json.dumps({"fake": [
                sys.executable, str(FAKE_AGENT), "--script", json.dumps(script or {}),
            ]}),
        }
        proc = subprocess.run(
            [sys.executable, "-m", "flow_atelier.main", "ask", *args, "--json"],
            stdin=subprocess.DEVNULL, capture_output=True, text=True, cwd=tmp_path,
            env=env, timeout=15,
        )
        if success:
            assert proc.returncode == 0, proc.stderr
            return json.loads(proc.stdout)
        assert proc.returncode != 0
        assert proc.stdout == ""
        return json.loads(proc.stderr)["error"]
    return invoke


@pytest.mark.parametrize("reload", ["resume", "load"])
def test_read_and_answer_pending_session(machine_ask, tmp_path, reload):
    records = tmp_path / "protocol"
    records.mkdir()
    script = {
        "reload": reload, "record_path": str(records),
        "turns": [{"chunks": ["Which colour (色)?"]}],
        "replay": ["OLD HISTORY"],
        "resumed_turns": [{"chunks": ["Blue it is. [ATELIER_DONE]"]}],
    }
    first = machine_ask("Pick a colour", "--path", ".", "--harness", "fake", script=script)
    flow_id = first["flow_id"]
    assert first["status"] == "needs_input" and first["question"] == "Which colour (色)?"
    assert machine_ask(flow_id) == first
    store = FilesystemStore(tmp_path / ".atelier")
    pending = store.read_progress(flow_id)
    assert pending.pending_ask and pending.runner_pid is None and pending.finished_at is None
    assert machine_ask(flow_id, "--request-id", "stale", "--answer", "wrong", success=False)["code"] == "no_pending_question"
    assert machine_ask(flow_id) == first

    reply = [flow_id, "--answer", "blue {{inputs.literal}}"]
    if reload == "load":
        reply += ["--request-id", first["request_id"]]
    result = machine_ask(*reply, script=script)
    assert result == {"flow_id": flow_id, "status": "completed", "answer": "Blue it is."}
    assert machine_ask(flow_id) == result
    assert machine_ask(*reply, script=script, success=False)["code"] == "no_pending_question"
    progress = store.read_progress(flow_id)
    assert progress.pending_ask is None
    assert progress.started_at == pending.started_at
    assert store.read_outputs(flow_id)["chat"] == result["answer"]

    events = [json.loads(line) for f in records.glob("*.events") for line in f.read_text().splitlines()]
    reopened = [e for e in events if e["event"] == f"{reload}_session"]
    assert len(reopened) == 1 and reopened[0]["session_id"] == pending.pending_ask.session_id
    assert sum(e["event"] == "new_session" for e in events) == 1
    prompts = [json.loads(line)[0]["text"] for f in records.glob("*.jsonl") for line in f.read_text().splitlines()]
    assert len(prompts) == 2 and any(p.startswith("blue {{inputs.literal}}") for p in prompts)


def test_direct_answer_and_legacy_progress(machine_ask, tmp_path):
    result = machine_ask(
        "Say hello", "--path", ".", "--harness", "fake",
        script={"turns": [{"chunks": ["Hello! [ATELIER_DONE]"]}]},
    )
    assert result["status"] == "completed" and result["answer"] == "Hello!"
    path = tmp_path / ".atelier" / "flows" / result["flow_id"] / "progress.json"
    progress = json.loads(path.read_text())
    progress.pop("pending_ask", None)
    path.write_text(json.dumps(progress))
    assert machine_ask(result["flow_id"]) == result


def test_machine_ask_cannot_substitute_a_fresh_session(machine_ask, tmp_path):
    records = tmp_path / "protocol"
    records.mkdir()
    script = {"turns": [{"chunks": ["Question?"]}], "record_path": str(records)}
    start = ["Help", "--path", ".", "--harness", "fake"]
    unsupported = machine_ask(*start, script=script, success=False)
    assert unsupported["code"] == "session_unsupported"
    script.update(reload="resume", reload_fails="session expired")
    question = machine_ask(*start, script=script)
    failed = machine_ask(
        question["flow_id"], "--request-id", question["request_id"], "--answer", "do it",
        script=script, success=False,
    )
    assert failed["code"] == "session_unavailable"
    assert sum(len(p.read_text().splitlines()) for p in records.glob("*.jsonl")) == 2


def test_ask_can_cancel_a_pending_turn(machine_ask, tmp_path):
    script = {
        "reload": "load", "turns": [{"chunks": ["Where?"]}],
        "resumed_turns": [{"chunks": ["When?"]}],
    }
    first = machine_ask("Help", "--path", ".", "--harness", "fake", "--timeout", "2", script=script)
    flow_id = first["flow_id"]
    reply = [flow_id, "--answer", "here", "--request-id", first["request_id"]]
    with FilesystemStore(tmp_path / ".atelier").lock_ask(flow_id) as acquired:
        assert acquired
        assert machine_ask(*reply, success=False)["code"] == "ask_busy"
    time.sleep(2.1)
    second = machine_ask(*reply, script=script)
    assert second["status"] == "needs_input" and second["question"] == "When?"
    assert second["request_id"] != first["request_id"]
    assert machine_ask(*reply, success=False)["code"] == "no_pending_question"
    assert machine_ask(flow_id, "--answer", "here", success=False)["code"] == "no_pending_question"
    assert machine_ask(flow_id) == second
    assert machine_ask(flow_id, "--cancel") == {"flow_id": flow_id, "status": "stopped"}
    assert machine_ask(flow_id, "--answer", "here", "--request-id", first["request_id"], success=False)["code"] == "no_pending_question"


@pytest.mark.parametrize(("turn", "message"), [
    ({"delay_before": 5}, "timeout"),
    ({"usage_limit": {"data": {"errorKind": "rate_limit"}}}, "usage limit"),
])
def test_machine_ask_bounds_active_execution(machine_ask, turn, message):
    error = machine_ask("Help", "--path", ".", "--harness", "fake", "--timeout", "1",
                        script={"turns": [turn]}, success=False)
    assert message in error["message"]
    assert machine_ask(error["flow_id"])["status"] == "failed"
