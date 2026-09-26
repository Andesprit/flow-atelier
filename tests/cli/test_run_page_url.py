"""CLI tests: flow-running commands print the run's page URL."""
from __future__ import annotations

import os

import pytest
from typer.testing import CliRunner

from flow_atelier.cli import app

# Long enough that the page line passes Rich's 80-column default for pipes.
NAME = "a-conduit-name-long-enough-to-wrap-the-page-line"


@pytest.fixture
def workdir(tmp_path, monkeypatch):
    """Isolated cwd with a one-task bash conduit and isolated global dir.

    :param tmp_path: pytest temp directory fixture.
    :param monkeypatch: pytest monkeypatch fixture.
    """
    cdir = tmp_path / ".atelier" / "conduits" / NAME
    cdir.mkdir(parents=True)
    (cdir / "conduit.yaml").write_text(
        f"name: {NAME}\ndescription: say hi\n"
        "tasks:\n  - greet:\n      description: greet\n"
        "      task: echo hi\n      tool: tool:bash\n      depends_on: []\n"
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


def test_run_prints_page_url(workdir):
    """A fresh run names its page on the default serve address, on one line."""
    result = CliRunner().invoke(app, ["run", NAME])
    assert result.exit_code == 0, result.stdout
    fid = _flow_id(result.stdout)
    assert f"· run page http://127.0.0.1:8000/runs/{fid}\n" in result.stdout


def test_page_url_follows_serve_url(workdir, monkeypatch):
    """ATELIER_SERVE_URL moves the page; a trailing slash is not doubled."""
    monkeypatch.setenv("ATELIER_SERVE_URL", "http://localhost:9123/")
    result = CliRunner().invoke(app, ["run", NAME])
    assert result.exit_code == 0, result.stdout
    fid = _flow_id(result.stdout)
    assert f"· run page http://localhost:9123/runs/{fid}\n" in result.stdout


def test_resume_prints_page_url(workdir):
    """A resumed run names the same page, though the engine does not announce it."""
    conduit = workdir / ".atelier" / "conduits" / NAME / "conduit.yaml"
    conduit.write_text(conduit.read_text().replace("echo hi", "exit 1"))
    runner = CliRunner()
    first = runner.invoke(app, ["run", NAME])
    assert first.exit_code == 1, first.stdout
    fid = _flow_id(first.stdout)
    resumed = runner.invoke(app, ["run", "--resume", fid])
    assert "resuming" in resumed.stdout
    assert f"· run page http://127.0.0.1:8000/runs/{fid}\n" in resumed.stdout
