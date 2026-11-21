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


def test_origin_is_https_only():
    for origin in ["http://example.test", "https://me@example.test", "https://x/a", "https://x?q"]:
        with pytest.raises(ValueError):
            Client(origin)


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
