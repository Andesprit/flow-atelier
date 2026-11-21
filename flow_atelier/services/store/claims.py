"""Durable FIFO intake shared by local producers and conduit runners."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import sqlite3
from collections.abc import Iterator
from contextlib import closing, contextmanager, suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from flow_atelier.schemas.conduit import CONDUIT_NAME_RE
from flow_atelier.schemas.flow import new_flow_id
from flow_atelier.services.store.events import RunJournal, file_lock, project_folder

logger = logging.getLogger(__name__)
DATABASE = "event-claims.sqlite3"


@dataclass(frozen=True)
class Claim:
    run_id: str
    status: str


class ClaimQueueFull(Exception):
    """All waiting positions are occupied."""


def expire_claim(home: Path, run_id: str) -> None:
    """Persist the tombstone before retention unlinks the last evidence of a run."""
    path = home / DATABASE
    if path.exists():
        with closing(sqlite3.connect(path)) as db, db:
            db.execute(
                "UPDATE claims SET status='history_expired', inputs=NULL WHERE run_id=?",
                (run_id,),
            )


class EventClaims:
    """One category of jobs sharing a machine-wide queue and concurrent-run cap."""

    def __init__(self, home: Path, category: str, *, queue_limit: int = 100) -> None:
        self.home = home.resolve()
        self.category = category
        self.queue_limit = queue_limit
        self.home.mkdir(parents=True, exist_ok=True)
        (self.home / DATABASE).touch(mode=0o600, exist_ok=True)
        with self._transaction() as db:
            db.execute(
                """CREATE TABLE IF NOT EXISTS claims (
                    category TEXT NOT NULL, key TEXT NOT NULL, run_id TEXT NOT NULL UNIQUE,
                    status TEXT NOT NULL, project TEXT NOT NULL, conduit TEXT NOT NULL,
                    inputs TEXT, PRIMARY KEY(category, key)
                )"""
            )
            db.execute("CREATE INDEX IF NOT EXISTS pending ON claims(category, status)")

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        with closing(sqlite3.connect(self.home / DATABASE)) as db, db:
            db.row_factory = sqlite3.Row
            db.execute("BEGIN IMMEDIATE")
            yield db

    def claim(self, key: str, project: Path, conduit: str, inputs: dict[str, Any]) -> Claim:
        """Return the first run for this key, even when the queue is full."""
        if not key or not CONDUIT_NAME_RE.fullmatch(conduit):
            raise ValueError("expected an event key and a valid conduit name")
        with self._transaction() as db:
            prior = db.execute(
                "SELECT run_id, status FROM claims WHERE category=? AND key=?",
                (self.category, key),
            ).fetchone()
            if prior:
                return Claim(*prior)
            waiting = db.execute(
                "SELECT COUNT(*) FROM claims WHERE category=? AND status='queued'",
                (self.category,),
            ).fetchone()[0]
            if waiting >= self.queue_limit:
                raise ClaimQueueFull
            result = Claim(new_flow_id(conduit), "queued")
            db.execute(
                "INSERT INTO claims VALUES (?, ?, ?, ?, ?, ?, ?)",
                (self.category, key, result.run_id, result.status, str(project.resolve()),
                 conduit, json.dumps(inputs)),
            )
            return result

    def take(self) -> sqlite3.Row | None:
        """Reserve the oldest waiting run before handing it to the engine."""
        with self._transaction() as db:
            return db.execute(
                """UPDATE claims SET status='running' WHERE rowid=(
                    SELECT rowid FROM claims WHERE category=? AND status='queued'
                    ORDER BY rowid LIMIT 1
                ) RETURNING run_id, project, conduit, inputs""", (self.category,),
            ).fetchone()

    def _recover(self) -> None:
        # Only the dispatch lease holder may decide whether a reserved run actually started.
        with self._transaction() as db:
            for row in db.execute(
                "SELECT run_id, project FROM claims WHERE category=? AND status='running'",
                (self.category,),
            ).fetchall():
                folder = self.home / "projects" / project_folder(Path(row["project"]))
                journal = RunJournal(folder / (row["run_id"] + ".jsonl"))
                try:
                    progress = journal.latest("progress")
                except FileNotFoundError:
                    retired = journal.path.with_suffix(".expired").exists()
                    status = "history_expired" if retired else "queued"
                else:
                    status = progress["status"]
                    if status == "running":
                        status = "pending" if progress.get("pending_ask") else "interrupted"
                db.execute(
                    """UPDATE claims SET status=?, inputs=CASE WHEN ?='queued' THEN inputs END
                       WHERE run_id=?""", (status, status, row["run_id"]),
                )

    async def _worker(self, stopped: asyncio.Event) -> None:
        from flow_atelier.core.atelier import Atelier
        from flow_atelier.core.settings import AtelierSettings

        while not stopped.is_set():
            row = self.take()
            if row is None:
                with suppress(TimeoutError):
                    await asyncio.wait_for(stopped.wait(), 0.5)
                continue
            project = Path(row["project"])
            try:
                if not project.is_dir():
                    raise FileNotFoundError(project)
                settings = AtelierSettings(
                    atelier_dir=project / ".atelier", global_atelier_dir=self.home,
                    _env_file=project / ".env",
                )
                atelier = Atelier(settings=settings)
                await atelier.run_conduit(
                    row["conduit"], json.loads(row["inputs"]), working_dir=project,
                    flow_id=row["run_id"],
                )
                progress = atelier.store.read_progress(row["run_id"])
                status = "pending" if progress.pending_ask else progress.status.value
            except Exception as exc:
                logger.error("Event run %s failed (%s)", row["run_id"], type(exc).__name__)
                status = "failed"
            with self._transaction() as db:
                db.execute(
                    "UPDATE claims SET status=?, inputs=NULL WHERE run_id=? AND status='running'",
                    (status, row["run_id"]),
                )

    async def dispatch(self, cap: int, stopped: asyncio.Event) -> None:
        """Drain on startup and accept new jobs until asked to finish active runs."""
        if cap < 1:
            raise ValueError("run cap must be positive")
        digest = hashlib.sha256(self.category.encode()).hexdigest()
        while not stopped.is_set():
            with file_lock(self.home / f".dispatch-{digest}.lock", wait=False) as owner:
                if owner:
                    self._recover()
                    async with asyncio.TaskGroup() as group:
                        for _ in range(cap):
                            group.create_task(self._worker(stopped))
                    return
            with suppress(TimeoutError):
                await asyncio.wait_for(stopped.wait(), 0.5)
