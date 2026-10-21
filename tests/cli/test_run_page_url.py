"""CLI tests: flow-running commands print the run's page URL."""
from __future__ import annotations

import http.client
import importlib
import io
import json
import os
from unittest.mock import Mock

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
    monkeypatch.setattr(http.client, "HTTPConnection", Mock(side_effect=ConnectionRefusedError))
    return tmp_path


@pytest.fixture
def matching_server(monkeypatch):
    """An HTTP boundary stub that returns the requested flow, without a live listener."""
    connection = Mock()
    connection.request.side_effect = lambda method, path, **kw: setattr(connection, "path", path)

    def response():
        body = io.BytesIO(json.dumps({"flow_id": connection.path.rsplit("/", 1)[-1]}).encode())
        body.status = 200
        return body

    connection.getresponse.side_effect = response
    monkeypatch.setattr(http.client, "HTTPConnection", Mock(return_value=connection))
    return connection


def _flow_id(stdout: str) -> str:
    """Extract the printed flow_id from run command stdout.

    :param stdout: captured CLI stdout.
    :returns: the flow id following the ``flow_id:`` label.
    """
    for line in stdout.splitlines():
        if "flow_id:" in line:
            return line.split("flow_id:", 1)[1].strip()
    raise AssertionError(f"no flow_id in output:\n{stdout}")


def test_run_prints_page_url(workdir, matching_server):
    """A fresh run names its page on the default serve address, on one line."""
    result = CliRunner().invoke(app, ["run", NAME])
    assert result.exit_code == 0, result.stdout
    fid = _flow_id(result.stdout)
    assert f"· run page http://127.0.0.1:8000/runs/{fid}\n" in result.stdout


def test_page_url_follows_serve_url(workdir, monkeypatch, matching_server):
    """ATELIER_SERVE_URL moves the page; a trailing slash is not doubled."""
    monkeypatch.setenv("ATELIER_SERVE_URL", "http://localhost:9123/")
    result = CliRunner().invoke(app, ["run", NAME])
    assert result.exit_code == 0, result.stdout
    fid = _flow_id(result.stdout)
    assert f"· run page http://localhost:9123/runs/{fid}\n" in result.stdout


def test_resume_prints_page_url(workdir, matching_server):
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


def test_nested_run_names_only_the_top_flow(workdir, matching_server):
    """A tool:conduit child neither gets its own page line nor becomes the resume target."""
    conduits = workdir / ".atelier" / "conduits"
    (conduits / "child").mkdir()
    (conduits / "child" / "conduit.yaml").write_text(
        "name: child\ndescription: fails\n"
        "tasks:\n  - boom:\n      description: boom\n"
        "      task: exit 1\n      tool: tool:bash\n      depends_on: []\n"
    )
    (conduits / "parent").mkdir()
    (conduits / "parent" / "conduit.yaml").write_text(
        "name: parent\ndescription: nests\n"
        "tasks:\n  - nest:\n      description: nest\n"
        "      task: child\n      tool: tool:conduit\n      depends_on: []\n"
    )
    result = CliRunner().invoke(app, ["run", "parent"])
    assert result.exit_code == 1, result.stdout
    fid = _flow_id(result.stdout)
    assert fid.endswith("_parent")
    assert result.stdout.count("· run page ") == 1
    assert f"· run page http://127.0.0.1:8000/runs/{fid}\n" in result.stdout
    assert f"atelier run --resume {fid}" in result.stdout


@pytest.mark.parametrize("mode", ["fresh", "again", "resume"])
@pytest.mark.parametrize("online", [False, True])
def test_all_run_modes_check_page_availability(workdir, monkeypatch, mode, online):
    run_module = importlib.import_module("flow_atelier.cli.commands.run")
    checked = []

    def available(base, fid, token):
        checked.append(fid)
        return online

    monkeypatch.setattr(run_module, "_run_page_available", available)
    runner = CliRunner()
    first = runner.invoke(app, ["run", NAME])
    fid = _flow_id(first.stdout)
    if mode == "resume":
        conduit = workdir / ".atelier" / "conduits" / NAME / "conduit.yaml"
        conduit.write_text(conduit.read_text().replace("echo hi", "exit 1"))
        first = runner.invoke(app, ["run", NAME])
        fid = _flow_id(first.stdout)
    checked.clear()
    result = first if mode == "fresh" else runner.invoke(app, ["run", f"--{mode}", fid])
    assert result.exit_code == (1 if mode == "resume" else 0), result.stdout
    if mode != "fresh":
        assert checked == [_flow_id(result.stdout)]
    if online:
        assert "· run page http://" in result.stdout
    else:
        assert "· run page http://" not in result.stdout
        assert "atelier serve --port 0" in result.stdout
        assert "printed address" in result.stdout


def test_offline_run_has_one_unwrapped_actionable_hint(workdir):
    result = CliRunner().invoke(app, ["run", NAME])
    assert result.exit_code == 0, result.stdout
    hint = next(line for line in result.stdout.splitlines() if "run page" in line)
    assert "no atelier serve for this project" in hint
    assert "atelier serve --port 0" in hint
    assert "printed address" in hint
    assert "http://127.0.0.1:8000/runs/" not in hint


@pytest.mark.parametrize("status,payload", [
    (200, b'{"flow_id": "wanted"}'),
    (200, b'{"flow_id": "foreign"}'),
    (200, b'[]'),
    (200, b'not JSON'),
    (302, b'{"flow_id": "wanted"}'),
    (404, b'{"detail": "not found"}'),
    (401, b'{"detail": "unauthorized"}'),
])
def test_page_requires_matching_flow_json_without_redirects(monkeypatch, status, payload):
    connection = Mock()
    body = io.BytesIO(payload)
    body.status = status
    connection.getresponse.return_value = body
    factory = Mock(return_value=connection)
    monkeypatch.setattr(http.client, "HTTPConnection", factory)
    run_module = importlib.import_module("flow_atelier.cli.commands.run")
    expected = status == 200 and b'"wanted"' in payload
    assert run_module._run_page_available("http://localhost:9123/prefix/", "wanted", "fixture") == expected
    connection.request.assert_called_once_with(
        "GET", "/prefix/flows/wanted", headers={"Authorization": "Bearer fixture"},
    )
    factory.assert_called_once_with("localhost", 9123, timeout=0.1)
    connection.sock.settimeout.assert_called_once_with(0.5)
    connection.close.assert_called_once()


@pytest.mark.parametrize("url", ["bad-url", "http://[broken", "ftp://localhost", "http://localhost:bad"])
def test_bad_serve_url_cannot_break_a_run(workdir, monkeypatch, url):
    monkeypatch.setenv("ATELIER_SERVE_URL", url)
    result = CliRunner().invoke(app, ["run", NAME])
    assert result.exit_code == 0, result.stdout
    assert "atelier serve --port 0" in result.stdout
