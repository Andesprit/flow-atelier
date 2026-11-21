import json
from pathlib import Path

import jsonschema
import pytest

from flow_atelier.schemas.runner import messages
from flow_atelier.services.runner.client import Client
from flow_atelier.services.runner.local import Local
from tests.runner.plane import Plane


def test_wire_schema_and_examples():
    root = Path(__file__).resolve().parents[2] / "docs/runner/v1"
    spec = json.loads((root / "messages.schema.json").read_text())
    assert spec == messages()
    samples = json.loads((root / "samples.json").read_text())
    assert samples.keys() == spec["properties"].keys()
    for name, body in samples.items():
        jsonschema.validate(body, {**spec["properties"][name], "$defs": spec["$defs"]})


def test_origin_is_https_only(tmp_path):
    for origin in ["http://example.test", "https://me@example.test", "https://x/a", "https://x?q"]:
        with pytest.raises(ValueError):
            Client(origin, tmp_path)


def test_status_displays_last_acknowledged_heartbeat(tmp_path, monkeypatch):
    import time

    from typer.testing import CliRunner

    from flow_atelier.cli import app

    plane = Plane()
    local = Local(tmp_path)
    monkeypatch.setenv("ATELIER_GLOBAL_ATELIER_DIR", str(tmp_path))
    local.pair(plane, plane.invite("one"))
    now = 1000
    monkeypatch.setattr(time, "time", lambda: now)
    local.poll(plane)
    cli = CliRunner()
    assert "online" in cli.invoke(app, ["runner", "status"]).output
    now += 60
    report = cli.invoke(app, ["runner", "status"])
    assert report.exit_code == 0
    assert "offline" in report.output and "1970-01-01T00:16:40" in report.output


def test_tenant_pairing_registration_revocation(tmp_path):
    plane = Plane()
    for tenant in ["one", "two"]:
        local = Local(tmp_path / tenant)
        token = plane.invite(tenant)
        local.pair(plane, token)
        with pytest.raises(PermissionError):
            Local(tmp_path / (tenant + "-copy")).pair(plane, token)
        fid = local.expose(tmp_path, tenant)
        local.poll(plane)
        view, = plane.view(tenant)
        assert view["folders"] == [{"id": fid, "label": tenant, "conduits": []}]
        assert view["online"] and view["heartbeat"] == plane.now
    plane.revoke("one")
    with pytest.raises(PermissionError):
        Local(tmp_path / "one").poll(plane)


def recipe(folder, name, tasks):
    import yaml
    directory = folder / ".atelier/conduits" / name
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "conduit.yaml").write_text(yaml.safe_dump(
        {"name": name, "description": "fixture", "tasks": tasks}))
    return directory


def step(tool, task):
    return {"name": "step", "tool": tool, "task": task, "description": "fixture"}


@pytest.mark.parametrize("command", [
    "bash ./helper", 'bash "{{conduit_dir}}/../helper"', 'bash "{{conduit_dir}}/missing"',
    "echo ok > marker", "printf text >> marker", "echo $(bash ./helper)", "true; bash ./helper",
])
def test_external_or_dynamic_shell_entry_points_need_packaging(tmp_path, command):
    project = tmp_path / "project"
    project.mkdir()
    (project / "helper").write_text("echo before")
    local = Local(tmp_path / "user")
    fid = local.expose(project, "Manuals")
    recipe(project, "build", [step("tool:bash", command)])
    with pytest.raises(ValueError, match="bundled"):
        local.approve(fid, "build")


