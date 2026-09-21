"""CLI tests for `atelier harness list` / `atelier harness sync`."""
from __future__ import annotations

import json
import os
import shlex
import sys
from pathlib import Path
from urllib.error import URLError

import pytest
from typer.testing import CliRunner

from flow_atelier.cli import app
from flow_atelier.services.executor import acp_registry

FAKE_AGENT = Path(__file__).resolve().parents[1] / "fixtures" / "fake_acp_agent.py"


def _fake_cmd(script: str) -> str:
    """Build a --cmd string running the fake ACP agent with ``script``.

    Quoted the way a user would have to quote it: the JSON contains spaces,
    and --cmd is parsed with shell word rules.

    :param script: the fake agent's JSON scenario.
    """
    return shlex.join([sys.executable, str(FAKE_AGENT), "--script", script])


@pytest.fixture
def workdir(tmp_path, monkeypatch):
    """Isolated cwd with an empty `.atelier` tree and isolated global dir.

    :param tmp_path: pytest temp directory fixture.
    :param monkeypatch: pytest monkeypatch fixture.
    """
    (tmp_path / ".atelier" / "conduits").mkdir(parents=True)
    global_dir = tmp_path / "global"
    (global_dir / "conduits").mkdir(parents=True)
    monkeypatch.chdir(tmp_path)
    for k in list(os.environ):
        if k.startswith("ATELIER_"):
            monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("ATELIER_GLOBAL_ATELIER_DIR", str(global_dir))
    monkeypatch.setenv("ATELIER_NO_UPDATE_CHECK", "1")
    return tmp_path


def test_list_json_includes_registry_and_custom_harnesses(workdir, monkeypatch):
    """Verify `harness list --json` reports registry agents and custom ones.

    :param workdir: isolated working directory fixture.
    :param monkeypatch: pytest monkeypatch fixture.
    """
    monkeypatch.setenv("ATELIER_HARNESSES", '{"mine": ["my-agent", "--acp"]}')
    result = CliRunner().invoke(app, ["list", "harnesses", "--json"])
    assert result.exit_code == 0, result.output
    rows = {row["tool"]: row for row in json.loads(result.output)}
    assert rows["harness:gemini"]["agent"] == "Gemini CLI"
    assert rows["harness:gemini"]["launch"].startswith("npx -y @google/gemini-cli")
    # A legacy alias is described by the registry entry it points at.
    assert rows["harness:claude-code"]["agent"] == "Claude Agent"
    assert rows["harness:mine"]["launch"] == "my-agent --acp"
    assert rows["harness:mine"]["via"] == "custom"


def test_list_ready_filters_out_missing_clis(workdir):
    """Verify `--ready` hides harnesses whose CLI is not on PATH.

    :param workdir: isolated working directory fixture.
    """
    result = CliRunner().invoke(app, ["list", "harnesses", "--ready", "--json"])
    assert result.exit_code == 0, result.output
    rows = json.loads(result.output)
    assert rows, "expected at least one runnable harness"
    assert all(row["ready"] for row in rows)


def test_check_reports_a_reachable_agent(workdir):
    """Verify `harness check --cmd` reports a working ACP agent.

    :param workdir: isolated working directory fixture.
    """
    script = json.dumps({"turns": []})
    result = CliRunner().invoke(
        app,
        ["harness", "check", "--cmd", _fake_cmd(script)],
    )
    assert result.exit_code == 0, result.output
    assert "ok" in result.output
    assert "fake-acp-agent" in result.output


def test_check_lists_models_and_how_to_pick_one(workdir):
    """A reachable agent's models are listed with the `harness:<name>:<model>` hint.

    :param workdir: isolated working directory fixture.
    """
    script = json.dumps(
        {
            "turns": [],
            "models": {
                "current": "m1",
                "available": [{"id": "m1", "name": "One"}, {"id": "m2", "name": "Two"}],
            },
        }
    )
    result = CliRunner().invoke(app, ["harness", "check", "--cmd", _fake_cmd(script)])
    assert result.exit_code == 0, result.output
    assert "models: m1, m2 (current m1)" in result.output
    # An ad-hoc --cmd has no harness name to put a model suffix on.
    assert ":<model>" not in result.output


