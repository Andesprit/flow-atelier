"""CLI tests for `atelier init`: idempotency and the AI starter."""
from __future__ import annotations

import os

import pytest
import yaml
from typer.testing import CliRunner

from flow_atelier.cli import app
from flow_atelier.schemas.conduit import Conduit


@pytest.fixture
def workdir(tmp_path, monkeypatch):
    """Isolated cwd with no `.atelier` tree and update checks disabled.

    :param tmp_path: pytest temp directory fixture.
    :param monkeypatch: pytest monkeypatch fixture.
    """
    monkeypatch.chdir(tmp_path)
    for k in list(os.environ):
        if k.startswith("ATELIER_"):
            monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("ATELIER_NO_UPDATE_CHECK", "1")
    return tmp_path


def _conduit_file(workdir):
    """Return the hello conduit path under ``workdir``."""
    return workdir / ".atelier" / "conduits" / "hello" / "conduit.yaml"


def test_init_scaffolds_in_empty_dir(workdir):
    """init creates the hello conduit when nothing exists yet."""
    result = CliRunner().invoke(app, ["init"])
    assert result.exit_code == 0
    assert _conduit_file(workdir).exists()


def test_init_fills_in_when_atelier_exists_without_hello(workdir):
    """A pre-existing .atelier/ (without the hello conduit) is filled in."""
    (workdir / ".atelier" / "conduits").mkdir(parents=True)
    assert not _conduit_file(workdir).exists()
    result = CliRunner().invoke(app, ["init"])
    assert result.exit_code == 0
    assert _conduit_file(workdir).exists()


def test_init_preserves_existing_starters(workdir, monkeypatch):
    """Running init again preserves edits to every starter."""
    _fake_clis(workdir, monkeypatch, "codex")
    assert CliRunner().invoke(app, ["init"]).exit_code == 0
    paths = [workdir / ".atelier" / "conduits" / name / "conduit.yaml"
             for name in ("hello", "explain", "pair")]
    for path in paths:
        path.write_text("# customized\n")
    result = CliRunner().invoke(app, ["init"])
    assert result.exit_code == 0
    assert "already set up" in result.stdout
    assert all(path.read_text() == "# customized\n" for path in paths)


def _fake_clis(tmp_path, monkeypatch, *names):
    """Put executables named ``names`` on an otherwise empty PATH.

    :param tmp_path: pytest temp directory fixture.
    :param monkeypatch: pytest monkeypatch fixture.
    :param names: CLI names to provide.
    """
    bin_dir = tmp_path / f"bin-{'-'.join(names)}"
    bin_dir.mkdir()
    for name in names:
        # The .cmd twin is what PATH lookup finds on Windows.
        for exe in (bin_dir / name, bin_dir / f"{name}.cmd"):
            exe.write_text("#!/bin/sh\n")
            exe.chmod(0o755)
    monkeypatch.setenv("PATH", str(bin_dir))


def _explain(workdir):
    """Return the parsed explain conduit under ``workdir``."""
    path = workdir / ".atelier" / "conduits" / "explain" / "conduit.yaml"
    return Conduit.model_validate(yaml.safe_load(path.read_text()))


def test_init_without_agents_writes_only_hello(workdir, monkeypatch):
    """With no agent CLI on PATH, init writes hello and says how to get more."""
    _fake_clis(workdir, monkeypatch)
    result = CliRunner().invoke(app, ["init"])
    assert result.exit_code == 0
    assert [p.name for p in (workdir / ".atelier" / "conduits").iterdir()] == ["hello"]
    assert "install Claude Code or Codex" in result.stdout


@pytest.mark.parametrize(("clis", "reader"), [
    (["codex"], "codex"),
    (["codex", "claude"], "claude-code"),
])
def test_init_writes_one_read_only_look(workdir, monkeypatch, clis, reader):
    """Explain uses one preferred reader regardless of other installed CLIs."""
    _fake_clis(workdir, monkeypatch, *clis)
    result = CliRunner().invoke(app, ["init"])
    assert result.exit_code == 0
    conduit = _explain(workdir)
    look, = conduit.tasks
    assert (look.name, look.tool, look.permissions) == ("look", f"harness:{reader}", "read-only")
    assert "atelier run explain" in result.stdout
    assert "atelier serve --no-scheduler" in result.stdout


def test_init_adds_explain_to_an_existing_setup(workdir, monkeypatch):
    """A project set up before an agent was installed gains the AI starter."""
    _fake_clis(workdir, monkeypatch)
    CliRunner().invoke(app, ["init"])
    _fake_clis(workdir, monkeypatch, "claude")
    result = CliRunner().invoke(app, ["init"])
    assert "already set up" not in result.stdout
    assert [t.tool for t in _explain(workdir).tasks] == ["harness:claude-code"]


def _pair(workdir):
    """Return the parsed pair conduit under ``workdir``."""
    path = workdir / ".atelier" / "conduits" / "pair" / "conduit.yaml"
    return Conduit.model_validate(yaml.safe_load(path.read_text()))


def test_init_with_two_agents_pairs_them(workdir, monkeypatch):
    """The preferred agent works in one session; the other supervises and reviews."""
    _fake_clis(workdir, monkeypatch, "codex", "claude")
    result = CliRunner().invoke(app, ["init"])
    assert "atelier run pair" in result.stdout
    conduit = _pair(workdir)
    assert conduit.interaction.questions == "hybrid"
    assert conduit.interaction.supervisor.tool == "harness:codex"
    tasks = {t.name: (t.tool, t.session) for t in conduit.tasks}
    assert tasks == {
        "draft": ("harness:claude-code", "worker"),
        "review": ("harness:codex", None),
        "revise": ("harness:claude-code", "worker"),
    }
    assert conduit.inputs["goal"].default
    assert all(t.permissions == "read-only" for t in conduit.tasks)


@pytest.mark.parametrize("others", [(), ("gemini", "opencode")])
def test_init_with_one_agent_pairs_it_with_itself(workdir, monkeypatch, others):
    """One agent CLI plays worker, supervisor and reviewer."""
    _fake_clis(workdir, monkeypatch, "codex", *others)
    CliRunner().invoke(app, ["init"])
    conduit = _pair(workdir)
    assert conduit.interaction.supervisor.tool == "harness:codex"
    assert {t.tool for t in conduit.tasks} == {"harness:codex"}
    assert all(t.permissions == "read-only" for t in conduit.tasks)


def test_unsupported_agents_report_missing_read_only_harness(workdir, monkeypatch):
    _fake_clis(workdir, monkeypatch, "gemini", "opencode")
    for _ in range(2):
        result = CliRunner().invoke(app, ["init"])
        assert result.exit_code == 0
        assert "No installed harness can enforce read-only" in result.stdout
        assert "install Claude Code or Codex" in result.stdout
        assert [p.name for p in _conduit_file(workdir).parents[1].iterdir()] == ["hello"]
