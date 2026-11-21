"""Read-only schedule discovery and scheduler-free serving."""
import json
import os

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


def test_corrupt_legacy_state_is_reported_by_migrate_add_and_serve(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    shared = ScheduleStore(tmp_path / "global")
    monkeypatch.setenv("ATELIER_GLOBAL_ATELIER_DIR", str(shared.atelier_dir))
    local = ScheduleStore(tmp_path / ".atelier")
    payload = CreateScheduleInput(
        conduit_name="hello", inputs={"name": "world"}, run_path=".",
        schedule={"name": "legacy", "mode": "once", "run_at": "2099-01-01T00:00Z"},
    )
    legacy = local.create(payload)
    source, = local.schedules_dir.glob("*.yaml")
    local.state_path.write_text("{broken")
    runner = CliRunner()
    result = runner.invoke(app, ["schedule", "migrate"])
    assert result.exit_code == 0, result.exception
    assert "moved 0, kept 1" in result.stdout
    warning = f"kept {source}: invalid scheduler state in {local.state_path}"
    assert warning in result.stderr
    assert local.list() == [legacy]
    assert shared.list() == []

    assert runner.invoke(app, ["init"]).exit_code == 0
    new_payload = payload.model_dump(mode="json")
    new_payload["schedule"]["name"] = "fresh"
    new_file = tmp_path / "fresh.json"
    new_file.write_text(json.dumps(new_payload))
    added = runner.invoke(app, ["schedule", "add", str(new_file)])
    assert added.exit_code == 0, added.output
    assert warning in added.stderr
    assert [job.schedule.name for job in shared.list()] == ["fresh"]

    async def serve_in_process(server):
        app = server.config.app
        async with app.app.router.lifespan_context(app.app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1"
            ) as client:
                assert (await client.get("/flows")).status_code == 200
            server.started = True

    monkeypatch.setattr(uvicorn.Server, "serve", serve_in_process)
    with shared.acquire_daemon_lease():
        served = runner.invoke(app, ["serve"])
    assert served.exit_code == 0, served.exception
    assert warning in served.stderr
    assert f"scheduler already running (pid {os.getpid()}); serving without scheduler" in served.stderr
    assert f"Scheduler disabled (running in pid {os.getpid()})" in served.stdout.splitlines()
    assert "Schedule store:" not in served.stdout
    assert local.state_path.read_text() == "{broken"


def test_migrate_explains_name_conflict_and_moves_other_files(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    shared = ScheduleStore(tmp_path / "global")
    monkeypatch.setenv("ATELIER_GLOBAL_ATELIER_DIR", str(shared.atelier_dir))
    local = ScheduleStore(tmp_path / ".atelier")
    payload = CreateScheduleInput(
        conduit_name="report", run_path=".",
        schedule={"name": "nightly report", "mode": "interval", "every_minutes": 60},
    )
    conflict = local.create(payload)
    source, = local.schedules_dir.glob("*.yaml")
    installed = shared.create(payload)
    payload.schedule.name = "other"
    other = local.create(payload)
    result = CliRunner().invoke(app, ["schedule", "migrate"])
    assert result.exit_code == 0, result.exception
    assert "moved 1, kept 1" in result.stdout
    assert f"kept {source}: name conflict for 'nightly report'" in result.stderr
    assert "change schedule.name" in result.stderr
    assert local.list() == [conflict]
    assert {job.id for job in shared.list()} == {installed.id, other.id}