def test_local_policy_includes_children_helpers_and_rejects_symlinks(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    local = Local(tmp_path / "user")
    fid = local.expose(project, "docs")
    recipe(project, "root", [step("tool:conduit", "child")])
    child = recipe(project, "child", [step("tool:bash", 'bash "{{conduit_dir}}/helper"')])
    helper = child / "helper"
    helper.write_text("echo approved")
    local.approve(fid, "root")
    with local.snapshot(fid, "root") as (_, copy):
        assert (copy / "conduits/child/helper").read_text() == "echo approved"
        helper.write_text("echo changed")
        assert (copy / "conduits/child/helper").read_text() == "echo approved"
    with pytest.raises(ValueError), local.snapshot(fid, "root"):
        pass
    for folder, name in [(fid, "child"), ("foreign", "root")]:
        with pytest.raises(ValueError), local.snapshot(folder, name):
            pass
    helper.unlink()
    helper.symlink_to(__file__)
    with pytest.raises(ValueError):
        local.approve(fid, "root")
    elsewhere = project.rename(tmp_path / "moved")
    project.symlink_to(elsewhere, target_is_directory=True)
    with pytest.raises(ValueError):
        local.approve(fid, "root")


def test_short_environment_values_and_secret_names(tmp_path, monkeypatch):
    plane = Plane()
    local = Local(tmp_path / "user")
    local.pair(plane, plane.invite("one"))
    fid = local.expose(tmp_path, "Customer Docs")
    for key, value in {"SHLVL": "1", "XPC_SERVICE_NAME": "0", "LANG": "C",
                       "DEPLOY_PASS": "xy", "CUSTOM": "longvalue"}.items():
        monkeypatch.setenv(key, value)
    for name in ["build1", "release-2026", "buildxy", "build-longvalue"]:
        recipe(tmp_path, name, [step("tool:bash", "true")])
        local.approve(fid, name)
    job = plane.enqueue("one", {local.identity()["id"]: fid}, "build1")
    assert local.poll(plane).job.id == job
    assert plane.view("one")[0]["folders"] == [
        {"id": fid, "label": "Customer Docs", "conduits": ["build1", "release-2026"]}]


def test_approval_manifest_and_management_cli(tmp_path, monkeypatch):
    from typer.testing import CliRunner

    from flow_atelier.cli import app
    from flow_atelier.services.runner.worker import Worker

    cli = CliRunner()

    def invoke(*args, answer=None):
        return cli.invoke(app, ["runner", *args], input=answer)

    project = tmp_path / "project"
    root = recipe(project, "build", [step("tool:conduit", "child")])
    child = recipe(tmp_path, "child", [{**step("harness:codex", "review"), "permissions": "edits"}])
    helper = child / "helper"
    helper.write_text("true")
    helper.chmod(0o755)
    local = Local(tmp_path / ".atelier")
    monkeypatch.setenv("ATELIER_GLOBAL_ATELIER_DIR", str(local.user_dir))
    plane = Plane()
    local.pair(plane, plane.invite("one"))
    fid = local.expose(project, "Manuals")
    before = invoke("approve", fid, "build", answer="n\n")
    assert not local.inventory()[0].conduits
    for value in (ascii(str(root)), ascii(str(child)), "helper", "conduit.yaml",
                  "harness:codex", "permissions=edits"):
        assert value in before.output
    executable = "+x" if helper.stat().st_mode & 0o111 else "-x"
    assert f"{executable} 'conduits/child/helper'" in before.output
    assert invoke("approve", fid, "build", answer="\n").exit_code != 0
    assert not local.inventory()[0].conduits
    (local.root / "redact.json").write_text('["build","Manuals"]')
    approved = invoke("approve", fid, "build", "--yes")
    assert approved.exit_code == 0, approved.exception
    assert "advertised" in approved.output

    async def stop(_self):
        pass

    monkeypatch.setattr(Worker, "serve", stop)
    assert "advertised" in invoke("start").output
    with local.db() as db:
        digest = db.execute("SELECT digest FROM approvals").fetchone()[0]
    assert digest in approved.output
    listing = invoke("folders").output
    assert all(word in listing for word in [fid, "Manuals", ascii(str(project)), "build", digest[:8]])
    local.approve(fid, "child")
    assert invoke("revoke", fid, "build").exit_code == 0
    assert local.inventory()[0].conduits == ["child"]
    assert invoke("revoke", fid).exit_code == 0
    assert local.inventory()[0].conduits == []
    assert invoke("unexpose", fid).exit_code == 0
    assert not local.inventory() and project.is_dir()
    assert invoke("unpair").exit_code == 0
    with pytest.raises(ValueError, match="pair"):
        local.identity()
    local.pair(plane, plane.invite("second"))
    assert local.status() == (False, None)


def test_detached_runner_refuses_child_hitl(tmp_path):
    local = Local(tmp_path / "user")
    recipe(tmp_path, "root", [step("tool:conduit", "child")])
    recipe(tmp_path, "child", [step("tool:hitl", "input")])
    fid = local.expose(tmp_path, "docs")
    with pytest.raises(ValueError, match="hitl"):
        local.approve(fid, "root")
    assert not local.inventory()[0].conduits
