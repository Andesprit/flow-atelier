"""Test the machine error contract at the process boundary."""
from __future__ import annotations

import json
import subprocess
import sys

import click
import pytest
import typer
from typer.testing import CliRunner

from flow_atelier.cli.main import AtelierTyper
from flow_atelier.core.atelier import Atelier
from flow_atelier.schemas.progress import FlowStatus, Progress


def _invoke(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "flow_atelier.main", *args],
        stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=30,
    )


@pytest.mark.parametrize(
    ("args", "code", "exit_code", "fields"),
    [
        (["wait", "absent", "--json"], "unknown_flow", 1, {"flow_id": "absent"}),
        (["wait", "latest", "--json"], "unknown_flow", 1, {"flow_id": "latest"}),
        (["wait", "--json"], "invalid_arguments", 2, {"parameter": "flow_id"}),
        (["wait", "--timeout", "soon", "--json"], "invalid_arguments", 2, {"parameter": "--timeout"}),
        (["wait", "x", "--json", "--timeout", "0"], "invalid_arguments", 2, {"minimum": 1}),
        (["wait", "--bogus", "--json"], "invalid_arguments", 2, {"parameter": "--bogus"}),
        (["list", "flows", "--bogus", "--json"], "invalid_arguments", 2, {"parameter": "--bogus"}),
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


def test_ambiguous_flow_lists_every_candidate(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    store = Atelier().store
    ids = [store.create_flow("report", {}, flow_id=f"20260101_{i:08d}_report") for i in range(11)]
    result = _invoke("wait", "20260101", "--json")
    assert result.returncode == 1
    assert result.stdout == ""
    error = json.loads(result.stderr)["error"]
    assert error["code"] == "ambiguous_flow_id"
    assert sorted(error["candidates"]) == ids


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
    assert "--timeout" in result.stderr
    assert not result.stderr.lstrip().startswith("{")


def test_choice_error_names_allowed_values():
    app = AtelierTyper()

    @app.command()
    def example(
        mode: str = typer.Option("short", click_type=click.Choice(["short", "long"])),
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
