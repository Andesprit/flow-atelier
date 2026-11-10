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


def test_init_is_noop_when_hello_conduit_present(workdir):
    """Running init when the hello conduit already exists is a no-op."""
    CliRunner().invoke(app, ["init"])
    original = _conduit_file(workdir).read_text()
    result = CliRunner().invoke(app, ["init"])
    assert result.exit_code == 0
    assert "already set up" in result.stdout
    assert _conduit_file(workdir).read_text() == original


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
    assert not (workdir / ".atelier" / "conduits" / "explain").exists()
    assert "install Claude Code or Codex" in result.stdout


def test_init_with_one_agent_writes_a_single_look(workdir, monkeypatch):
    """One agent CLI yields a one-task explain conduit on that agent."""
    _fake_clis(workdir, monkeypatch, "codex")
    result = CliRunner().invoke(app, ["init"])
    assert result.exit_code == 0
    conduit = _explain(workdir)
    assert [t.tool for t in conduit.tasks] == ["harness:codex"]
    assert "atelier run explain" in result.stdout


def test_init_with_two_agents_runs_them_in_parallel_then_merges(workdir, monkeypatch):
    """Two agent CLIs look in parallel and the preferred one merges."""
    _fake_clis(workdir, monkeypatch, "codex", "claude", "gemini")
    CliRunner().invoke(app, ["init"])
    tools = {t.name: t.tool for t in _explain(workdir).tasks}
    assert tools == {
        "look_claude_code": "harness:claude-code",
        "look_codex": "harness:codex",
        "merge": "harness:claude-code",
    }


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


def test_init_with_one_agent_pairs_it_with_itself(workdir, monkeypatch):
    """One agent CLI plays worker, supervisor and reviewer."""
    _fake_clis(workdir, monkeypatch, "codex")
    CliRunner().invoke(app, ["init"])
    conduit = _pair(workdir)
    assert conduit.interaction.supervisor.tool == "harness:codex"
    assert {t.tool for t in conduit.tasks} == {"harness:codex"}


def test_init_without_agents_writes_no_pair(workdir, monkeypatch):
    """With no agent CLI on PATH, init writes no pair conduit."""
    _fake_clis(workdir, monkeypatch)
    CliRunner().invoke(app, ["init"])
    assert not (workdir / ".atelier" / "conduits" / "pair").exists()
