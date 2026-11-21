"""Test the machine error contract at the process boundary."""
from __future__ import annotations

import json
import subprocess
import sys
from enum import StrEnum

import pytest
import typer
from click import unstyle
from typer.testing import CliRunner

from flow_atelier.cli.main import AtelierTyper
from flow_atelier.core.atelier import Atelier
from flow_atelier.schemas.progress import FlowStatus, Progress

_FLOW_COMMANDS = ["diagnose", "logs", "outputs", "rm", "status", "timing", "wait"]


def _invoke(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "flow_atelier.main", *args],
        stdin=subprocess.DEVNULL, capture_output=True, encoding="utf-8", timeout=30,
    )


@pytest.mark.parametrize(
    ("args", "code", "exit_code", "fields"),
    [
        *[
            ([cmd, "absent", "--json"], "unknown_flow", 1, {"flow_id": "absent"})
            for cmd in _FLOW_COMMANDS
        ],
        (["compare", "absent", "other", "--json"], "unknown_flow", 1, {"flow_id": "absent"}),
        (["compare", "absent", "--json"], "insufficient_runs", 1, {"conduit": "absent"}),
        *[
            ([cmd, "absent", "--json"], "unknown_conduit", 1, {"conduit": "absent"})
            for cmd in ["check", "plan", "show"]
        ],
        (["check", "--probe", "--json"], "invalid_arguments", 2, {"parameter": "--probe"}),
        (["check", "--agent", "bad", "--json"], "invalid_arguments", 2, {"parameter": "--agent"}),
        (["plan", "x", "--worktree", "a=b", "--json"], "invalid_arguments", 2,
         {"parameter": "--worktree"}),
        (["logs", "x", "--last", "-1", "--json"], "invalid_arguments", 2,
         {"parameter": "--last"}),
        (["logs", "x", "--follow", "--json"], "invalid_arguments", 2, {"parameter": "--follow"}),
        (["logs", "x", "--show", "bad", "--json"], "invalid_arguments", 2,
         {"parameter": "--show", "allowed": ["output", "stdout", "stderr", "steps", "all"]}),
        (["prune", "--json"], "invalid_arguments", 2, {}),
        (["schedule", "history", "absent", "--json"], "unknown_schedule", 1, {"schedule": "absent"}),
        (["wait", "latest", "--json"], "unknown_flow", 1, {"flow_id": "latest"}),
        (["wait", "--json"], "invalid_arguments", 2, {"parameter": "flow_id"}),
        (["wait", "--timeout", "soon", "--json"], "invalid_arguments", 2, {"parameter": "--timeout"}),
        (["wait", "x", "--json", "--timeout", "0"], "invalid_arguments", 2, {"minimum": 1}),
        (["wait", "--bogus", "--json"], "invalid_arguments", 2, {"parameter": "--bogus"}),
        *[
            ([*cmd, "--bogus", "--json"], "invalid_arguments", 2, {"parameter": "--bogus"})
            for cmd in [
                ["list", "conduits"], ["list", "flows"], ["list", "harnesses"],
                ["list", "schedules"], ["schedule", "list"], ["scheduler", "status"],
                ["harness", "list"],
            ]
        ],
    ],
)
def test_json_errors_leave_stdout_empty(tmp_path, monkeypatch, args, code, exit_code, fields):
    monkeypatch.chdir(tmp_path)
    # Exercise the default update check on a fresh store, too.
    monkeypatch.delenv("ATELIER_NO_UPDATE_CHECK")
    result = _invoke(*args)
    assert result.returncode == exit_code, result.stderr
    assert result.stdout == ""
    error = json.loads(result.stderr)["error"]
    assert error["code"] == code
    assert error["message"]
    assert fields.items() <= error.items()


@pytest.mark.parametrize("cmd", [[name] for name in _FLOW_COMMANDS] + [["compare", "latest"]])
def test_ambiguous_flow_lists_every_candidate(tmp_path, monkeypatch, cmd):
    monkeypatch.chdir(tmp_path)
    store = Atelier().store
    ids = [store.create_flow("report", {}, flow_id=f"20260101_{i:08d}_report") for i in range(11)]
    result = _invoke(*cmd, "20260101", "--json")
    assert result.returncode == 1
    assert result.stdout == ""
    error = json.loads(result.stderr)["error"]
    assert error["code"] == "ambiguous_flow_id"
    assert sorted(error["candidates"]) == ids


