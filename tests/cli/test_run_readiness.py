"""CLI test: `atelier run` refuses an unrunnable conduit before starting."""
from __future__ import annotations

import os

import pytest
from typer.testing import CliRunner

from flow_atelier.cli import app


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


def test_run_refuses_unrunnable_conduit(workdir, monkeypatch):
    """`run` exits 1 with the readiness message and never starts the flow."""
    monkeypatch.setattr(
        "flow_atelier.services.executor.harness.shutil.which",
        lambda _binary: None,
    )
    cdir = workdir / ".atelier" / "conduits" / "agentic"
    cdir.mkdir(parents=True)
    (cdir / "conduit.yaml").write_text(
        "name: agentic\ndescription: d\n"
        "tasks:\n  - name: build\n    description: b\n    task: do it\n    tool: harness:claude-code\n"
    )
    result = CliRunner().invoke(app, ["run", "agentic"])
    assert result.exit_code == 1
    assert "cannot run" in result.stdout
    assert "starting flow" not in result.stdout


def test_unknown_agent_names_the_discovery_command(workdir):
    cdir = workdir / ".atelier/conduits/agentic"
    cdir.mkdir()
    (cdir / "conduit.yaml").write_text(
        "name: agentic\ndescription: d\ntasks:\n"
        "- build:\n    description: build\n    task: hi\n    tool: harness:nosuch\n"
    )
    result = CliRunner().invoke(app, ["run", "agentic"])
    assert result.exit_code == 1
    assert "atelier harness list" in result.stdout
    assert "starting flow" not in result.stdout
