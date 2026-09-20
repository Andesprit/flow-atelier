"""CLI tests: `atelier run --again` re-runs a past flow reusing its inputs."""
from __future__ import annotations

import os

import pytest
from typer.testing import CliRunner

from flow_atelier.cli import app


@pytest.fixture
def workdir(tmp_path, monkeypatch):
    """Isolated cwd with a one-task bash conduit and isolated global dir.

    :param tmp_path: pytest temp directory fixture.
    :param monkeypatch: pytest monkeypatch fixture.
    """
    cdir = tmp_path / ".atelier" / "conduits" / "hello"
    cdir.mkdir(parents=True)
    (cdir / "conduit.yaml").write_text(
        "name: hello\ndescription: say hi\n"
        "inputs:\n  msg:\n    description: what to echo\n    default: hi\n"
        "tasks:\n  - greet:\n      description: greet\n"
        '      task: "echo {{ inputs.msg }}"\n'
        "      tool: tool:bash\n      depends_on: []\n"
    )
    global_dir = tmp_path / "global"
    (global_dir / "conduits").mkdir(parents=True)
    monkeypatch.chdir(tmp_path)
    for k in list(os.environ):
        if k.startswith("ATELIER_"):
            monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("ATELIER_GLOBAL_ATELIER_DIR", str(global_dir))
    monkeypatch.setenv("ATELIER_NO_UPDATE_CHECK", "1")
    return tmp_path


def _flow_id(stdout: str) -> str:
    """Extract the printed flow_id from run command stdout.

    :param stdout: captured CLI stdout.
    :returns: the flow id following the ``flow_id:`` label.
    """
    for line in stdout.splitlines():
        if "flow_id:" in line:
            return line.split("flow_id:", 1)[1].strip()
    raise AssertionError(f"no flow_id in output:\n{stdout}")


def test_run_again_starts_new_flow(workdir):
    """`run --again <id>` starts a fresh flow with a new id and start banner."""
    runner = CliRunner()
    first = runner.invoke(app, ["run", "hello", "-i", "msg=one"])
    assert first.exit_code == 0, first.stdout
    src = _flow_id(first.stdout)

    again = runner.invoke(app, ["run", "--again", src])
    assert again.exit_code == 0, again.stdout
    assert "starting flow" in again.stdout
    assert _flow_id(again.stdout) != src


def test_run_again_resolves_prefix(workdir):
    """`run --again <prefix>` resolves a unique id prefix."""
    runner = CliRunner()
    first = runner.invoke(app, ["run", "hello", "-i", "msg=one"])
    src = _flow_id(first.stdout)

    again = runner.invoke(app, ["run", "--again", src[:17]])
    assert again.exit_code == 0, again.stdout
    assert _flow_id(again.stdout) != src


def test_run_again_with_override(workdir):
    """`--again` plus `--input k=v` overrides only that key in the new flow."""
    runner = CliRunner()
    first = runner.invoke(app, ["run", "hello", "-i", "msg=one"])
    src = _flow_id(first.stdout)

    again = runner.invoke(app, ["run", "--again", src, "-i", "msg=two"])
    assert again.exit_code == 0, again.stdout
    new_id = _flow_id(again.stdout)
    from flow_atelier.core.atelier import Atelier

    assert Atelier().store.read_input(new_id).get("msg") == "two"


def test_run_again_conflicts_with_resume(workdir):
    """`--resume X --again Y` exits non-zero with the conflict error."""
    result = CliRunner().invoke(
        app, ["run", "--resume", "X", "--again", "Y"]
    )
    assert result.exit_code == 2
    assert "mutually exclusive" in result.stdout