_EFFORT_SCRIPT = {
    "turns": [],
    "models": {
        "current": "m1",
        "available": [{"id": "m1", "name": "One"}, {"id": "m2", "name": "Two"}],
    },
    "efforts": {
        "id": "reasoning_effort",
        "current": "medium",
        "available": ["low", "medium", "high"],
        "per_model": {"m2": {"current": "high", "available": ["high", "max"]}},
    },
}


def _check(workdir, monkeypatch, name: str, script: dict | None = None):
    """Register the fake agent as `harness:fake` and check ``name``.

    :param workdir: isolated working directory fixture.
    :param monkeypatch: pytest monkeypatch fixture.
    :param name: the argument passed to `atelier harness check`.
    :param script: the fake agent's scenario; defaults to the effort script.
    :returns: the CliRunner result.
    """
    launch = [sys.executable, str(FAKE_AGENT), "--script",
              json.dumps(script if script is not None else _EFFORT_SCRIPT)]
    monkeypatch.setenv("ATELIER_HARNESSES", json.dumps({"fake": launch}))
    # Wide enough that Rich reports a selection error on one line.
    monkeypatch.setenv("COLUMNS", "200")
    return CliRunner().invoke(app, ["harness", "check", name])


def test_check_lists_the_efforts_of_the_default_model(workdir, monkeypatch):
    """A bare check reports the efforts the session's own model offers.

    :param workdir: isolated working directory fixture.
    :param monkeypatch: pytest monkeypatch fixture.
    """
    result = _check(workdir, monkeypatch, "fake")
    assert result.exit_code == 0, result.output
    assert "efforts for m1: low, medium, high (current medium)" in result.output


def test_check_with_a_model_reports_that_model_s_efforts(workdir, monkeypatch):
    """Naming a model selects it first, so the efforts listed are its own.

    :param workdir: isolated working directory fixture.
    :param monkeypatch: pytest monkeypatch fixture.
    """
    result = _check(workdir, monkeypatch, "fake:m2")
    assert result.exit_code == 0, result.output
    assert "efforts for m2: high, max (current high)" in result.output
    assert "tool: harness:fake:m2:<effort>" in result.output


def test_check_reports_the_effort_it_selected_not_the_one_before_it(
    workdir, monkeypatch
):
    """The current effort must come from the setter's answer, not from before it.

    m2 opens on `high`; asking for `max` and then reporting `high` would tell
    the user a run is something it is not.
    """
    result = _check(workdir, monkeypatch, "fake:m2:max")
    assert result.exit_code == 0, result.output
    assert "efforts for m2: high, max (current max)" in result.output
    # A check never prompts, so the agent's scripted turns stay untouched.
    assert "done" not in result.output


def test_check_reports_the_model_it_selected_over_legacy_set_model(
    workdir, monkeypatch
):
    """`session/set_model` answers with nothing; the check still names m2."""
    script = {
        "turns": [],
        "models": {
            "via": "legacy",
            "current": "m1",
            "available": [{"id": "m1", "name": "One"}, {"id": "m2", "name": "Two"}],
        },
        "efforts": {"current": "low", "available": ["low"]},
    }
    result = _check(workdir, monkeypatch, "fake:m2", script)
    assert result.exit_code == 0, result.output
    assert "models: m1, m2 (current m2)" in result.output
    # Those efforts belong to m1; the legacy setter reports none for m2.
    assert "efforts for" not in result.output


def test_check_with_a_bad_model_is_a_selection_error(workdir, monkeypatch):
    """An unoffered model fails as a selection error, not as a login problem.

    :param workdir: isolated working directory fixture.
    :param monkeypatch: pytest monkeypatch fixture.
    """
    result = _check(workdir, monkeypatch, "fake:m9")
    assert result.exit_code == 1
    assert "'m9' is not offered" in result.output
    assert "log in with the agent's own CLI" not in result.output


def test_check_with_a_bad_effort_names_the_models_own_choices(workdir, monkeypatch):
    """An effort the chosen model drops fails and lists what it does offer.

    :param workdir: isolated working directory fixture.
    :param monkeypatch: pytest monkeypatch fixture.
    """
    result = _check(workdir, monkeypatch, "fake:m2:low")
    assert result.exit_code == 1
    assert "'low' is not offered for model 'm2'" in result.output
    assert "high, max" in result.output


