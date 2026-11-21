import asyncio
import base64
import hashlib
import hmac
import json
import time

import httpx
import pytest

from flow_atelier.core.settings import AtelierSettings
from flow_atelier.services.api.error_hooks import MAX_REPORT_BYTES, create_webhook_app


def signed(data, timestamp=None, secret=b"fixture key"):
    stamp = str(int(time.time()) if timestamp is None else timestamp)
    signature = hmac.digest(secret, stamp.encode() + b"." + data, hashlib.sha256).hex()
    return {"X-Atelier-Timestamp": stamp, "X-Atelier-Signature": "sha256=" + signature}


@pytest.fixture
def receiver(tmp_path, monkeypatch):
    home = AtelierSettings().global_atelier_dir
    sources = {name: {"project": str(tmp_path), "conduit": "demo"} for name in ["alpha", "beta"]}
    (home / "settings.json").write_text(
        json.dumps(
            {
                "webhookSources": sources,
                "webhookSecrets": {"alpha": "fixture key", "beta": "second key"},
            }
        )
    )
    monkeypatch.setenv("ATELIER_WEBHOOK_SECRETS", '{"alpha":"wrong"}')
    return create_webhook_app(AtelierSettings())


def test_webhook_settings_only_come_from_user_file(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    home = AtelierSettings().global_atelier_dir
    trusted = {
        "webhookSources": {"app": {"project": str(tmp_path), "conduit": "trusted"}},
        "webhookSecrets": {"app": "trusted-value"},
        "runCaps": {"errors": 2},
    }
    (home / "settings.json").write_text(json.dumps(trusted))
    project = tmp_path / ".atelier"
    project.mkdir()
    (project / "settings.json").write_text(
        json.dumps(
            {
                "webhookSources": {"other": {}},
                "webhookSecrets": {"other": "wrong"},
                "runCaps": {"errors": 3},
            }
        )
    )
    alt = tmp_path / "secrets"
    alt.mkdir()
    for name, value in {
        "ATELIER_WEBHOOK_SOURCES": '{"other":{}}',
        "ATELIER_WEBHOOK_SECRETS": '{"other":"wrong"}',
        "ATELIER_RUN_CAPS": '{"other":5}',
    }.items():
        monkeypatch.setenv(name, value)
        (alt / name).write_text(value)
        with (tmp_path / ".env").open("a") as dotenv:
            dotenv.write(f"{name}={value}\n")
    settings = AtelierSettings(_secrets_dir=alt)
    assert settings.webhook_sources == trusted["webhookSources"]
    assert {k: v.get_secret_value() for k, v in settings.webhook_secrets.items()} == trusted[
        "webhookSecrets"
    ]
    assert settings.run_caps == trusted["runCaps"]
    assert "trusted-value" not in capsys.readouterr().err


@pytest.fixture
async def client(receiver):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=receiver),
        base_url="http://local",
    ) as client:
        yield client


async def test_group_and_isolate_reports(client):
    async def post(data, source="alpha", secret=b"fixture key"):
        body = json.dumps(data).encode()
        return await client.post(
            "/hooks/errors/" + source, content=body, headers=signed(body, secret=secret)
        )

    first = await post({"fingerprint": "order-17", "message": "first"})
    second = await post({"fingerprint": "order-17", "message": "second"})
    assert first.status_code == second.status_code == 202
    assert first.json() == second.json()
    other = await post({"fingerprint": "order-17"}, "beta", b"second key")
    assert first.json()["run_id"] != other.json()["run_id"]
    frame = {"module": "app.orders", "function": "save"}
    report = {"exception_class": "KeyError", "frames": [dict(frame, line=12)]}
    third = await post(report)
    fourth = await post(dict(report, frames=[dict(frame, line=99)], message="different"))
    assert third.status_code == fourth.status_code == 202
    assert third.json() == fourth.json()
    assert first.json() != third.json()
    assert (await post(dict(report, exception_class="TypeError"))).json() != third.json()


async def test_reject_invalid_reports_before_claim(client):
    url = "/hooks/errors/alpha"
    body = b'{"fingerprint":"abc"}'
    for path in ["/flows", "/docs", "/redoc", "/openapi.json"]:
        assert (await client.get(path)).status_code == 404
    assert (await client.get(url)).status_code == 405
    assert (
        await client.post("/hooks/errors/unknown", content=body, headers=signed(body))
    ).status_code == 404
    assert (
        await client.post(url, content=body, headers=signed(body, secret=b"wrong"))
    ).status_code == 401
    assert (await client.post(url, content=body + b" ", headers=signed(body))).status_code == 401
    for skew in (-360, 360):
        assert (
            await client.post(url, content=body, headers=signed(body, int(time.time()) + skew))
        ).status_code == 401
    assert (await client.post(url, content=b"bad", headers=signed(b"bad"))).status_code == 400
    assert (await client.post(url, content=b"{}", headers=signed(b"{}"))).status_code == 422
    big = b"x" * (MAX_REPORT_BYTES + 1)
    assert (await client.post(url, content=big, headers=signed(big))).status_code == 413

    async def chunks():
        yield big[:-1]
        yield big[-1:]
        pytest.fail("receiver read beyond the size limit")

    assert (await client.post(url, content=chunks(), headers=signed(big))).status_code == 413


async def test_receiver_dispatches_original_bytes_in_configured_project(receiver, client, tmp_path):
    recipe = tmp_path / ".atelier/conduits/demo/conduit.yaml"
    recipe.parent.mkdir(parents=True)
    recipe.write_text("""name: demo
description: Collect one report
inputs:
  report_b64: {description: Signed report bytes}
tasks:
  - name: collect
    description: Capture the report
    tool: tool:bash
    task: echo '{{inputs.report_b64}}' >> reports
""")
    body = b'{"fingerprint":"one", "message":"user input"}'
    async with receiver.router.lifespan_context(receiver):
        path = "/hooks/errors/alpha"
        first = await client.post(path, content=body, headers=signed(body))
        async with asyncio.timeout(5):
            while True:
                replay = await client.post(path, content=body, headers=signed(body))
                assert replay.json()["run_id"] == first.json()["run_id"]
                if replay.json()["status"] == "completed":
                    break
                await asyncio.sleep(0.05)
    assert (tmp_path / "reports").read_text().splitlines() == [base64.b64encode(body).decode()]
