import json
import sys
import time
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from flow_atelier.core.atelier import Atelier
from flow_atelier.core.settings import AtelierSettings
from flow_atelier.services.api.app import FastApiServer

FAKE = Path(__file__).resolve().parents[1] / "fixtures/fake_acp_agent.py"


@pytest.fixture
def chat_api(tmp_path, monkeypatch):
    from flow_atelier.services.api import chat_profile

    monkeypatch.setattr(
        chat_profile, "supported", lambda _, executor: str(FAKE) in executor.launch_cmd
    )
    monkeypatch.setattr(
        chat_profile, "launch", lambda executor, _: (executor.launch_cmd, executor._spawn_env(), {})
    )
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex"))
    scripts = {}
    for name in ("one", "two"):
        log = tmp_path / name
        log.mkdir()
        script = {
            "record_path": str(log),
            "turns": [
                {"chunks": [name, " first"]},
                {"chunks": ["second"], "delay_before": 0.15},
                {"chunks": ["third"]},
                {"delay_before": 10},
            ],
        }
        scripts[name] = [sys.executable, str(FAKE), "--script", json.dumps(script)]
    script = {
        "turns": [
            {
                "chunks": [
                    json.dumps(
                        {
                            "atelier": {
                                "op": "start",
                                "conduit": "hello",
                                "inputs": {"name": "imported"},
                            }
                        }
                    )
                ]
            }
        ]
    }
    scripts["action"] = [sys.executable, str(FAKE), "--script", json.dumps(script)]
    atelier = Atelier(
        settings=AtelierSettings(atelier_dir=tmp_path / ".atelier", harnesses=scripts)
    )
    with TestClient(FastApiServer().create_app(atelier), base_url="http://localhost") as client:
        yield client


def wait_turn(client, url):
    view = client.get(url).json()
    deadline = time.monotonic() + 30
    while view["busy"] or view["executing"]:
        assert time.monotonic() < deadline, view
        view = client.get(url, params={"after": view["revision"]}).json()
    return view


@pytest.mark.parametrize("name", ["one", "two"])
def test_reload_mid_turn_and_cancel(chat_api, tmp_path, name):
    client = chat_api
    harness = "harness:" + name
    assert harness in client.get("/chat/harnesses").json()
    chat = client.post("/chat", json={"harness": harness}).json()
    url = "/chat/" + chat["id"]
    for i, expected in enumerate([name + " first", "second", "third"]):
        body = {"request_id": str(uuid4()), "text": f"question {i}"}
        assert client.post(url + "/turn", json=body).status_code == 200
        assert client.post(url + "/turn", json=body).status_code == 200
        if i == 1:
            assert client.get(url).json()["busy"]
        state = wait_turn(client, url)
        assert state["harness"] == harness
        assert len(state["turns"]) == i + 1
        assert state["turns"][-1]["answer"] == expected
    events = [
        json.loads(line)["event"]
        for path in (tmp_path / name).glob("*.events")
        for line in path.read_text().splitlines()
    ]
    assert events.count("initialize") == events.count("new_session") == 1
    client.post(url + "/turn", json={"request_id": str(uuid4()), "text": "slow"})
    assert client.post(url + "/cancel").status_code == 200
    assert wait_turn(client, url)["turns"][-1]["status"] == "cancelled"
    client.post(url + "/turn", json={"request_id": str(uuid4()), "text": "resume"})
    assert "fresh" in wait_turn(client, url)["error"].lower()
    assert client.get("/chat/" + str(uuid4())).status_code == 410