@pytest.mark.parametrize(
    ("args", "code", "exit_code"),
    [
        (["logs", "latest"], "no_log_entries", 1),
        (["timing", "latest"], "no_log_entries", 1),
        (["outputs", "latest", "--task", "absent"], "unknown_task", 1),
        (["rm", "latest"], "confirmation_required", 2),
    ],
)
def test_errors_on_existing_flow(tmp_path, monkeypatch, args, code, exit_code):
    monkeypatch.chdir(tmp_path)
    store = Atelier().store
    fid = store.create_flow("report", {})
    store.write_progress(fid, Progress(status=FlowStatus.completed))
    result = _invoke(*args, "--json")
    assert result.returncode == exit_code, result.stderr
    assert result.stdout == ""
    error = json.loads(result.stderr)["error"]
    assert error["code"] == code
    assert error["flow_id"] == fid
    assert store.read_progress(fid).status == FlowStatus.completed


def test_rm_running_flow_requires_force(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    store = Atelier().store
    fid = store.create_flow("report", {})
    store.write_progress(fid, Progress(status=FlowStatus.running))
    result = _invoke("rm", fid, "--yes", "--json")
    assert result.returncode == 1
    assert result.stdout == ""
    assert json.loads(result.stderr)["error"]["code"] == "flow_running"
    assert store.read_progress(fid).status == FlowStatus.running


def test_invalid_definition_reports_check_rows_and_inspection_errors(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    recipe = tmp_path / ".atelier" / "conduits" / "broken" / "conduit.yaml"
    recipe.parent.mkdir(parents=True)
    recipe.write_text("name: [")
    result = _invoke("check", "broken", "--json")
    assert result.returncode == 1
    assert result.stderr == ""
    assert json.loads(result.stdout)[0]["ok"] is False
    for cmd in ["show", "plan"]:
        result = _invoke(cmd, "broken", "--json")
        assert result.returncode == 1
        assert result.stdout == ""
        error = json.loads(result.stderr)["error"]
        assert error["code"] == "invalid_conduit"
        assert error["conduit"] == "broken"


def test_config_warning_survives_json_success_and_failure(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text("ATELIER_AGENT_PERMISSIONS=full\n")
    success = _invoke("list", "conduits", "--json")
    assert success.returncode == 0
    assert json.loads(success.stdout) == []
    assert "ignored ATELIER_AGENT_PERMISSIONS" in success.stderr

    failure = _invoke("outputs", "absent", "--json")
    assert failure.returncode == 1
    assert failure.stdout == ""
    error = json.loads(failure.stderr)["error"]
    assert error["code"] == "unknown_flow"
    assert error["diagnostics"] == success.stderr.splitlines()


def test_corrupt_saved_outputs(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    store = Atelier().store
    fid = store.create_flow("report", {})
    path = tmp_path / ".atelier" / "flows" / fid / "outputs.yaml"
    path.write_text("broken: [")
    result = _invoke("outputs", fid, "--json")
    assert result.returncode == 1
    assert result.stdout == ""
    assert json.loads(result.stderr)["error"]["code"] == "command_failed"


def test_missing_flow_keeps_human_guidance(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    result = _invoke("logs", "absent")
    assert result.returncode == 1
    assert "unknown flow: absent" in result.stdout
    assert "atelier list flows" in result.stdout
    assert result.stderr == ""


@pytest.mark.parametrize(("status", "exit_code"), [("completed", 0), ("failed", 1), ("stopped", 1)])
def test_wait_returns_observed_status_on_stdout(tmp_path, monkeypatch, status, exit_code):
    monkeypatch.chdir(tmp_path)
    store = Atelier().store
    fid = store.create_flow("report", {})
    store.write_progress(fid, Progress(status=FlowStatus(status)))
    result = _invoke("wait", "latest", "--json")
    assert result.returncode == exit_code
    assert json.loads(result.stdout) == {"flow_id": fid, "status": status}
    assert result.stderr == ""


@pytest.mark.parametrize("args", [["--timeout", "--json"], ["--timeout", "bad", "--", "--json"]])
def test_literal_json_argument_keeps_human_usage_errors(tmp_path, monkeypatch, args):
    monkeypatch.chdir(tmp_path)
    result = _invoke("wait", *args)
    assert result.returncode == 2
    assert "--timeout" in unstyle(result.stderr)
    assert not result.stderr.lstrip().startswith("{")


class Mode(StrEnum):
    short = "short"
    long = "long"


def test_choice_error_names_allowed_values():
    app = AtelierTyper()

    @app.command()
    def example(
        mode: Mode = typer.Option(Mode.short),
        json_mode: bool = typer.Option(False, "--json"),
    ) -> None:
        raise AssertionError("invalid choices must be rejected before invocation")

    result = CliRunner().invoke(app, ["--mode", "other", "--json"])
    assert result.exit_code == 2
    assert result.stdout == ""
    error = json.loads(result.stderr)["error"]
    assert error["code"] == "invalid_arguments"
    assert error["parameter"] == "--mode"
    assert error["allowed"] == ["short", "long"]
