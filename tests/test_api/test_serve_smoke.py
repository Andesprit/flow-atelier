"""End-to-end smoke test for ``atelier serve``."""
from __future__ import annotations

import asyncio
import json
import os
import re
import sys
from copy import deepcopy
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from typer.testing import CliRunner

from flow_atelier.cli import app
from flow_atelier.cli.commands.create import _FIX_LOOP
from flow_atelier.services.scheduler import ScheduleStore
from tests._shell import CLI


@pytest.mark.timeout(30)
async def test_serve_smoke_boots_and_serves_conduits(tmp_path, monkeypatch):
    """Exercise the real CLI server from a different project than the schedule's owner."""
    owner = tmp_path / "owner"
    viewer = tmp_path / "viewer"
    viewer.mkdir()
    shared = tmp_path / "global"
    recipe = owner / ".atelier/conduits/report/conduit.yaml"
    recipe.parent.mkdir(parents=True)
    recipe.write_text(
        "name: report\ndescription: test\ntasks:\n  - record:\n"
        "      description: record working directory\n      tool: tool:bash\n"
        "      task: echo fired >> fires.txt\n"
    )
    monkeypatch.chdir(owner)
    monkeypatch.setenv("ATELIER_GLOBAL_ATELIER_DIR", str(shared))
    monkeypatch.delenv("ATELIER_ATELIER_DIR", raising=False)
    monkeypatch.delenv("ATELIER_API_TOKEN", raising=False)

    server = await asyncio.create_subprocess_exec(
        sys.executable, "-u", "-c", CLI, "serve",
        "--port", os.environ.get("ATELIER_TEST_PORT", "0"), "--reload-interval", "0.1",
        "--log-level", "warning", cwd=viewer, stdout=asyncio.subprocess.PIPE,
    )
    try:
        async with asyncio.timeout(10):
            startup = (await server.stdout.readline()).decode()
            url = re.search(r"http://127\.0\.0\.1:\d+", startup).group()
            assert (await server.stdout.readline()).decode().strip() == (
                f"Project store: {(viewer / '.atelier').resolve()}"
            )
            assert (await server.stdout.readline()).decode().strip() == (
                f"Schedule store: {(shared / 'schedules').resolve()}"
            )
        async with httpx.AsyncClient(base_url=url) as c:
            resp = await c.get("/conduits", timeout=5.0)
            assert resp.status_code == 200
            assert resp.json() == []

            # A designer-style full PATCH must retain the inline loop and the
            # chosen model/effort after a real HTTP save and reload.
            payload = {"name": "ship", **deepcopy(_FIX_LOOP)}
            created = await c.post("/conduits", json=payload)
            assert created.status_code == 201, created.text
            before = (await c.get("/conduits/ship")).json()
            editor_payload = {
                key: before[key]
                for key in ("name", "description", "inputs", "timeout", "max_concurrency", "interaction", "tasks")
            }
            updated = await c.patch("/conduits/ship", json=editor_payload)
            assert updated.status_code == 200, updated.text
            after = (await c.get("/conduits/ship")).json()
            assert after == before
            assert after["tasks"][0]["tasks"][0]["tool"] == "harness:claude-code"

            editor_payload["tasks"][0]["tasks"][0]["tool"] = "harness:codex:gpt-5.6-sol:high"
            swapped = await c.patch("/conduits/ship", json=editor_payload)
            assert swapped.status_code == 200, swapped.text
            chosen = (await c.get("/conduits/ship")).json()
            assert chosen["tasks"][0]["tasks"][0]["tool"] == "harness:codex:gpt-5.6-sol:high"


            # The scheduler is already running in viewer. Install two jobs from owner.
            schedule_file = owner / "job.json"
            for name, delay in (("later", 3600), ("soon", 2)):
                fire_at = datetime.now(UTC) + timedelta(seconds=delay)
                schedule_file.write_text(json.dumps({
                    "conduit_name": "report", "run_path": ".",
                    "schedule": {"name": name, "mode": "once", "run_at": fire_at.isoformat()},
                }))
                added = CliRunner().invoke(app, ["schedule", "add", str(schedule_file)])
                assert added.exit_code == 0, added.output
                assert str(shared / "schedules") in added.stdout

            response = await c.get("/schedules")
            response.raise_for_status()
            jobs = {j["schedule"]["name"]: j for j in response.json()}
            assert set(jobs) == {"soon", "later"}
            assert jobs["soon"]["run_path"] == str(owner.resolve())
            store = ScheduleStore(shared)
            async with asyncio.timeout(10):
                while not store.run_history(jobs["soon"]["id"]):
                    await asyncio.sleep(0.05)
            assert [r.status for r in store.run_history(jobs["soon"]["id"])] == ["succeeded"]
            assert (owner / "fires.txt").read_text().splitlines() == ["fired"]
            assert store.run_history(jobs["later"]["id"]) == []
            assert not (viewer / "fires.txt").exists()
    finally:
        if server.returncode is None:
            server.terminate()
        await server.wait()
