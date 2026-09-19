"""CLI tests for `atelier create` — scaffold a new conduit by name."""
from __future__ import annotations

import os

import pytest
from typer.testing import CliRunner

from flow_atelier.cli import app
from flow_atelier.schemas.conduit import Conduit


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


def test_create_writes_valid_conduit(workdir):
    """Happy path: writes a parseable conduit.yaml and prints the run hint."""
    result = CliRunner().invoke(app, ["create", "my-flow"])
    assert result.exit_code == 0, result.output
    path = workdir / ".atelier" / "conduits" / "my-flow" / "conduit.yaml"
    assert path.exists()
    Conduit.model_validate_json(_yaml_to_json(path.read_text()))
    assert "atelier run my-flow" in result.output


def test_create_refuses_to_clobber(workdir):
    """Re-creating an existing name exits 1 and leaves the original intact."""
    assert CliRunner().invoke(app, ["create", "dup"]).exit_code == 0
    path = workdir / ".atelier" / "conduits" / "dup" / "conduit.yaml"
    before = path.read_text()
    result = CliRunner().invoke(app, ["create", "dup"])
    assert result.exit_code == 1
    assert "already exists" in result.output
    assert "Traceback" not in result.output
    assert path.read_text() == before


def test_create_rejects_invalid_name(workdir):
    """An invalid name exits 1 with a validation message and no traceback."""
    result = CliRunner().invoke(app, ["create", "bad name!"])
    assert result.exit_code == 1
    assert "invalid" in result.output
    assert "Traceback" not in result.output


def test_create_then_list_and_check(workdir):
    """The scaffold is genuinely valid: it lists and passes `check`."""
    assert CliRunner().invoke(app, ["create", "roundtrip"]).exit_code == 0
    listed = CliRunner().invoke(app, ["list", "conduits"])
    assert listed.exit_code == 0
    assert "roundtrip" in listed.output
    checked = CliRunner().invoke(app, ["check", "roundtrip"])
    assert checked.exit_code == 0


def _yaml_to_json(text: str) -> str:
    """Parse YAML text and re-emit as JSON for model validation.

    :param text: YAML document.
    :returns: JSON string of the same data.
    """
    import json

    import yaml

    return json.dumps(yaml.safe_load(text))


def test_create_matches_init_shape(workdir):
    """`create` and `init` write the same compact YAML, differing only in name and description."""
    assert CliRunner().invoke(app, ["create", "hello", "-d", "Say hello"]).exit_code == 0
    created = (workdir / ".atelier" / "conduits" / "hello" / "conduit.yaml").read_text()
    other = workdir / "other"
    other.mkdir()
    import os as _os

    cwd = _os.getcwd()
    _os.chdir(other)
    try:
        assert CliRunner().invoke(app, ["init"]).exit_code == 0
    finally:
        _os.chdir(cwd)
    initialized = (other / ".atelier" / "conduits" / "hello" / "conduit.yaml").read_text()
    assert created == initialized
    assert "- greet:" in created
    assert "retry_backoff" not in created
