"""CLI tests for `atelier plan` — wave rendering, gate notes, error parity."""
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


def test_plan_renders_waves(workdir):
    """A valid diamond conduit renders waves and creates no flow."""
    _write_conduit(
        workdir,
        "diamond",
        "name: diamond\ndescription: d\ntasks:\n"
        "  - name: a\n    description: d\n    task: x\n    tool: tool:bash\n    depends_on: []\n"
        "  - name: b\n    description: d\n    task: x\n    tool: tool:bash\n    depends_on: [a]\n"
        "  - name: c\n    description: d\n    task: x\n    tool: tool:bash\n    depends_on: [a]\n"
        "  - name: d\n    description: d\n    task: x\n    tool: tool:bash\n    depends_on: [b, c]\n",
    )
    result = CliRunner().invoke(app, ["plan", "diamond"])
    assert result.exit_code == 0
    assert "Wave 0" in result.stdout
    assert "Wave 2" in result.stdout
    assert "diamond" in result.stdout
    # Read-only: no flow run was created.
    flows = workdir / ".atelier" / "flows"
    assert not flows.exists() or not any(flows.iterdir())


def test_plan_unknown_conduit_exits_1(workdir):
    """Planning a non-existent conduit exits 1."""
    result = CliRunner().invoke(app, ["plan", "nope"])
    assert result.exit_code == 1
    assert "unknown conduit" in result.stdout


def test_plan_invalid_conduit_matches_check_surface(workdir):
    """A cyclic conduit fails plan with the same cycle message check emits."""
    body = (
        "name: cyclic\ndescription: d\ntasks:\n"
        "  - name: a\n    description: d\n    task: x\n    tool: tool:bash\n    depends_on: [b]\n"
        "  - name: b\n    description: d\n    task: x\n    tool: tool:bash\n    depends_on: [a]\n"
    )
    _write_conduit(workdir, "cyclic", body)
    plan_res = CliRunner().invoke(app, ["plan", "cyclic"])
    check_res = CliRunner().invoke(app, ["check", "cyclic"])
    assert plan_res.exit_code == 1
    assert check_res.exit_code == 1
    assert "circular dependency" in plan_res.stdout
    assert "circular dependency" in check_res.stdout


def test_plan_flags_gate(workdir):
    """A conditional dependency makes its source render as a short-circuit gate."""
    _write_conduit(
        workdir,
        "gated",
        "name: gated\ndescription: d\ntasks:\n"
        "  - name: pick\n    description: d\n    task: x\n    tool: tool:bash\n    depends_on: []\n"
        "  - name: work\n    description: d\n    task: x\n    tool: tool:bash\n"
        "    depends_on:\n      - \"pick.output.match(^READY: )\"\n",
    )
    result = CliRunner().invoke(app, ["plan", "gated"])
    assert result.exit_code == 0
    assert "gate" in result.stdout
    assert "prunes" in result.stdout
    assert "work" in result.stdout


def test_plan_json_emits_waves(workdir):
    """`--json` emits the plan as one JSON document with its waves and sinks."""
    import json

    _write_conduit(
        workdir,
        "pair",
        "name: pair\ndescription: d\ntasks:\n"
        "  - name: a\n    description: d\n    task: x\n    tool: tool:bash\n    depends_on: []\n"
        "  - name: b\n    description: d\n    task: x\n    tool: tool:bash\n    depends_on: [a]\n",
    )
    result = CliRunner().invoke(app, ["plan", "pair", "--json"])
    assert result.exit_code == 0, result.stdout
    payload = json.loads(result.stdout)
    assert payload["conduit_name"] == "pair"
    assert [[t["name"] for t in wave] for wave in payload["waves"]] == [["a"], ["b"]]
    assert payload["sinks"] == ["b"]
