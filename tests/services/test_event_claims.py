import asyncio
import multiprocessing

import pytest

from flow_atelier.core.settings import AtelierSettings
from flow_atelier.services.store.claims import ClaimQueueFull, EventClaims
from flow_atelier.services.store.retention import cleanup_runs


def race(home, project, barrier, results):
    barrier.wait(timeout=10)
    store = EventClaims(home, "errors")
    results.put(store.claim("same", project, "demo", {"report": "first"}))


def test_two_processes_claim_one_id(tmp_path):
    ctx = multiprocessing.get_context("spawn")
    barrier, results = ctx.Barrier(2), ctx.Queue()
    workers = [
        ctx.Process(target=race, args=(tmp_path / "home", tmp_path, barrier, results))
        for _ in range(2)
    ]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(15)
        assert worker.exitcode == 0
    first, second = results.get(timeout=1), results.get(timeout=1)
    assert first == second
    assert first.status == "queued"


async def test_restart_drains_bounded_queue_preserving_ids(tmp_path):
    home = AtelierSettings().global_atelier_dir
    recipe = tmp_path / ".atelier/conduits/demo/conduit.yaml"
    recipe.parent.mkdir(parents=True)
    recipe.write_text("""name: demo
description: Test claim execution
inputs:
  label: {description: Test label}
tasks:
  - name: record
    description: Record actual dispatch
    tool: tool:bash
    task: |
      echo '{{inputs.label}}' >> starts
      while [ ! -f release ]; do sleep .05; done
""")
    store = EventClaims(home, "errors", queue_limit=3)
    claims = [store.claim(k, tmp_path, "demo", {"label": k}) for k in "abc"]
    assert len({claim.run_id for claim in claims}) == 3
    with pytest.raises(ClaimQueueFull):
        store.claim("d", tmp_path, "demo", {"label": "d"})
    assert store.claim("a", tmp_path, "demo", {}) == claims[0]

    # Simulate a dispatcher that died after reserving work, before publishing a journal.
    assert store.take()["run_id"] == claims[0].run_id
    stopped = asyncio.Event()
    workers = [
        asyncio.create_task(EventClaims(home, "errors").dispatch(2, stopped))
        for _ in range(2)
    ]
    try:
        async with asyncio.timeout(10):
            while store.claim("c", tmp_path, "demo", {}).status == "queued":
                starts = tmp_path / "starts"
                if starts.exists() and len(starts.read_text().splitlines()) == 2:
                    break
                await asyncio.sleep(.02)
            assert sorted(starts.read_text().splitlines()) == ["a", "b"]
            assert store.claim("c", tmp_path, "demo", {}).status == "queued"
            (tmp_path / "release").touch()
            while store.claim("c", tmp_path, "demo", {}).status != "completed":
                await asyncio.sleep(.02)
    finally:
        (tmp_path / "release").touch()
        stopped.set()
        await asyncio.gather(*workers)

    assert sorted(starts.read_text().splitlines()) == ["a", "b", "c"]
    cleanup_runs(home, 0)
    expired = EventClaims(home, "errors").claim("a", tmp_path, "demo", {})
    assert expired.run_id == claims[0].run_id
    assert expired.status == "history_expired"
    assert store.take() is None
