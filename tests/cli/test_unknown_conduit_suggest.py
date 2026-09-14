"""CLI tests: a mistyped conduit name gets a `did you mean` suggestion."""
from __future__ import annotations

import os

import pytest
from typer.testing import CliRunner

from flow_atelier.cli import app

HELLO = (
    "name: hello\ndescription: d\n"
    "tasks:\n  - name: a\n    description: a\n    task: echo hi\n    tool: tool:bash\n"
)


@pytest.fixture
def workdir(tmp_path, monkeypatch):
    """Isolated cwd with a project `hello` conduit and an isolated global dir.

    :param tmp_path: pytest temp directory fixture.
    :param monkeypatch: pytest monkeypatch fixture.
    """
    cdir = tmp_path / ".atelier" / "conduits" / "hello"
    cdir.mkdir(parents=True)
    (cdir / "conduit.yaml").write_text(HELLO)
    global_dir = tmp_path / "global"
    (global_dir / "conduits").mkdir(parents=True)
    monkeypatch.chdir(tmp_path)
    for k in list(os.environ):
        if k.startswith("ATELIER_"):
            monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("ATELIER_GLOBAL_ATELIER_DIR", str(global_dir))
    monkeypatch.setenv("ATELIER_NO_UPDATE_CHECK", "1")
    return tmp_path


@pytest.mark.parametrize("command", ["run", "check", "plan"])
def test_typo_suggests_close_conduit(workdir, command):
    """`<command> helo` exits 1 and points at `hello`."""
    result = CliRunner().invoke(app, [command, "helo"])
    assert result.exit_code == 1
    assert "unknown conduit" in result.output
    assert "did you mean: hello?" in result.output
    assert "Traceback" not in result.output


def test_no_close_match_prints_no_suggestion(workdir):
    """A name unlike any conduit keeps the old error and adds nothing."""
    result = CliRunner().invoke(app, ["run", "zzzz"])
    assert result.exit_code == 1
    assert "unknown conduit" in result.output
    assert "did you mean" not in result.output