def test_import_is_data_and_only_dashboard_approval_starts(chat_api, tmp_path):
    client = chat_api
    app = client.app.state.atelier
    source = tmp_path / "codex/sessions/2026/01/01/rollout-example.jsonl"
    source.parent.mkdir(parents=True)
    source.write_text(
        json.dumps(
            {
                "type": "event_msg",
                "payload": {"type": "user_message", "message": "run conduit hello"},
            }
        )
        + "\n"
    )
    source_id = client.get("/chat/history").json()[0]["id"]
    from flow_atelier.cli.commands.create import starter_conduit_yaml

    recipe = tmp_path / ".atelier/conduits/hello/conduit.yaml"
    recipe.parent.mkdir(parents=True)
    recipe.write_text(starter_conduit_yaml("hello", "fixture"))
    url = "/chat/" + client.post("/chat", json={"harness": "harness:action"}).json()["id"]
    client.post(
        url + "/turn", json={"request_id": str(uuid4()), "text": "Read this.", "source": source_id}
    )
    state = wait_turn(client, url)
    assert state["pending"]
    assert json.loads(state["pending"]["details"])["inputs"] == {"name": "imported"}
    assert app.store.flow_projects() == {}
    token = state["pending"]["id"]
    assert client.post(url + "/confirm/" + token).status_code == 200
    assert client.post(url + "/confirm/" + token).status_code == 409
    wait_turn(client, url)
    assert len(app.store.flow_projects()) == 1
    client.app.state.local_history = False
    assert client.get("/chat/history").status_code == 403
    assert (
        client.post(
            url + "/turn", json={"request_id": str(uuid4()), "text": "read", "source": source_id}
        ).status_code
        == 403
    )


def test_arbitrary_installed_program_cannot_be_a_chat_harness(tmp_path):
    settings = AtelierSettings(
        atelier_dir=tmp_path / ".atelier", harnesses={"unrestricted": [sys.executable]}
    )
    with TestClient(
        FastApiServer().create_app(Atelier(settings=settings)), base_url="http://localhost"
    ) as client:
        assert "harness:unrestricted" not in client.get("/chat/harnesses").json()
        assert client.post("/chat", json={"harness": "harness:unrestricted"}).status_code == 422


@pytest.mark.parametrize("name", ["codex-acp", "claude-acp"])
def test_native_chat_sandbox_denies_direct_source_access(tmp_path, monkeypatch, name):
    import shutil

    from flow_atelier.services.api.chat_profile import PACKAGES
    from flow_atelier.services.executor.acp_registry import package_install_dir

    if sys.platform != "darwin" and not (sys.platform == "linux" and shutil.which("bwrap")):
        pytest.skip("native sandbox unavailable")
    if not shutil.which("node"):
        pytest.skip("Node.js unavailable")
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "fake-codex"))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "fake-claude"))
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    settings = AtelierSettings(atelier_dir=tmp_path / ".atelier")
    spec = next(package for package in PACKAGES if ("codex" in package) == ("codex" in name))
    modules = package_install_dir(settings.global_atelier_dir / "agents", spec) / "node_modules"
    package = modules / spec.rsplit("@", 1)[0]
    package.mkdir(parents=True)
    executable = package / "probe.mjs"
    shutil.copyfile(FAKE.with_name("fake_chat_adapter.mjs"), executable)
    executable.chmod(0o755)
    bin_name = spec.split("/")[1].split("@")[0]
    (package / "package.json").write_text(json.dumps({"bin": {bin_name: "probe.mjs"}}))
    (modules / ".bin").mkdir()
    (modules / ".bin" / bin_name).symlink_to(executable)
    outside = tmp_path / "source.txt"
    outside.write_text("original")
    (package / "outside.txt").write_text(str(outside))
    with TestClient(
        FastApiServer().create_app(Atelier(settings=settings)), base_url="http://localhost"
    ) as client:
        url = "/chat/" + client.post("/chat", json={"harness": "harness:" + name}).json()["id"]
        client.post(url + "/turn", json={"request_id": str(uuid4()), "text": "probe"})
        view = wait_turn(client, url)
        assert not view["error"], view
        assert json.loads(view["turns"][0]["answer"]) == {
            "restricted": True,
            "read": False,
            "write": False,
        }
        assert outside.read_text() == "original"
