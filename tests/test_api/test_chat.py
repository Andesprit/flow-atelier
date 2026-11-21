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
def chat_api(tmp_path):
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
    atelier = Atelier(
        settings=AtelierSettings(atelier_dir=tmp_path / ".atelier", harnesses=scripts)
    )
    with TestClient(FastApiServer().create_app(atelier), base_url="http://localhost") as client:
        yield client


def wait_turn(client, url):
    for _ in range(100):
        view = client.get(url).json()
        if not view["busy"]:
            return view
        time.sleep(0.03)
    pytest.fail("turn did not settle")


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
