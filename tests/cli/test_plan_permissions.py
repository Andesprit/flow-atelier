"""`atelier plan` shows what each agent task may do without asking."""
from __future__ import annotations

import json
import os

import pytest
from typer.testing import CliRunner

from flow_atelier.cli import app

CONDUIT = """\
name: perm
description: d
tasks:
  - name: build
    description: d
    task: x
    tool: harness:claude-code
  - name: review
    description: d
    task: x
    tool: harness:codex
    permissions: read-only
  - name: test
    description: d
    task: make test
    tool: tool:bash
"""


@pytest.fixture
def workdir(tmp_path, monkeypatch):
    """Isolated cwd holding the ``perm`` conduit, with no ATELIER_* env.

    :param tmp_path: pytest temp directory fixture.
    :param monkeypatch: pytest monkeypatch fixture.
    """
    cdir = tmp_path / ".atelier" / "conduits" / "perm"
    cdir.mkdir(parents=True)
    (cdir / "conduit.yaml").write_text(CONDUIT)
    global_dir = tmp_path / "global"
    (global_dir / "conduits").mkdir(parents=True)
    monkeypatch.chdir(tmp_path)
    for k in list(os.environ):
        if k.startswith("ATELIER_"):
            monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("ATELIER_GLOBAL_ATELIER_DIR", str(global_dir))
    monkeypatch.setenv("ATELIER_NO_UPDATE_CHECK", "1")
    return tmp_path


def _levels() -> dict[str, str | None]:
    """Return each task's planned permission level from ``plan --json``."""
    result = CliRunner().invoke(app, ["plan", "perm", "--json"])
    assert result.exit_code == 0, result.output
    plan = json.loads(result.stdout)
    return {t["name"]: t["permissions"] for wave in plan["waves"] for t in wave}


def test_plan_shows_the_level_of_each_agent_task(workdir):
    """Unset is full; a task's own level wins; a shell step has none."""
    assert _levels() == {"build": "full", "review": "read-only", "test": None}
    text = CliRunner().invoke(app, ["plan", "perm"]).stdout
    assert "permissions: full" in text
    assert "permissions: read-only" in text


def test_plan_reflects_the_global_setting(workdir, monkeypatch):
    """ATELIER_AGENT_PERMISSIONS applies to tasks that name no level."""
    monkeypatch.setenv("ATELIER_AGENT_PERMISSIONS", "edits")
    assert _levels() == {"build": "edits", "review": "read-only", "test": None}
