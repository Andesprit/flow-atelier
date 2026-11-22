"""CLI tests for `atelier init`: idempotency and the AI starter."""
from __future__ import annotations

import os
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from flow_atelier.cli import app
from flow_atelier.schemas.conduit import Conduit

V0141_STARTERS = Path(__file__).parent / "fixtures" / "init-v0.14.1"


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


@pytest.mark.parametrize(("variant", "newline", "clis"), [
    ("codex", b"\n", ()),
    ("dual", b"\r\n", ("claude", "codex")),
])
def test_init_upgrades_stock_v0141_starters(workdir, monkeypatch, variant, newline, clis):
    _fake_clis(workdir, monkeypatch, *clis)
    originals = {}
    for name in ("explain", "pair"):
        stock = (V0141_STARTERS / f"{name}-{variant}.yaml").read_text(encoding="ascii")
        path = _conduit_file(workdir).parents[1] / name / "conduit.yaml"
        path.parent.mkdir(parents=True)
        originals[path] = stock.encode("ascii").replace(b"\n", newline)
        path.write_bytes(originals[path])

    result = CliRunner().invoke(app, ["init"])
    assert result.exit_code == 0, result.output
    for path, original in originals.items():
        updated = path.read_bytes()
        assert updated.replace(b"    permissions: read-only" + newline, b"") == original
        conduit = Conduit.model_validate(yaml.safe_load(updated))
        assert all(task.permissions == "read-only" for task in conduit.tasks)
        assert f"upgraded {path.relative_to(workdir).as_posix()} to read-only" in result.output
    repeated = CliRunner().invoke(app, ["init"])
    assert repeated.exit_code == 0
    assert "already set up" in repeated.output
    assert "warning" not in repeated.output


@pytest.mark.parametrize("name", ["explain", "pair"])
def test_init_keeps_edited_v0141_starter_and_warns(workdir, monkeypatch, name):
    _fake_clis(workdir, monkeypatch, "codex")
    stock = (V0141_STARTERS / f"{name}-codex.yaml").read_bytes()
    path = _conduit_file(workdir).parents[1] / name / "conduit.yaml"
    path.parent.mkdir(parents=True)
    edited = b"# Keep my custom comment\n" + stock
    path.write_bytes(edited)

    result = CliRunner().invoke(app, ["init"])
    assert result.exit_code == 0, result.output
    assert path.read_bytes() == edited
    assert f"warning {path.relative_to(workdir).as_posix()}" in result.output
    assert (
        "Add or set 'permissions: read-only' on every agent task in this file."
        in " ".join(result.output.split())
    )
    assert f"atelier run {name}" not in result.output


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
