"""Read-only schedule discovery and scheduler-free serving."""
import json

import httpx
import uvicorn
from typer.testing import CliRunner

from flow_atelier.cli import app
from flow_atelier.schemas.api import CreateScheduleInput
from flow_atelier.services.scheduler import ScheduleStore


def test_read_only_discovery_then_offline_migration(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    local = ScheduleStore(tmp_path / "custom-project-store")
    shared = tmp_path / "custom-global-store"
    monkeypatch.setenv("ATELIER_ATELIER_DIR", str(local.atelier_dir))
    monkeypatch.setenv("ATELIER_GLOBAL_ATELIER_DIR", str(shared))
    job = local.create(CreateScheduleInput(
        conduit_name="report", run_path=".",
        schedule={"name": "report", "mode": "interval", "every_minutes": 60},
    ))
    runner = CliRunner()
    discovery = runner.invoke(app, ["list", "schedules", "--json"])
    assert discovery.exit_code == 0
    assert json.loads(discovery.stdout)["schedules"] == []
    assert "atelier schedule migrate" in discovery.stderr
    assert not shared.exists()
    assert local.list() == [job]

    for _ in range(2):
        migrated = runner.invoke(app, ["schedule", "migrate"])
        assert migrated.exit_code == 0
        assert str(shared / "schedules") in migrated.stdout
    assert local.list() == []
    assert [j.id for j in ScheduleStore(shared).list()] == [job.id]


def test_viewer_serves_without_starting_scheduler(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    shared = tmp_path / "global"
    monkeypatch.setenv("ATELIER_GLOBAL_ATELIER_DIR", str(shared))

    local = ScheduleStore(tmp_path / ".atelier")
    legacy = local.create(CreateScheduleInput(
        conduit_name="report", run_path=".",
        schedule={"name": "legacy", "mode": "interval", "every_minutes": 60},
    ))

    def unexpected_daemon(*args, **kwargs):
        raise AssertionError("viewer constructed a scheduler")

    async def serve_in_process(server):
        clock = server.config.app
        async with clock.app.router.lifespan_context(clock.app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=clock), base_url="http://127.0.0.1"
            ) as client:
                assert (await client.get("/flows")).json() == []
            server.started = True

    monkeypatch.setattr("flow_atelier.cli.commands.serve.SchedulerDaemon", unexpected_daemon)
    monkeypatch.setattr(uvicorn.Server, "serve", serve_in_process)
    result = CliRunner().invoke(app, ["serve", "--port", "8755", "--no-scheduler"])
    assert result.exit_code == 0, result.exception
    assert f"Project store: {(tmp_path / '.atelier').resolve()}" in result.stdout
    assert "Scheduler disabled" in result.stdout
    assert local.list() == [legacy]
    assert not shared.exists()
