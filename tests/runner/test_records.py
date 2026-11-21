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
