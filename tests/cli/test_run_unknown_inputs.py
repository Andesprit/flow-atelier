"""CLI tests: `atelier run` rejects an `--input` key the conduit cannot use."""
from __future__ import annotations

import os

import pytest
from typer.testing import CliRunner

from flow_atelier.cli import app
from flow_atelier.core.atelier import Atelier

DECLARED = (
    "name: hello\ndescription: d\n"
    "inputs:\n  name: Who to greet\n  tone:\n    description: t\n    default: warm\n"
    "tasks:\n  - name: greet\n    description: g\n"
    '    task: "echo {{inputs.name}} {{inputs.tone}}"\n    tool: tool:bash\n'
)

REFERENCED_ONLY = (
    "name: hello\ndescription: d\n"
    "tasks:\n  - name: greet\n    description: g\n"
    '    task: "echo {{inputs.name}}"\n    tool: tool:bash\n'
)

NO_INPUTS = (
    "name: plain\ndescription: d\n"
    "tasks:\n  - name: a\n    description: a\n    task: echo hi\n    tool: tool:bash\n"
)

NESTED = (
    "name: outer\ndescription: d\n"
    "tasks:\n  - name: call\n    description: c\n    task: hello\n    tool: tool:conduit\n"
    '    inputs:\n      name: "{{inputs.who}}"\n'
)


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


def _write(workdir, name: str, body: str) -> None:
    """Write a project conduit.yaml.

    :param workdir: working directory path.
    :param name: conduit name (also the folder name).
    :param body: full YAML document for the conduit.
    """
    cdir = workdir / ".atelier" / "conduits" / name
    cdir.mkdir(parents=True)
    (cdir / "conduit.yaml").write_text(body)


def test_typo_on_declared_key_is_rejected_before_start(workdir):
    """A near-miss of a declared key exits 1, names it, and starts no flow."""
    _write(workdir, "hello", DECLARED)
    result = CliRunner().invoke(app, ["run", "hello", "--input", "nmae=world"])
    assert result.exit_code == 1, result.output
    assert "unknown input: nmae" in result.output
    assert "did you mean name?" in result.output
    assert "hello accepts --input: name, tone" in result.output
    assert "loading conduit" not in result.output
    assert "starting flow" not in result.output
    assert Atelier().list_flows() == []


def test_typo_on_defaulted_key_is_rejected(workdir):
    """A typo in an optional key no longer silently runs with the default."""
    _write(workdir, "hello", DECLARED)
    result = CliRunner().invoke(
        app, ["run", "hello", "-i", "name=world", "-i", "tonee=cold"]
    )
    assert result.exit_code == 1, result.output
    assert "unknown input: tonee" in result.output
    assert "did you mean tone?" in result.output


def test_referenced_but_undeclared_key_is_accepted(workdir):
    """`{{inputs.name}}` without an `inputs:` block still takes `--input name`."""
    _write(workdir, "hello", REFERENCED_ONLY)
    result = CliRunner().invoke(app, ["run", "hello", "--input", "name=world"])
    assert result.exit_code == 0, result.output
    assert "flow_id:" in result.output


def test_reference_inside_nested_conduit_inputs_is_accepted(workdir):
    """A key used only in a `tool:conduit` input map counts as referenced."""
    _write(workdir, "hello", REFERENCED_ONLY)
    _write(workdir, "outer", NESTED)
    result = CliRunner().invoke(app, ["run", "outer", "--input", "who=world"])
    assert result.exit_code == 0, result.output
    assert "flow_id:" in result.output


def test_conduit_without_inputs_rejects_any_key(workdir):
    """A conduit that uses no inputs says so instead of guessing a match."""
    _write(workdir, "plain", NO_INPUTS)
    result = CliRunner().invoke(app, ["run", "plain", "--input", "x=1"])
    assert result.exit_code == 1, result.output
    assert "unknown input: x" in result.output
    assert "did you mean" not in result.output
    assert "plain accepts no --input" in result.output


def test_again_override_is_checked(workdir):
    """`--again` applies the same check to its `--input` overrides."""
    _write(workdir, "hello", DECLARED)
    runner = CliRunner()
    first = runner.invoke(app, ["run", "hello", "--input", "name=a"])
    assert first.exit_code == 0, first.output
    flow_id = Atelier().list_flows()[0]
    again = runner.invoke(app, ["run", "--again", flow_id, "--input", "nmae=b"])
    assert again.exit_code == 1, again.output
    assert "unknown input: nmae" in again.output
    assert Atelier().list_flows() == [flow_id]
