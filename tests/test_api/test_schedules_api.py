"""/schedules REST tests."""
from __future__ import annotations

import httpx
import pytest
import yaml

from flow_atelier.core.atelier import Atelier
from flow_atelier.services.api.app import FastApiServer


@pytest.fixture
async def fixture(tmp_path, monkeypatch):
    """Yield an httpx client wired to a fresh Atelier instance.

    :param tmp_path: pytest temp directory fixture.
    :param monkeypatch: pytest monkeypatch fixture.
    """
    monkeypatch.delenv("ATELIER_GLOBAL_ATELIER_DIR", raising=False)
    atelier = Atelier(base_dir=tmp_path / ".atelier")
    # create_schedule validates conduit_name and rejects inputs the conduit
    # cannot use, so the scheduled conduit must exist on disk and reference
    # the `foo` input the payloads send.
    conduit_dir = tmp_path / ".atelier" / "conduits" / "report"
    conduit_dir.mkdir(parents=True)
    (conduit_dir / "conduit.yaml").write_text(
        "name: report\n"
        "description: test conduit\n"
        "tasks:\n"
        "  - greet:\n"
        "      description: say hi\n"
        '      task: "echo {{inputs.foo}}"\n'
        "      tool: tool:bash\n"
        "      depends_on: []\n"
    )
    app = FastApiServer().create_app(atelier)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as c:
        yield c, atelier, tmp_path


def _payload(**overrides):
    """Build a baseline schedule payload, with optional overrides.

    :param overrides: keys to override on the baseline payload.
    """
    base = {
        "conduit_name": "report",
        "inputs": {"foo": "bar"},
        "run_path": "/tmp/x",
        "schedule": {
            "mode": "recurring",
            "name": "weekday mornings",
            "days": [1, 2, 3, 4, 5],
            "times": ["06:00"],
        },
    }
    base.update(overrides)
    return base


async def test_list_schedules_starts_empty(fixture):
    """Verify GET /schedules starts empty.

    :param fixture: client+atelier+tmp_path tuple fixture.
    """
    client, _, _ = fixture
    resp = await client.get("/schedules")
    assert resp.status_code == 200
    assert resp.json() == []


async def test_create_schedule_returns_201(fixture):
    """Verify POST /schedules returns 201 and a SCH- prefixed id.

    :param fixture: client+atelier+tmp_path tuple fixture.
    """
    client, _, _ = fixture
    resp = await client.post("/schedules", json=_payload())
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["id"].startswith("SCH-")
    assert body["conduit_name"] == "report"


async def test_create_interval_schedule_returns_201(fixture):
    """Verify POST /schedules accepts an interval-mode payload.

    :param fixture: client+atelier+tmp_path tuple fixture.
    """
    client, _, _ = fixture
    payload = _payload(
        schedule={"mode": "interval", "name": "half hourly", "every_minutes": 30}
    )
    resp = await client.post("/schedules", json=payload)
    assert resp.status_code == 201, resp.text
    assert resp.json()["schedule"]["every_minutes"] == 30


async def test_create_schedule_persists_to_yaml_file(fixture):
    """Verify a created schedule is persisted to a YAML file under schedules/.

    :param fixture: client+atelier+tmp_path tuple fixture.
    """
    client, _, tmp_path = fixture
    resp = await client.post("/schedules", json=_payload())
    sch_id = resp.json()["id"]
    (yaml_path,) = list(
        (tmp_path / ".atelier" / "schedules").glob("weekday-mornings-*.yaml")
    )
    raw = yaml.safe_load(yaml_path.read_text())
    assert raw["id"] == sch_id
    assert raw["schedule"]["mode"] == "recurring"
    assert raw["schedule"]["name"] == "weekday mornings"


async def test_create_schedule_invalid_returns_400(fixture):
    """Verify an invalid schedule payload returns 400 or 422.

    :param fixture: client+atelier+tmp_path tuple fixture.
    """
    client, _, _ = fixture
    resp = await client.post(
        "/schedules",
        json={
            "conduit_name": "x",
            "run_path": "/tmp",
            "schedule": {"mode": "weekly"},
        },
    )
    assert resp.status_code in (400, 422)


async def test_create_schedule_unknown_conduit_returns_400(fixture):
    """Verify POST /schedules with an unknown conduit_name returns 400.

    :param fixture: client+atelier+tmp_path tuple fixture.
    """
    client, _, _ = fixture
    resp = await client.post("/schedules", json=_payload(conduit_name="ghost"))
    assert resp.status_code == 400, resp.text
    assert "unknown conduit" in resp.text


async def test_create_schedule_unknown_input_returns_400(fixture):
    """Verify POST /schedules with an input the conduit cannot use returns 400.

    :param fixture: client+atelier+tmp_path tuple fixture.
    """
    client, atelier, _ = fixture
    resp = await client.post("/schedules", json=_payload(inputs={"fooo": "bar"}))
    assert resp.status_code == 400, resp.text
    detail = resp.json()["detail"]
    assert "unknown inputs: ['fooo']" in detail
    assert "did you mean 'foo' for 'fooo'?" in detail
    assert atelier.list_schedules() == []


async def test_create_schedule_past_run_at_rejected(fixture):
    """Verify a one-shot with a past run_at is rejected at validation.

    :param fixture: client+atelier+tmp_path tuple fixture.
    """
    client, _, _ = fixture
    resp = await client.post(
        "/schedules",
        json=_payload(
            schedule={
                "mode": "once",
                "name": "backfill",
                "run_at": "2000-01-01T00:00:00Z",
            }
        ),
    )
    assert resp.status_code == 422, resp.text


async def test_delete_schedule_removes_from_list(fixture):
    """Verify DELETE /schedules/<id> removes it from disk and from the list.

    :param fixture: client+atelier+tmp_path tuple fixture.
    """
    client, _, _ = fixture
    resp = await client.post("/schedules", json=_payload())
    sch_id = resp.json()["id"]

    resp = await client.delete(f"/schedules/{sch_id}")
    assert resp.status_code == 200
    assert resp.json()["id"] == sch_id

    listed = await client.get("/schedules")
    assert listed.json() == []


async def test_delete_unknown_returns_404(fixture):
    """Verify deleting an unknown schedule returns 404.

    :param fixture: client+atelier+tmp_path tuple fixture.
    """
    client, _, _ = fixture
    resp = await client.delete("/schedules/SCH-nope")
    assert resp.status_code == 404


async def test_create_logs_when_daemon_sync_fails(fixture, caplog):
    """Verify a failing daemon.sync() during create is logged, not swallowed.

    :param fixture: client+atelier+tmp_path tuple fixture.
    :param caplog: pytest log capture fixture.
    """
    import logging

    client, atelier, _ = fixture

    class _BoomDaemon:
        async def sync(self) -> None:
            raise RuntimeError("boom")

    atelier.scheduler_daemon = _BoomDaemon()
    with caplog.at_level(logging.WARNING, logger="flow_atelier.routes.schedules"):
        resp = await client.post("/schedules", json=_payload())
    assert resp.status_code == 201
    assert any("scheduler sync after create failed" in r.message for r in caplog.records)