def test_check_rejects_a_malformed_suffix(workdir, monkeypatch):
    """A fourth segment is a usage error, not a spawn attempt.

    :param workdir: isolated working directory fixture.
    :param monkeypatch: pytest monkeypatch fixture.
    """
    result = _check(workdir, monkeypatch, "fake:m2:high:max")
    assert result.exit_code == 2
    assert "malformed harness" in result.output


def test_check_tells_the_user_to_install_a_missing_agent(workdir):
    """A missing CLI must say so, and say that installing it is the user's job.

    :param workdir: isolated working directory fixture.
    """
    result = CliRunner().invoke(app, ["harness", "check", "goose"])
    assert result.exit_code == 1
    assert "not found on PATH" in result.output
    assert "install this agent yourself" in result.output


def test_check_reports_a_logged_out_agent_and_its_auth_methods(workdir):
    """A logged-out agent points at the agent's own login, not at ours.

    :param workdir: isolated working directory fixture.
    """
    script = json.dumps(
        {
            "turns": [],
            "auth_methods": [{"id": "oauth", "name": "OAuth"}],
            "fail_session": "not authenticated",
        }
    )
    result = CliRunner().invoke(
        app,
        ["harness", "check", "--cmd", _fake_cmd(script)],
    )
    assert result.exit_code == 1
    assert "needs a login" in result.output
    assert "oauth" in result.output
    assert "log in with the agent's own CLI" in result.output


def test_check_rejects_an_unknown_harness_name(workdir):
    """An unregistered name is a clear error, not a spawn attempt.

    :param workdir: isolated working directory fixture.
    """
    result = CliRunner().invoke(app, ["harness", "check", "not-a-real-agent"])
    assert result.exit_code == 1
    assert "unknown harness" in result.output


def test_check_requires_exactly_one_target(workdir):
    """Passing both a name and --cmd, or neither, is a usage error.

    :param workdir: isolated working directory fixture.
    """
    assert CliRunner().invoke(app, ["harness", "check"]).exit_code == 2
    both = CliRunner().invoke(app, ["harness", "check", "gemini", "--cmd", "x"])
    assert both.exit_code == 2


def test_sync_writes_the_snapshot_to_the_global_dir(workdir, monkeypatch):
    """Verify `harness sync` persists the fetched registry where runs read it.

    :param workdir: isolated working directory fixture.
    :param monkeypatch: pytest monkeypatch fixture.
    """
    snapshot = {
        "source": "test",
        "version": "1.0.0",
        "agents": [
            {
                "id": "brand-new",
                "name": "Brand New",
                "version": "0.1.0",
                "description": "d",
                "distribution": {"npx": {"package": "brand-new@0.1.0"}},
            }
        ],
    }
    monkeypatch.setattr(
        "flow_atelier.cli.commands.harness.fetch_registry", lambda: snapshot
    )
    result = CliRunner().invoke(app, ["harness", "sync"])
    assert result.exit_code == 0, result.output
    assert "brand-new" in result.output
    written = workdir / "global" / acp_registry.SNAPSHOT_FILENAME
    assert json.loads(written.read_text())["agents"][0]["id"] == "brand-new"
    # The synced snapshot is what the next run resolves against.
    assert list(acp_registry.load_registry(written)) == ["brand-new"]


def test_sync_failure_exits_non_zero_without_writing(workdir, monkeypatch):
    """Verify a failed download leaves the existing snapshot untouched.

    :param workdir: isolated working directory fixture.
    :param monkeypatch: pytest monkeypatch fixture.
    """
    def boom() -> dict:
        """Simulate an unreachable registry endpoint."""
        raise URLError("offline")

    monkeypatch.setattr("flow_atelier.cli.commands.harness.fetch_registry", boom)
    result = CliRunner().invoke(app, ["harness", "sync"])
    assert result.exit_code == 1
    assert "sync failed" in result.output
    assert not (workdir / "global" / acp_registry.SNAPSHOT_FILENAME).exists()
