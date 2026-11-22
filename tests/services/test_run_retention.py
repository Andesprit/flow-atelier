import asyncio
import json
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from flow_atelier.core.atelier import Atelier
from flow_atelier.core.settings import AtelierSettings
from flow_atelier.schemas.progress import PendingAsk, Progress
from flow_atelier.services.api.app import FastApiServer
from flow_atelier.services.scheduler.runner import SchedulerDaemon
from flow_atelier.services.store import retention
from flow_atelier.services.store.filesystem import FilesystemStore


@pytest.mark.parametrize("configured", [False, True], ids=["missing", "null"])
async def test_unset_retention_keeps_old_history(tmp_path, monkeypatch, configured):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("ATELIER_CLEANUP_PERIOD_DAYS", raising=False)
    home = AtelierSettings().global_atelier_dir
    if configured:
        (home / "settings.json").write_text('{"cleanupPeriodDays":null}')
    atlas = Atelier()
    fid = atlas.store.create_flow("old", {}, project=tmp_path)
    atlas.store.write_progress(
        fid, Progress(status="completed", finished_at="2000-01-01T00:00:00Z")
    )
    api = FastApiServer().create_app(atlas)
    retention.cleanup_runs(home, atlas.settings.cleanup_period_days)
    async with api.router.lifespan_context(api):
        assert atlas.store.list_project_flows() == [fid]


@pytest.mark.parametrize("service", ["server", "scheduler"])
async def test_retention_at_startup_and_again_next_day(tmp_path, monkeypatch, service):
    monkeypatch.chdir(tmp_path)
    settings = AtelierSettings()
    home = settings.global_atelier_dir
    home.mkdir(exist_ok=True)
    (home / "settings.json").write_text('{"cleanupPeriodDays":30}')
    atlas = Atelier()
    store = atlas.store
    now = datetime(2030, 1, 1, tzinfo=UTC)
    monkeypatch.setattr(retention, "_now", lambda: now)
    monkeypatch.setattr(retention, "DAY_SECONDS", 0.025)

    def seed(name, days, *, parent=None, status="completed", pending=False):
        fid = store.create_flow(name, {}, parent_flow_id=parent, project=tmp_path)
        progress = Progress(
            status=status,
            finished_at=(now - timedelta(days=days)).isoformat(),
            pending_ask=PendingAsk(
                request_id="q",
                question="?",
                session_id="s",
                tool="harness:fake",
                permissions="edits",
                timeout=5,
            )
            if pending
            else None,
        )
        store.write_progress(fid, progress)
        return store.journal(fid)

    old = seed("old", 31)
    child = seed("child", 33, parent=old.path.stem)
    store.write_outputs(child.path.stem, {"task": "a" * 100001})
    recent = seed("recent", 29.5)
    active = seed("active", 60, status="running")
    pending = seed("ask", 60, pending=True)
    parent = seed("parent", 31)
    busy_child = seed("nested", 60, parent=parent.path.stem, status="running")
    locked = seed("locked", 40)
    old_parent = seed("old_parent", 40)
    fresh_child = seed("fresh_child", 3, parent=old_parent.path.stem)
    checkout = tmp_path / ".atelier/workspaces" / old.path.stem
    checkout.mkdir(parents=True)
    (checkout / "keep").write_text("work")
    legacy = FilesystemStore(store.base_dir)
    legacy_id = legacy.create_flow("legacy", {})
    legacy.write_progress(
        legacy_id, Progress(status="completed", finished_at="2020-01-01T00:00:00Z")
    )

    @asynccontextmanager
    async def scheduler():
        daemon = SchedulerDaemon(atlas.schedule_store)
        await daemon.start()
        try:
            yield
        finally:
            await daemon.stop()

    api = FastApiServer().create_app(atlas)
    lifecycle = scheduler() if service == "scheduler" else api.router.lifespan_context(api)
    with store.lock_writer(locked.path.stem) as held:
        assert held
        async with lifecycle:
            for record in (old, child):
                assert not record.path.exists()
                assert not record.side.exists()
            assert recent.path.exists()
            now += timedelta(days=1)
            async with asyncio.timeout(3):
                while recent.path.exists() or recent.side.exists():
                    await asyncio.sleep(0.01)
            for record in (active, pending, parent, busy_child, old_parent, fresh_child, locked):
                assert record.path.exists() and record.side.exists()
            assert (checkout / "keep").read_text() == "work"
            assert legacy.read_progress(legacy_id).status == "completed"


def test_zero_and_invalid_retention(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    atlas = Atelier()
    store = atlas.store
    home = atlas.settings.global_atelier_dir
    fid = store.create_flow("done", {}, project=tmp_path)
    store.write_progress(
        fid, Progress(status="completed", finished_at=datetime.now(UTC).isoformat())
    )
    (home / "settings.json").write_text(json.dumps({"cleanupPeriodDays": -1}))
    with pytest.raises(ValidationError, match="cleanup_period_days"):
        AtelierSettings()
    (home / "settings.json").write_text('{"cleanupPeriodDays":0}')
    retention.cleanup_runs(home, 10**12)
    assert fid in store.list_project_flows()
    retention.cleanup_runs(home, AtelierSettings().cleanup_period_days)
    assert not store.list_project_flows()
