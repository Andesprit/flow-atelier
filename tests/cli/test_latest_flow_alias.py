"""Tests for the ``latest`` flow-id alias accepted by every flow command."""
from __future__ import annotations

import json
import os

import pytest
from typer.testing import CliRunner

from flow_atelier.cli import app
from flow_atelier.core.atelier import Atelier

CONDUIT_YAML = """
name: hello
description: Say hello
tasks:
  - greet:
      description: greet
      task: "echo hello"
      tool: tool:bash
      depends_on: []
"""

# Id order says `bbbb` is newer; started_at says `aaaa` is. `latest` must
# follow started_at.
OLDER = "20260101_bbbbbbbb_hello"
NEWER = "20260101_aaaaaaaa_hello"


@pytest.fixture
def workdir(tmp_path, monkeypatch):
    """Provide an isolated working directory seeded with the hello conduit.

    :param tmp_path: pytest temp directory fixture.
    :param monkeypatch: pytest monkeypatch fixture.
    """
    atelier_dir = tmp_path / ".atelier"
    (atelier_dir / "conduits" / "hello").mkdir(parents=True)
    (atelier_dir / "conduits" / "hello" / "conduit.yaml").write_text(CONDUIT_YAML)
    monkeypatch.chdir(tmp_path)
    for k in list(os.environ):
        if k.startswith("ATELIER_") and k not in (
            "ATELIER_GLOBAL_ATELIER_DIR",
            "ATELIER_NO_UPDATE_CHECK",
        ):
            monkeypatch.delenv(k, raising=False)
    return tmp_path


def _seed_flow(atelier: Atelier, flow_id: str, started_at: str, output: str) -> None:
    """Create a finished-looking flow with a fixed start time and one output.

    :param atelier: Atelier facade rooted at the test workdir.
    :param flow_id: explicit flow id to create.
    :param started_at: ISO timestamp to record as the flow's start.
    :param output: value saved as the ``greet`` task output.
    """
    atelier.store.create_flow("hello", {}, flow_id=flow_id)
    progress = atelier.store.read_progress(flow_id)
    progress.started_at = started_at
    atelier.store.write_progress(flow_id, progress)
    atelier.store.write_outputs(flow_id, {"greet": output})


@pytest.fixture
def two_flows(workdir):
    """Seed an older and a newer flow whose id order disagrees with start order."""
    atelier = Atelier()
    _seed_flow(atelier, OLDER, "2026-01-01T10:00:00Z", "older")
    _seed_flow(atelier, NEWER, "2026-01-01T12:00:00Z", "newer")


def test_latest_picks_newest_started_at_not_highest_id(two_flows):
    """`outputs latest --task` prints only the newest flow's value on stdout."""
    result = CliRunner().invoke(app, ["outputs", "latest", "--task", "greet"])
    assert result.exit_code == 0, result.output
    assert result.stdout == "newer\n"
    assert f"latest: {NEWER}" in result.stderr


def test_latest_keeps_json_stdout_clean(two_flows):
    """`status latest --json` stays parseable: the alias note goes to stderr."""
    result = CliRunner().invoke(app, ["status", "latest", "--json"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["flow_id"] == NEWER


def test_latest_with_no_flows_exits_1(workdir):
    """`latest` with nothing recorded is an unknown flow, not a crash."""
    result = CliRunner().invoke(app, ["status", "latest"])
    assert result.exit_code == 1
    assert "unknown flow" in result.output
    assert "no flows recorded yet" in result.output


def test_unknown_flow_hints_list_flows_and_latest(two_flows):
    """Passing a conduit name where a flow id belongs points at the fix."""
    result = CliRunner().invoke(app, ["logs", "hello"])
    assert result.exit_code == 1
    assert "unknown flow" in result.output
    assert "try 'atelier list flows' or 'latest'" in result.output


def test_real_id_and_prefix_still_resolve(two_flows):
    """The alias does not disturb exact-id and unique-prefix resolution."""
    runner = CliRunner()
    exact = runner.invoke(app, ["outputs", OLDER, "--task", "greet"])
    assert exact.exit_code == 0, exact.output
    assert exact.stdout == "older\n"
    prefix = runner.invoke(app, ["outputs", "20260101_a", "--task", "greet"])
    assert prefix.exit_code == 0, prefix.output
    assert prefix.stdout == "newer\n"
    assert "latest:" not in exact.stderr + prefix.stderr
