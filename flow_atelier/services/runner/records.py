"""The only event upload path. Store and send a closed, filtered projection."""

import time

from flow_atelier.schemas.runner import Job, Record, Records
from flow_atelier.services.runner.local import Local
from flow_atelier.services.runner.privacy import private_values, public_name


class Events:
    def __init__(self, local: Local):
        self.local = local
        self.token = local.identity()["token"]

    def _project(self, job: Job, kind: str, data: dict, sequence: int) -> Record:
        # Only the structured task name can carry local text. A path or host drops out,
        # leaving the locally resolved folder ID as the sole location on wire.
        task = public_name(data.get("task"), private_values(self.local.root, self.token))
        return Record(sequence=sequence, job=job.id, folder=job.folder, kind=kind,
                      status=data["status"], task=task)

    def append(self, job: Job, kind: str, data: dict) -> None:
        with self.local.db() as db:
            db.execute("BEGIN IMMEDIATE")
            self.store(db, job, kind, data)

    def store(self, db, job: Job, kind: str, data: dict) -> None:
        if not db.execute("SELECT 1 FROM jobs JOIN folders ON jobs.folder=folders.id "
                          "WHERE jobs.job=? AND jobs.folder=? AND jobs.conduit=?",
                          (job.id, job.folder, job.conduit)).fetchone():
            raise ValueError("record has no local job mapping")
        db.execute("UPDATE runner_state SET sequence=sequence+1")
        seq = db.execute("SELECT sequence FROM runner_state").fetchone()[0]
        record = self._project(job, kind, data, seq)
        db.execute("INSERT INTO records(sequence,body,created) VALUES (?,?,?)",
                   (seq, record.model_dump_json(exclude_none=True), time.time()))

    def replay(self, after: int, client) -> None:
        with self.local.db() as db:
            rows = db.execute("SELECT sequence,body FROM records WHERE sequence>? "
                              "ORDER BY sequence LIMIT 100", (after,)).fetchall()
        if rows:
            records = [Record.model_validate_json(row["body"]) for row in rows]
            self.local.send(client, "records", Records(records=records))
