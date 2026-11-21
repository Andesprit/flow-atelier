import json
import os
import sys
import time
from contextlib import nullcontext
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
        chat_profile,
        "launch",
        lambda executor, _: nullcontext((executor.launch_cmd, executor._spawn_env(), {})),
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
    with TestClient(
        FastApiServer().create_app(atelier, api_token="test-token"),
        base_url="http://localhost",
        headers={"Authorization": "Bearer test-token"},
    ) as client:
        yield client


def wait_turn(client, url, timeout=30):
    view = client.get(url).json()
    deadline = time.monotonic() + timeout
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
        FastApiServer().create_app(Atelier(settings=settings), api_token="test-token"),
        base_url="http://localhost",
        headers={"Authorization": "Bearer test-token"},
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
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "fixture-token")
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
        FastApiServer().create_app(Atelier(settings=settings), api_token="test-token"),
        base_url="http://localhost",
        headers={"Authorization": "Bearer test-token"},
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


def test_linux_token_required_for_chat(chat_api, monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    chat_api.app.state.api_token = ""
    assert chat_api.get("/chat/harnesses").status_code == 403
    response = chat_api.post("/chat", json={"harness": "harness:one"})
    assert response.status_code == 403
    assert "ATELIER_API_TOKEN" in response.json()["detail"]
    assert chat_api.get("/chat/history").status_code == 200


def test_macos_claude_requires_setup_token_at_start(chat_api, monkeypatch):
    from flow_atelier.routes import chat

    monkeypatch.setattr(sys, "platform", "darwin")
    executor = next(iter(chat.available(chat_api.app.state.atelier).values()))
    monkeypatch.setattr(executor, "launch_cmd", ["/fake/bin/claude-agent-acp"])
    monkeypatch.setattr(chat, "available", lambda _: {"claude": executor})
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    response = chat_api.post("/chat", json={"harness": "claude"})
    assert response.status_code == 422
    assert "claude setup-token" in response.json()["detail"]
    assert not chat_api.app.state.chats


def test_history_import_errors_and_size_limit_keep_chat_open(chat_api, tmp_path):
    url = "/chat/" + chat_api.post("/chat", json={"harness": "harness:one"}).json()["id"]
    path = tmp_path / "codex/sessions/2026/01/01/rollout-test.jsonl"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"invalid JSON\n")
    source = chat_api.get("/chat/history").json()[0]["id"]

    def turn():
        chat_api.post(
            url + "/turn", json={"request_id": str(uuid4()), "text": "read", "source": source}
        )
        return wait_turn(chat_api, url)

    state = turn()
    assert state["error"] is None
    assert "Invalid JSON" in state["turns"][-1]["result"]

    def message(text):
        return (
            json.dumps({"type": "event_msg", "payload": {"type": "user_message", "message": text}})
            + "\n"
        )

    path.write_text(message("first") + message("oversize" * 20000))
    preview = chat_api.get("/chat/history", params={"source": source}).json()
    assert "truncat" in preview["warning"].lower()
    assert len(preview["messages"]) == 1
    state = turn()
    assert state["error"] is None
    assert "truncat" in state["turns"][-1]["result"].lower()
    log = next((tmp_path / "one").glob("*.jsonl")).read_text()
    assert "first" in log and "oversize" not in log


@pytest.mark.parametrize("reload", ["load", "resume"])
async def test_reconnected_codex_must_report_read_only(tmp_path, monkeypatch, reload):
    from flow_atelier.services.api import chat_profile
    from flow_atelier.services.api.chat import ChatSession
    from flow_atelier.services.executor.harness import AcpHarnessExecutor

    script = {
        "reload": reload,
        "modes": {"available": [{"id": "read-only", "name": "Read only"}], "current": "read-only"},
    }
    executor = AcpHarnessExecutor([sys.executable, str(FAKE), "--script", json.dumps(script)])
    monkeypatch.setattr(
        chat_profile,
        "launch",
        lambda *_: nullcontext(
            (executor.launch_cmd, dict(os.environ, INITIAL_AGENT_MODE="read-only"), {})
        ),
    )
    session = ChatSession("fake", executor, tmp_path, tmp_path)
    try:
        await session.connect()
        await session.disconnect()
        with pytest.raises(ValueError, match="read-only"):
            await session.connect()
    finally:
        await session.close()


