import json
import socket

import pytest

from flow_atelier.schemas.runner import Job, Record, Records, Result
from flow_atelier.services.runner.worker import Worker
from tests.runner.plane import Plane
from tests.runner.test_dispatch import prepared


async def test_filter_at_rest_on_wire_and_replay(tmp_path, monkeypatch):
    plane = Plane()
    local, fid = prepared(tmp_path, plane, "user")
    foreign, _ = prepared(tmp_path, plane, "other", "two")
    project = tmp_path / "project"
    canary, private = "plantedEnvCanary", "knownLocalSetting"
    monkeypatch.setenv("CANARY", canary)
    (local.root / "redact.json").write_text(json.dumps([private]))
    (project / "session.jsonl").write_text(json.dumps({"text": canary}))
    helper = project / ".atelier/conduits/run/helper"
    helper.write_text("cat session.jsonl; echo \"$CANARY\"; echo ran >> count")
    local.approve(fid, "run")
    jid = plane.enqueue("one", {local.identity()["id"]: fid}, "run")
    worker = Worker(local, plane)
    await worker.tick()
    await worker.active
    job = Job(id=jid, folder=fid, conduit="run")
    contaminants = [canary, private, "ghp_" + "x" * 36, "sk-proj-" + "Z" * 32,
                    str(project), socket.gethostname()]
    for kind in ("event", "log", "chat"):
        for value in contaminants:
            worker.events.append(job, kind, {"status": "completed", "task": value,
                "env": {"CANARY": canary}, "stdout": canary, "text": private, "path": str(project)})
    with local.db() as db:
        buffered = str([tuple(r) for r in db.execute("SELECT * FROM records")])
        assert all(value not in buffered for value in contaminants)
    plane.drop_next = "records"
    with pytest.raises(ConnectionError):
        await worker.tick()
    # Reopen the database and request an overlapping prefix, as on reconnect.
    restarted = Worker(local, plane)
    await restarted.tick()
    restarted.events.replay(1, plane)
    records = plane.records_for("one", local.identity()["id"])
    assert [r["sequence"] for r in records] == list(range(1, len(records) + 1))
    assert {r["kind"] for r in records} == {"log", "event", "chat"}
    assert any(r.get("task") == "step" for r in records)
    assert all(r["folder"] == fid for r in records)
    assert all(v not in json.dumps(plane.traffic) for v in contaminants)
    assert all(key not in json.dumps(plane.traffic) for key in ['"env"', '"stdout"', '"text"'])
    with pytest.raises(PermissionError):
        plane.records_for("two", local.identity()["id"])
    assert plane.records_for("two", foreign.identity()["id"]) == []
    forged = Record(sequence=1, job=jid, folder=fid, kind="event", status="started")
    with pytest.raises(PermissionError):
        foreign.send(plane, "records", Records(records=[forged]))
    with pytest.raises(PermissionError):
        foreign.send(plane, "result", Result(job=jid, status="completed"))


async def test_replay_batches_and_updated_local_redactions(tmp_path):
    plane = Plane()
    local, fid = prepared(tmp_path, plane, "user")
    jid = plane.enqueue("one", {local.identity()["id"]: fid}, "run")
    worker = Worker(local, plane)
    await worker.tick()
    await worker.active
    job = Job(id=jid, folder=fid, conduit="run")
    for _ in range(101):
        worker.events.append(job, "chat", {"status": "delivered", "task": "wasPublic"})
    (local.root / "redact.json").write_text('["wasPublic"]')
    await worker.tick()
    assert len(plane.records_for("one", local.identity()["id"])) == 100
    await worker.tick()
    records = plane.records_for("one", local.identity()["id"])
    assert len(records) == 104
    assert [r["sequence"] for r in records] == list(range(1, 105))
    assert "wasPublic" not in json.dumps(plane.traffic)

@pytest.mark.parametrize("cleanup_days", [None, 0])
async def test_retention_keeps_unacknowledged_records_and_sequence(tmp_path, cleanup_days):
    plane = Plane()
    local, fid = prepared(tmp_path, plane, "user")
    if cleanup_days is not None:
        (local.user_dir / "settings.json").write_text('{"cleanupPeriodDays": 0}')
    rid = local.identity()["id"]
    jid = plane.enqueue("one", {rid: fid}, "run")
    worker = Worker(local, plane)
    await worker.tick()
    await worker.active
    bundle = local.root / "runs" / jid / "conduits"
    assert bundle.is_dir()
    # The first poll after completion uploads the records; the next confirms the cursor.
    await Worker(local, plane).tick()
    await Worker(local, plane).tick()
    with local.db() as db:
        assert db.execute("SELECT COUNT(*) FROM records").fetchone()[0] == (
            3 if cleanup_days is None else 0
        )
    assert bundle.is_dir() == (cleanup_days is None)
    assert len(plane.records_for("one", rid)) == 3
    plane.enqueue("one", {rid: fid}, "run")
    restarted = Worker(local, plane)
    await restarted.tick()
    await restarted.active
    await restarted.tick()
    assert [r["sequence"] for r in plane.records_for("one", rid)] == list(range(1, 7))


def test_migrate_existing_outbox_and_unpair_before_new_tenant(tmp_path):
    import sqlite3

    from flow_atelier.services.runner.local import Local

    state = tmp_path / "user" / "runner"
    state.mkdir(parents=True)
    with sqlite3.connect(state / "runner.sqlite3") as db:
        db.executescript("""
            CREATE TABLE records(sequence INTEGER PRIMARY KEY, body TEXT NOT NULL);
            INSERT INTO records VALUES (41, '{}');
            CREATE TABLE jobs(job TEXT PRIMARY KEY,folder TEXT,conduit TEXT,run TEXT,state TEXT);
            INSERT INTO jobs VALUES ('old','old','private','old','refused');
        """)
    plane = Plane()
    local = Local(state.parent)
    local.pair(plane, plane.invite("one"))
    fid = local.expose(tmp_path, "Private")
    local.poll(plane)
    with local.db() as db:
        assert db.execute("SELECT sequence FROM runner_state").fetchone()[0] == 41
        assert db.execute("SELECT created FROM records").fetchone()[0] > 0
        assert db.execute("SELECT reason FROM jobs").fetchone()[0] is None
    local.unpair()
    local.pair(plane, plane.invite("two"))
    local.poll(plane)
    assert fid not in str(plane.view("two"))
    with local.db() as db:
        assert db.execute("SELECT sequence FROM runner_state").fetchone()[0] == 0
        for table in ("records", "jobs"):
            assert db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0


async def test_unremovable_bundle_does_not_stop_dispatch(tmp_path, monkeypatch, capsys):
    import shutil

    plane = Plane()
    local, fid = prepared(tmp_path, plane, "user")
    (local.user_dir / "settings.json").write_text('{"cleanupPeriodDays": 0}')
    old = local.root / "runs" / ("a" * 32) / "conduits"
    old.mkdir(parents=True)
    remove = shutil.rmtree

    def deny_old(path, *args, **kwargs):
        if path == old:
            raise PermissionError("private file")
        return remove(path, *args, **kwargs)

    monkeypatch.setattr(shutil, "rmtree", deny_old)
    job = plane.enqueue("one", {local.identity()["id"]: fid}, "run")
    worker = Worker(local, plane)
    await worker.tick()
    await worker.active
    assert plane.job_view("one", job) == "completed"
    diagnostic = capsys.readouterr().err
    assert "cleanup" in diagnostic and "private file" not in diagnostic
