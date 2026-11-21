import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from typer.testing import CliRunner

from flow_atelier.cli.main import app
from flow_atelier.core.atelier import Atelier
from flow_atelier.modules import engine, liveness
from flow_atelier.schemas.conduit import Conduit
from flow_atelier.schemas.log import ExecutionResult
from flow_atelier.schemas.progress import PendingAsk
from flow_atelier.services.api.flow_watch import FlowWatcher


@pytest.mark.parametrize("tool", ["tool:bash", "tool:hitl"])
async def test_silent_owner_and_sleep_wake(tmp_path, monkeypatch, tool):
    monkeypatch.chdir(tmp_path)
    atlas = Atelier()
    recipe = Conduit.model_validate(
        {
            "name": "quiet",
            "description": "quiet",
            "tasks": [
                {
                    "name": "step",
                    "task": "wait",
                    "tool": tool,
                    "description": "wait",
                    "depends_on": [],
                }
            ],
        }
    )
    atlas.store.write_conduit(recipe)
    start = datetime.now(UTC)
    clock = start
    monkeypatch.setattr(engine, "_now", lambda: clock.isoformat())
    monkeypatch.setattr(liveness, "time", lambda: clock.timestamp())
    # Intercept the heartbeat timer only. Advance wall time separately to model suspend.
    loop = asyncio.get_running_loop()
    later = loop.call_later
    beats = []

    def schedule(delay, callback, *args, **kwargs):
        if delay == 15:
            beats.append(callback)
            delay = 86400
        return later(delay, callback, *args, **kwargs)

    monkeypatch.setattr(loop, "call_later", schedule)
    entered, finish = asyncio.Event(), asyncio.Event()

    async def quiet(*args, **kwargs):
        entered.set()
        await finish.wait()
        return ExecutionResult()

    monkeypatch.setattr(atlas.executors[tool], "execute", quiet)
    ids = []
    run = asyncio.create_task(atlas.engine.run(recipe, {}, on_flow_started=ids.append))
    try:
        await asyncio.wait_for(entered.wait(), 3)
        [fid] = ids
        watcher = FlowWatcher(atlas, fid)
        assert watcher.view.display_status == "running"
        assert atlas.get_status(fid).heartbeat_at == start
        clock += timedelta(seconds=15)
        beats.pop(0)()
        assert atlas.get_status(fid).heartbeat_at == clock
        clock += timedelta(minutes=3)
        assert liveness.display_status(atlas.get_status(fid)) == "crashed"
        assert watcher.poll()[0]["flow"]["display_status"] == "crashed"
        assert atlas.list_prior_flows()[0].status == "crashed"
        with atlas.store.lock_writer(fid) as acquired:
            assert not acquired
        beats.pop(0)()
        assert watcher.poll()[0]["flow"]["display_status"] == "running"
        monkeypatch.setattr(liveness, "_pid_state", lambda pid: liveness._PidState.dead)
        assert liveness.display_status(atlas.get_status(fid)) == "crashed"
    finally:
        finish.set()
        await run
    assert liveness.display_status(atlas.get_status(fid)) == "completed"


def test_saved_question_after_heartbeat_is_waiting(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    atlas = Atelier()
    fid = atlas.store.create_flow("ask", {}, project=tmp_path)
    progress = atlas.get_status(fid)
    progress.runner_pid = None
    progress.runner_host = None
    progress.heartbeat_at = datetime.now(UTC) - timedelta(days=1)
    progress.pending_ask = PendingAsk(
        request_id="q",
        question="Continue?",
        session_id="s",
        tool="harness:demo",
        permissions="edits",
        timeout=20,
    )
    atlas.store.write_progress(fid, progress)
    assert liveness.display_status(atlas.get_status(fid)) == "needs_input"
    assert not liveness.is_crashed(atlas.get_status(fid))
    assert atlas.get_flow_view(fid).display_status == "needs_input"
    assert "needs_input" in CliRunner().invoke(app, ["status", fid]).stdout
