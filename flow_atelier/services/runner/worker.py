"""Execute a bound claim once, even if its result or the process is lost."""

import asyncio
import re
import shutil
import sqlite3
import sys
import time
from contextlib import ExitStack
from pathlib import Path

from flow_atelier.core.atelier import Atelier
from flow_atelier.core.settings import AtelierSettings
from flow_atelier.schemas.flow import new_flow_id
from flow_atelier.schemas.runner import Job, Result
from flow_atelier.services.runner.client import RunnerResponseError
from flow_atelier.services.runner.local import Local, PolicyError
from flow_atelier.services.runner.records import Events
from flow_atelier.services.store.events import file_lock
from flow_atelier.services.store.retention import DAY_SECONDS


class Worker:
    def __init__(self, local: Local, client):
        self.local = local
        self.client = client
        self.active: asyncio.Task | None = None
        self.events = Events(local)
        self.cleanup_days = AtelierSettings(global_atelier_dir=local.user_dir).cleanup_period_days
        self.next_cleanup = 0.0

    def _record_error(self, operation: str, error: Exception) -> None:
        detail = str(error) if isinstance(error, RunnerResponseError) else type(error).__name__
        message = f"{operation}: {detail}"
        print(message, file=sys.stderr, flush=True)
        try:
            with self.local.db() as db:
                db.execute("UPDATE runner_state SET last_error=?", (message,))
        except sqlite3.Error as store_error:
            print(f"Cannot save last error: {type(store_error).__name__}", file=sys.stderr,
                  flush=True)

    def _cleanup(self, after: int, days: int) -> None:
        cutoff = time.time() - days * DAY_SECONDS
        with self.local.db() as db:
            db.execute("DELETE FROM records WHERE sequence<=? AND created<=?", (after, cutoff))
        runs = self.local.root / "runs"
        with file_lock(self.local.root / "execution.lock", wait=False) as idle:
            if not idle:
                return
            if runs.is_dir() and runs.resolve() == runs:
                for directory in runs.iterdir():
                    if not re.fullmatch(r"[0-9a-f]{32}", directory.name):
                        continue
                    bundle = directory / "conduits"
                    if (bundle.is_dir() and bundle.resolve() == bundle
                            and directory.stat().st_mtime <= cutoff):
                        shutil.rmtree(bundle)
            self.next_cleanup = time.monotonic() + DAY_SECONDS

    def report(self, job: Job, status: str) -> None:
        self.local.send(self.client, "result", Result(job=job.id, status=status))

    def _refuse(self, job: Job, run: str, error: Exception) -> None:
        reason = str(error) if isinstance(error, PolicyError) else (
            f"cannot verify local bundle ({type(error).__name__})")
        self._save(job, run, "refused", reason)
        print(f"Job {job.id} refused: {reason}", file=sys.stderr, flush=True)
        self.report(job, "refused")

    def dispatch(self, job: Job) -> None:
        # A second daemon (or a retry while the first is executing) cannot infer a crash.
        with file_lock(self.local.root / "execution.lock", wait=False) as owned:
            if not owned:
                return
            print(f"Job {job.id} claimed", file=sys.stderr, flush=True)
            with self.local.db() as db:
                row = db.execute("SELECT * FROM jobs WHERE job=?", (job.id,)).fetchone()
            if row and (row["folder"], row["conduit"]) != (job.folder, job.conduit):
                raise ValueError("control plane changed an existing job")
            if row and row["state"] != "prepared":
                state = row["state"]
                if state == "started":
                    state = "unknown"
                    self._save(job, row["run"], state)
                detail = f": {row['reason']}" if row["reason"] else ""
                print(f"Job {job.id} {state}{detail}", file=sys.stderr, flush=True)
                self.report(job, state)
                return
            run = row["run"] if row else new_flow_id(job.conduit)
            with ExitStack() as files:
                try:
                    bundle = self.local.snapshot(job.folder, job.conduit)
                    project, copy = files.enter_context(bundle)
                except (ValueError, OSError) as error:
                    self._refuse(job, run, error)
                    return
                if row is None:
                    self._save(job, run, "prepared")
                # A lost acknowledgement leaves the mapping prepared for retry.
                self.report(job, "started")
                try:
                    self.local.folder_path(job.folder)
                except (ValueError, OSError) as error:
                    self._refuse(job, run, error)
                    return
                self._save(job, run, "started")
                base = self.local.root / "runs" / job.id
                base.mkdir(parents=True, exist_ok=True)
                (copy / "conduits").rename(base / "conduits")

            state = self._execute(base, project, job, run)
            self._save(job, run, state)
            print(f"Job {job.id} finished: {state}", file=sys.stderr, flush=True)
            self.report(job, state)

    def _execute(self, base: Path, project: Path, job: Job, run: str) -> str:
        try:
            settings = AtelierSettings(atelier_dir=project / ".atelier",
                                       global_atelier_dir=self.local.user_dir,
                                       _env_file=project / ".env")
            atelier = Atelier(settings=settings.model_copy(update={"atelier_dir": base}))
            recipe = atelier.store.read_conduit(job.conduit)

            def task_finished(event):
                self.events.append(job, "log", {"task": event.task, "status": event.status.value})

            asyncio.run(atelier.engine.run(recipe, {}, working_dir=project, flow_id=run,
                                           on_task_event=task_finished,
                                           show_steps=False))
            status = atelier.store.read_progress(run).status.value
            return status if status in ("completed", "failed") else "unknown"
        except Exception:
            # Raw errors can contain shell output or credentials. Run details stay local.
            return "failed"

    def _save(self, job: Job, run: str, state: str, reason: str | None = None) -> None:
        with self.local.db() as db:
            db.execute("INSERT OR REPLACE INTO jobs(job,folder,conduit,run,state,reason) "
                       "VALUES (?,?,?,?,?,?)",
                       (job.id, job.folder, job.conduit, run, state, reason))
            if state in ("started", "completed", "failed"):
                self.events.store(db, job, "event", {"status": state})

    async def tick(self) -> None:
        if self.active and self.active.done():
            finished, self.active = self.active, None
            finished.result()
        reply = await asyncio.to_thread(self.local.poll, self.client, claim=self.active is None)
        if self.cleanup_days is not None and time.monotonic() >= self.next_cleanup:
            try:
                await asyncio.to_thread(self._cleanup, reply.after, self.cleanup_days)
            except (OSError, sqlite3.Error) as error:
                self.next_cleanup = time.monotonic() + DAY_SECONDS
                self._record_error("Runner cleanup", error)
        await asyncio.to_thread(self.events.replay, reply.after, self.client)
        if reply.job and self.active is None:
            self.active = asyncio.create_task(asyncio.to_thread(self.dispatch, reply.job))

    async def serve(self) -> None:
        with file_lock(self.local.root / "poll.lock", wait=False) as owned:
            if not owned:
                raise ValueError("this runner is already polling")
            while True:
                try:
                    await self.tick()
                except Exception as error:
                    self._record_error("Runner error", error)
                    if isinstance(error, PermissionError):
                        raise
                    await asyncio.sleep(10)
                    continue
                if self.active:
                    await asyncio.wait({self.active}, timeout=10)
                else:
                    await asyncio.sleep(10)