@pytest.mark.parametrize("case", ["unchanged", "refreshed", "concurrent", "symlink"])
def test_chat_credential_refresh(tmp_path, monkeypatch, case):
    from types import SimpleNamespace

    from flow_atelier.services.api import chat_profile

    monkeypatch.setattr(chat_profile, "sandbox", lambda *_args, **_kw: [])
    monkeypatch.setattr(chat_profile.shutil, "which", lambda _: sys.executable)
    auth = tmp_path / "codex/auth.json"
    auth.parent.mkdir()
    monkeypatch.setenv("CODEX_HOME", str(auth.parent))
    stale = '{"refresh_token":"fixture","last_refresh":"2020-01-01T00:00:00Z"}'
    fresh = '{"refresh_token":"fixture-new"}'
    auth.write_text(stale)
    original = auth.stat()
    root = tmp_path / "scratch"
    root.mkdir()
    with chat_profile.launch(SimpleNamespace(launch_cmd=["/adapter/.bin/codex-acp"]), str(root)):
        scratch = root / ".codex/auth.json"
        assert not scratch.is_symlink() and scratch.read_text() == stale
        if case == "symlink":
            scratch.unlink()
            scratch.symlink_to(auth)
        elif case != "unchanged":
            scratch.write_text(fresh)
        if case == "concurrent":
            auth.write_text('{"refresh_token":"another-login"}')
    if case == "refreshed":
        with chat_profile.launch(
            SimpleNamespace(launch_cmd=["/adapter/.bin/codex-acp"]), str(root)
        ):
            assert scratch.read_text() == fresh
        assert auth.read_text() == fresh
        if os.name == "posix":
            assert auth.stat().st_mode & 0o077 == 0
    elif case == "concurrent":
        assert json.loads(auth.read_text())["refresh_token"] == "another-login"
    else:
        assert auth.read_text() == stale and auth.stat().st_mtime_ns == original.st_mtime_ns


@pytest.mark.live
@pytest.mark.parametrize("name", ["codex-acp", "claude-acp"])
def test_pinned_chat_adapter_live(tmp_path, name):
    from flow_atelier.services.api.chat_profile import PACKAGES
    from flow_atelier.services.executor.acp_registry import (
        installed_executable,
        package_install_dir,
    )

    spec = next(s for s in PACKAGES if ("codex" in s) == ("codex" in name))
    agents = Path(os.environ.get("ATELIER_LIVE_AGENT_DIR") or Path.home() / ".atelier/agents")
    if not installed_executable(agents, spec):
        pytest.skip(f"{spec} is not installed")
    settings = AtelierSettings(atelier_dir=tmp_path / ".atelier")
    target = package_install_dir(settings.global_atelier_dir / "agents", spec)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.symlink_to(package_install_dir(agents, spec), target_is_directory=True)
    app = FastApiServer().create_app(Atelier(settings=settings), api_token="live-fixture")
    with TestClient(
        app, base_url="http://localhost", headers={"Authorization": "Bearer live-fixture"}
    ) as client:
        response = client.post("/chat", json={"harness": "harness:" + name})
        if (
            name == "claude-acp"
            and sys.platform == "darwin"
            and not os.getenv("CLAUDE_CODE_OAUTH_TOKEN")
        ):
            assert response.status_code == 422 and "setup-token" in response.json()["detail"]
            pytest.skip("Claude macOS setup token unavailable")
        assert response.status_code == 200, response.text
        url = "/chat/" + response.json()["id"]
        client.post(
            url + "/turn",
            json={"request_id": str(uuid4()), "text": "Reply with just the word apricot."},
        )
        state = wait_turn(client, url, timeout=180)
        assert not state["error"], state["error"]
        assert state["turns"][0]["answer"].strip().lower().strip(".") == "apricot"
        assert state["pending"] is None

        if name == "codex-acp":
            chat = app.state.chats[state["id"]]
            session_id = chat.session_id
            assert session_id
            client.portal.call(chat.disconnect)
            client.portal.call(chat.connect)
            assert chat.session_id == session_id


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS sandbox profile")
def test_model_network_has_dns_and_ca_bundle(tmp_path):
    from types import SimpleNamespace

    from flow_atelier.services.api.chat_cli import sandbox
    from flow_atelier.services.api.chat_profile import launch

    with launch(SimpleNamespace(launch_cmd=["/adapter/.bin/codex-acp"]), str(tmp_path)) as (
        argv,
        env,
        _,
    ):
        assert '(allow network-outbound (literal "/private/var/run/mDNSResponder"))' in argv[2]
        assert Path(env["NODE_EXTRA_CA_CERTS"]).is_file()
    assert "mDNSResponder" not in sandbox(tmp_path, set())[2]


def test_adapter_environment_isolated():
    assert all(
        name not in os.environ
        for name in ("CODEX_HOME", "CLAUDE_CONFIG_DIR", "CLAUDE_CODE_OAUTH_TOKEN")
    )
