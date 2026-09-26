"""/flows REST tests."""
from __future__ import annotations

import httpx
import pytest

from flow_atelier.core.atelier import Atelier
from flow_atelier.schemas.api import RunTaskInput
from flow_atelier.services.api.app import FastApiServer


@pytest.fixture
async def fixture(tmp_path, monkeypatch):
    """Yield an httpx client wired to a fresh Atelier instance.

    :param tmp_path: pytest temp directory fixture.
    :param monkeypatch: pytest monkeypatch fixture.
    """
    monkeypatch.delenv("ATELIER_GLOBAL_ATELIER_DIR", raising=False)
    atelier = Atelier(base_dir=tmp_path / ".atelier")
    app = FastApiServer().create_app(atelier)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://127.0.0.1", timeout=15.0
    ) as c:
        yield c, atelier


async def _seed_flow(atelier: Atelier, run_path: str = ".") -> str:
    """Run a one-off bash task to seed a completed flow.

    :param atelier: Atelier instance to run the task against.
    :param run_path: working directory for the bash subprocess.
    """
    out = await atelier.run_single_task(
        RunTaskInput(
            name="echo",
            description="d",
            task="echo flow-api-output",
            tool="tool:bash",
            run_path=run_path,
        )
    )
    return out.flow_id


async def test_list_flows_empty(fixture):
    """Verify GET /flows returns an empty list when no flows have run.

    :param fixture: client+atelier tuple fixture.
    """
    client, _ = fixture
    resp = await client.get("/flows")
    assert resp.status_code == 200
    assert resp.json() == []


async def test_list_flows_returns_completed(fixture, tmp_path):
    """Verify GET /flows includes the seeded completed flow.

    :param fixture: client+atelier tuple fixture.
    """
    client, atelier = fixture
    flow_id = await _seed_flow(atelier, run_path=str(tmp_path))
    resp = await client.get("/flows")
    assert resp.status_code == 200
    bodies = resp.json()
    assert any(item["flow_id"] == flow_id for item in bodies)
    target = next(item for item in bodies if item["flow_id"] == flow_id)
    assert target["status"] == "completed"


async def test_get_logs_round_trips(fixture, tmp_path):
    """Verify GET /flows/<id>/logs returns the recorded stdout entries.

    :param fixture: client+atelier tuple fixture.
    """
    client, atelier = fixture
    flow_id = await _seed_flow(atelier, run_path=str(tmp_path))
    resp = await client.get(f"/flows/{flow_id}/logs")
    assert resp.status_code == 200
    body = resp.json()
    assert isinstance(body, dict)
    logs = body["logs"]
    assert any(
        "flow-api-output" in (entry.get("stdout") or "") for entry in logs
    )


async def test_get_logs_unknown_returns_404(fixture):
    """Verify requesting logs for an unknown flow returns 404.

    :param fixture: client+atelier tuple fixture.
    """
    client, _ = fixture
    resp = await client.get("/flows/no_such_flow/logs")
    assert resp.status_code == 404


async def test_get_logs_rejects_encoded_dot_segment(fixture, tmp_path):
    """Verify %2E%2E cannot read a logs.jsonl outside the flows/ directory.

    httpx normalizes a literal `..` away, so the traversal only ever reached
    the route percent-encoded — which is exactly how it arrived over the wire.

    :param fixture: client+atelier tuple fixture.
    :param tmp_path: pytest temp directory fixture.
    """
    client, atelier = fixture
    (atelier.store.base_dir / "logs.jsonl").write_text(
        '{"task":"t","tool":"tool:bash","command":"c","exit_code":0,'
        '"output":"SECRET","stdout":"SECRET","stderr":"",'
        '"started_at":"2026-04-12T10:00:00Z","finished_at":"2026-04-12T10:00:00Z"}\n'
    )
    resp = await client.get("/flows/%2E%2E/logs")
    assert resp.status_code == 404
    assert "SECRET" not in resp.text


async def test_get_flow_view_lists_the_run_tasks(fixture, tmp_path):
    """Verify GET /flows/<id> returns the run's tasks and their progress.

    :param fixture: client+atelier tuple fixture.
    :param tmp_path: pytest temp directory fixture.
    """
    client, atelier = fixture
    flow_id = await _seed_flow(atelier, run_path=str(tmp_path))
    resp = await client.get(f"/flows/{flow_id}")
    assert resp.status_code == 200
    body = resp.json()
    assert body["flow_id"] == flow_id
    assert body["status"] == "completed"
    assert [(t["name"], t["status"]) for t in body["tasks"]] == [("echo", "completed")]


async def test_get_task_log_returns_the_task_output(fixture, tmp_path):
    """Verify GET /flows/<id>/tasks/<task>/log returns the command and its output.

    :param fixture: client+atelier tuple fixture.
    :param tmp_path: pytest temp directory fixture.
    """
    client, atelier = fixture
    flow_id = await _seed_flow(atelier, run_path=str(tmp_path))
    resp = await client.get(f"/flows/{flow_id}/tasks/echo/log")
    assert resp.status_code == 200
    body = resp.json()
    (only,) = body["rounds"]
    assert only["status"] == "completed"
    lines = [(line["kind"], line["text"]) for line in only["lines"]]
    assert ("run", "$ echo flow-api-output") in lines
    assert ("out", "flow-api-output") in lines


async def test_get_task_log_unknown_task_returns_404(fixture, tmp_path):
    """Verify asking for a task the flow does not have returns 404.

    :param fixture: client+atelier tuple fixture.
    :param tmp_path: pytest temp directory fixture.
    """
    client, atelier = fixture
    flow_id = await _seed_flow(atelier, run_path=str(tmp_path))
    resp = await client.get(f"/flows/{flow_id}/tasks/nope/log")
    assert resp.status_code == 404


async def test_get_flow_view_unknown_returns_404(fixture):
    """Verify the run page's map 404s for an unknown or traversing flow id.

    :param fixture: client+atelier tuple fixture.
    """
    client, _ = fixture
    assert (await client.get("/flows/no_such_flow")).status_code == 404
    assert (await client.get("/flows/%2E%2E")).status_code == 404
    assert (await client.get("/flows/%2E%2E/tasks/t/log")).status_code == 404
