import json
import os
from datetime import UTC, datetime

from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
from fastapi.testclient import TestClient
from py_vapid import Vapid, b64urlencode
from requests import Response

from flow_atelier.core.atelier import Atelier
from flow_atelier.schemas.api import PriorFlow
from flow_atelier.services.api.app import FastApiServer
from flow_atelier.services.web_push import WebPush


def test_push_is_opt_in_private_and_deduplicated(tmp_path, monkeypatch):
    atelier = Atelier(base_dir=tmp_path / ".atelier")
    push = WebPush(atelier.settings.global_atelier_dir)
    assert not push.path.exists()
    client = TestClient(
        FastApiServer().create_app(atelier, api_token="secret"), base_url="http://localhost"
    )
    assert client.get("/push/key").status_code == 401
    client.headers.update({"Authorization": "Bearer secret"})
    public = client.get("/push/key").json()["key"]
    assert client.get("/push/key").json()["key"] == public
    if os.name == "posix":
        assert push.path.stat().st_mode & 0o077 == 0
        assert push.path.parent.stat().st_mode & 0o077 == 0

    device = Vapid()
    device.generate_keys()
    key = b64urlencode(
        device.public_key.public_bytes(Encoding.X962, PublicFormat.UncompressedPoint)
    )
    sub = {
        "endpoint": "https://fcm.googleapis.com/fcm/send/test",
        "keys": {"p256dh": key, "auth": b64urlencode(b"0123456789abcdef")},
    }
    assert (
        client.post(
            "/push/subscriptions", json={**sub, "endpoint": "https://localhost/bad"}
        ).status_code
        == 422
    )
    assert client.post("/push/subscriptions", json=sub).status_code == 204
    sent = []
    status = 201

    def fake_provider(self, url, **kwargs):
        # Exercise real VAPID signing and encryption without contacting a provider.
        assert url == sub["endpoint"]
        assert kwargs["headers"]["authorization"].startswith("vapid ")
        assert kwargs["headers"]["content-encoding"] == "aes128gcm"
        assert b"private-project" not in kwargs["data"]
        sent.append(kwargs["data"])
        res = Response()
        res.status_code = status
        res._content = b""
        return res

    monkeypatch.setattr("requests.Session.post", fake_provider)
    run = PriorFlow(
        flow_id="20261121_abcdef12_private-project",
        conduit_name="private-project",
        project="/private/path",
        status="completed",
        finished_at=datetime.now(UTC).isoformat(),
    )
    monkeypatch.setattr(atelier, "list_prior_flows", lambda: [run])
    payloads = []
    import flow_atelier.services.web_push as module

    real_send = module.webpush

    def capture(**kwargs):
        payloads.append(json.loads(kwargs["data"]))
        return real_send(**kwargs)

    monkeypatch.setattr(module, "webpush", capture)
    push.deliver(atelier)
    push.deliver(atelier)
    assert len(sent) == 1
    notice = payloads[0]
    assert set(notice) == {"run_id", "status"}
    assert notice["status"] == "completed"
    assert "private" not in json.dumps(notice)
    assert client.get("/push/runs/" + notice["run_id"]).json()["flow_id"] == run.flow_id
    assert (
        client.request(
            "DELETE", "/push/subscriptions", json={"endpoint": sub["endpoint"]}
        ).status_code
        == 204
    )
    run.flow_id = "20261121_abcdef13_second"
    push.deliver(atelier)
    assert len(sent) == 1
    assert client.post("/push/subscriptions", json=sub).status_code == 204
    run.finished_at = datetime.now(UTC).isoformat()
    status = 410
    push.deliver(atelier)
    run.flow_id = "20261121_abcdef14_third"
    push.deliver(atelier)
    assert len(sent) == 2
