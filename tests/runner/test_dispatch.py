import asyncio
from concurrent.futures import ThreadPoolExecutor

import pytest

from flow_atelier.services.runner.local import Local
from flow_atelier.services.runner.worker import Worker
from tests.runner.plane import Plane
from tests.runner.test_registration import recipe, step


def prepared(tmp_path, plane, user, tenant="one"):
    project = tmp_path / "project"
    project.mkdir(exist_ok=True)
    directory = recipe(project, "run", [step("tool:bash", 'bash "{{conduit_dir}}/helper"')])
    (directory / "helper").write_text("echo ran >> count")
    sub = tmp_path / user
    local = Local(sub)
    fid = local.expose(project, "manuals")
    local.approve(fid, "run")
    local.pair(plane, plane.invite(tenant))
    local.poll(plane)
    return local, fid


async def test_race_lost_claim_and_lost_finish(tmp_path):
    plane = Plane()
    a, fa = prepared(tmp_path, plane, "a")
    b, fb = prepared(tmp_path, plane, "b")
    jid = plane.enqueue("one", {a.identity()["id"]: fa, b.identity()["id"]: fb}, "run")
    plane.drop_next = "poll"
    with ThreadPoolExecutor(2) as pool:
        results = [pool.submit(r.poll, plane) for r in (a, b)]
        for res in results:
            try:
                assert res.result().job is None
            except ConnectionError:
                pass
    winner, loser = (a, b) if plane.jobs[jid]["owner"] == a.identity()["id"] else (b, a)
    assert loser.poll(plane).job is None
    # Restart before learning of the claim, then lose the terminal response too.
    worker = Worker(Local(winner.user_dir), plane)
    job = winner.poll(plane).job
    plane.drop_next = "completed"
    with pytest.raises(ConnectionError):
        await asyncio.to_thread(worker.dispatch, job)
    worker = Worker(Local(winner.user_dir), plane)
    await asyncio.to_thread(worker.dispatch, job)
    assert (tmp_path / "project/count").read_text().splitlines() == ["ran"]
    with winner.db() as db:
        row, = db.execute("SELECT * FROM jobs")
        assert row["job"] == jid and row["state"] == "completed"
        assert len(list((winner.user_dir / "projects").rglob(row["run"] + ".jsonl"))) == 1


@pytest.mark.parametrize("alter", ["recipe", "helper", "unlisted", "foreign"])
async def test_hostile_dispatch_fails_before_execution(tmp_path, alter):
    plane = Plane()
    local, folder = prepared(tmp_path, plane, "user")
    directory = tmp_path / "project/.atelier/conduits/run"
    name = "run"
    if alter in ("recipe", "helper"):
        with (directory / ("helper" if alter == "helper" else "conduit.yaml")).open("a") as stream:
            stream.write("\n# edited")
    elif alter == "unlisted":
        name = "other"
    else:
        folder = "f" * 32
    jid = plane.enqueue("one", {local.identity()["id"]: folder}, name, hostile=True)
    worker = Worker(local, plane)
    await worker.tick()
    await worker.active
    assert plane.jobs[jid]["state"] == "refused"
    assert not (tmp_path / "project/count").exists()


def test_started_expiry_and_restart_never_relaunch(tmp_path):
    plane = Plane()
    local, fid = prepared(tmp_path, plane, "user")
    jid = plane.enqueue("one", {local.identity()["id"]: fid}, "run")
    job = local.poll(plane).job
    worker = Worker(local, plane)
    with local.db() as db:
        db.execute("INSERT INTO jobs VALUES (?,?,?,?,?)", (jid, fid, "run", "existing", "started"))
    from flow_atelier.schemas.runner import Result
    plane.post("result", Result(job=jid, status="started"), local.identity()["token"])
    plane.now += 61
    assert plane.job_view("one", jid) == "unknown"
    assert not plane.view("one")[0]["online"]
    worker.dispatch(job)
    assert plane.job_view("one", jid) == "unknown"
    assert not (tmp_path / "project/count").exists()
