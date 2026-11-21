"""Inspect an empty project with the default update-check behavior."""
import json

import pytest
from typer.testing import CliRunner

from flow_atelier.cli.main import app


@pytest.mark.parametrize(("args", "code", "message"), [
    (["list"], 0, "no conduits found"),
    (["list", "flows"], 0, "no flows found"),
    (["list", "schedules"], 0, "no schedules found"),
    (["check"], 0, "no conduits found"),
    (["--require-read-only-stores", "check", "--json"], 0, "[]"),
    (["show", "missing"], 1, "unknown conduit"),
    (["--require-read-only-stores", "show", "missing"], 1, "unknown conduit"),
    (["status", "latest"], 1, "no flows recorded"),
    (["diagnose", "latest"], 1, "no flows recorded"),
])
def test_inspection_leaves_fresh_project_and_home_alone(tmp_path, monkeypatch, args, code, message):
    project = tmp_path / "project"
    project.mkdir()
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.chdir(project)
    monkeypatch.setenv("ATELIER_ATELIER_DIR", ".atelier")
    monkeypatch.setenv("ATELIER_GLOBAL_ATELIER_DIR", str(home / ".atelier"))
    monkeypatch.delenv("ATELIER_NO_UPDATE_CHECK", raising=False)
    result = CliRunner().invoke(app, args)
    assert result.exit_code == code, result.output
    assert message in result.output
    assert list(project.iterdir()) == list(home.iterdir()) == []


def test_inspect_global_conduit_without_initializing_project(tmp_path, monkeypatch):
    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.chdir(project)
    shared = tmp_path / "shared"
    monkeypatch.setenv("ATELIER_ATELIER_DIR", ".atelier")
    monkeypatch.setenv("ATELIER_GLOBAL_ATELIER_DIR", str(shared))
    monkeypatch.delenv("ATELIER_NO_UPDATE_CHECK", raising=False)
    recipe = shared / "conduits/hello/conduit.yaml"
    recipe.parent.mkdir(parents=True)
    recipe.write_text("name: hello\ndescription: Hello\ntasks:\n  - hi:\n"
                      "      description: Say hi\n      tool: tool:bash\n      task: echo hi\n")
    before = sorted(tmp_path.rglob("*"))
    for command in (["check", "hello"], ["show", "hello"], ["list"]):
        result = CliRunner().invoke(app, command)
        assert result.exit_code == 0, result.output
        assert "global" in result.output
        assert sorted(tmp_path.rglob("*")) == before


@pytest.mark.parametrize("kind", ["conduits", "flows", "schedules"])
def test_unusable_store_reports_the_path(tmp_path, monkeypatch, kind):
    # A file where a directory belongs fails reliably even under privileged CI.
    monkeypatch.setenv("ATELIER_ATELIER_DIR", str(tmp_path))
    path = tmp_path / kind
    path.write_text("preserve me")
    result = CliRunner().invoke(app, ["list", kind, "--json"])
    assert result.exit_code == 1
    assert result.stdout == ""
    error = json.loads(result.stderr)["error"]
    assert error["code"] == "filesystem_error"
    assert repr(str(path)) in error["message"]
    assert path.read_text() == "preserve me"
    if kind == "conduits":
        result = CliRunner().invoke(app, ["list"])
        assert result.exit_code == 1
        assert repr(str(path)) in result.stderr
