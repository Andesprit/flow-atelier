"""CLI tests for `atelier check` readiness gating + required-input listing."""
from __future__ import annotations

import os

import pytest
from typer.testing import CliRunner

from flow_atelier.cli import app


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


def _write_conduit(workdir, name: str, body: str) -> None:
    """Write a conduit.yaml under the project `.atelier/conduits/<name>/`.

    :param workdir: working directory path.
    :param name: conduit name (also the folder name).
    :param body: full YAML document for the conduit.
    """
    cdir = workdir / ".atelier" / "conduits" / name
    cdir.mkdir(parents=True)
    (cdir / "conduit.yaml").write_text(body)


def test_check_ok_for_bash_only(workdir):
    """A bash-only conduit checks OK and lists no required inputs."""
    _write_conduit(
        workdir,
        "bashy",
        "name: bashy\ndescription: d\n"
        "tasks:\n  - name: a\n    description: a\n    task: echo hi\n    tool: tool:bash\n",
    )
    result = CliRunner().invoke(app, ["check", "bashy"])
    assert result.exit_code == 0
    assert "OK" in result.stdout
    assert "requires --input" not in result.stdout


def test_check_lists_required_inputs(workdir):
    """An OK conduit with a no-default input shows the requires line."""
    _write_conduit(
        workdir,
        "needy",
        "name: needy\ndescription: d\n"
        "inputs:\n  topic:\n    description: the topic\n"
        "tasks:\n  - name: a\n    description: a\n    task: echo {{inputs.topic}}\n    tool: tool:bash\n",
    )
    result = CliRunner().invoke(app, ["check", "needy"])
    assert result.exit_code == 0
    assert "requires --input" in result.stdout
    assert "topic" in result.stdout


def test_check_fails_when_harness_binary_missing(workdir, monkeypatch):
    """A harness whose CLI is absent from PATH makes check FAIL with exit 1."""
    monkeypatch.setattr(
        "flow_atelier.services.executor.harness.shutil.which",
        lambda _binary: None,
    )
    _write_conduit(
        workdir,
        "agentic",
        "name: agentic\ndescription: d\n"
        "tasks:\n  - name: build\n    description: b\n    task: do it\n    tool: harness:claude-code\n",
    )
    result = CliRunner().invoke(app, ["check", "agentic"])
    assert result.exit_code == 1
    assert "FAIL" in result.stdout
    assert "build" in result.stdout


def test_check_json_reports_each_conduit(workdir):
    """`--json` emits one record per conduit and still exits 1 on any failure."""
    import json

    _write_conduit(
        workdir,
        "good",
        "name: good\ndescription: d\ninputs:\n  who: w\n"
        "tasks:\n  - name: a\n    description: a\n    task: echo {{inputs.who}}\n    tool: tool:bash\n",
    )
    _write_conduit(
        workdir,
        "broken",
        "name: broken\ndescription: d\ntasks:\n"
        "  - name: a\n    description: a\n    task: x\n    tool: tool:bash\n    depends_on: [a]\n",
    )
    result = CliRunner().invoke(app, ["check", "--json"])
    assert result.exit_code == 1
    rows = {row["name"]: row for row in json.loads(result.stdout)}
    assert rows["good"]["ok"] is True
    assert rows["good"]["required_inputs"] == ["who"]
    assert rows["broken"]["ok"] is False
    assert rows["broken"]["error"]
