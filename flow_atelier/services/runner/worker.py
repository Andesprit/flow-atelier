"""Execute a bound claim once, even if its result or the process is lost."""

import asyncio
import http.client
from pathlib import Path

from flow_atelier.core.atelier import Atelier
from flow_atelier.core.settings import AtelierSettings
from flow_atelier.schemas.flow import new_flow_id
from flow_atelier.schemas.runner import Job, Result
from flow_atelier.services.runner.local import Local
from flow_atelier.services.store.events import file_lock


class Worker:
    def __init__(self, local: Local, client):
        self.local = local
        self.client = client
        self.token = local.identity()["token"]
        self.active: asyncio.Task | None = None
        with local.db() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS jobs (
                job TEXT PRIMARY KEY, folder TEXT, conduit TEXT, run TEXT, state TEXT)""")

    def report(self, job: Job, status: str) -> None:
        self.client.post("result", Result(job=job.id, status=status), self.token)

    def dispatch(self, job: Job) -> None:
        # A second daemon (or a retry while the first is executing) cannot infer a crash.
        with file_lock(self.local.root / "execution.lock", wait=False) as owned:
            if not owned:
                return
            with self.local.db() as db:
                row = db.execute("SELECT * FROM jobs WHERE job=?", (job.id,)).fetchone()
            if row and (row["folder"], row["conduit"]) != (job.folder, job.conduit):
                raise ValueError("control plane changed an existing job")
            if row and row["state"] != "prepared":
                state = row["state"]
                if state == "started":
                    state = "unknown"
                    self._save(job, row["run"], state)
                self.report(job, state)
                return
            run = row["run"] if row else new_flow_id(job.conduit)
            try:
                with self.local.snapshot(job.folder, job.conduit) as (project, copy):
                    if row is None:
                        self._save(job, run, "prepared")
                    # A lost acknowledgement leaves the mapping prepared for retry.
                    self.report(job, "started")
                    self.local.folder_path(job.folder)
                    self._save(job, run, "started")
                    base = self.local.root / "runs" / job.id
                    base.mkdir(parents=True, exist_ok=True)
                    (copy / "conduits").rename(base / "conduits")
            except (ValueError, FileNotFoundError, NotADirectoryError):
                self._save(job, run, "refused")
                self.report(job, "refused")
                return

            state = self._execute(base, project, job, run)
            self._save(job, run, state)
            self.report(job, state)

    def _execute(self, base: Path, project: Path, job: Job, run: str) -> str:
        try:
            settings = AtelierSettings(atelier_dir=project / ".atelier",
                                       global_atelier_dir=self.local.user_dir,
                                       _env_file=project / ".env")
            atelier = Atelier(settings=settings.model_copy(update={"atelier_dir": base}))
            recipe = atelier.store.read_conduit(job.conduit)
            asyncio.run(atelier.engine.run(recipe, {}, working_dir=project, flow_id=run,
                                           show_steps=False))
            status = atelier.store.read_progress(run).status.value
            return status if status in ("completed", "failed") else "unknown"
        except Exception:
            # Raw errors can contain shell output or credentials. Run details stay local.
            return "failed"

    def _save(self, job: Job, run: str, state: str) -> None:
        with self.local.db() as db:
            db.execute("INSERT OR REPLACE INTO jobs VALUES (?,?,?,?,?)",
                       (job.id, job.folder, job.conduit, run, state))

    async def tick(self) -> None:
        if self.active and self.active.done():
            finished, self.active = self.active, None
            finished.result()
        reply = await asyncio.to_thread(self.local.poll, self.client, claim=self.active is None)
        if reply.job and self.active is None:
            self.active = asyncio.create_task(asyncio.to_thread(self.dispatch, reply.job))

    async def serve(self) -> None:
        with file_lock(self.local.root / "poll.lock", wait=False) as owned:
            if not owned:
                raise ValueError("this runner is already polling")
            while True:
                try:
                    await self.tick()
                except PermissionError:
                    raise
                except (OSError, http.client.HTTPException):
                    pass
                if self.active:
                    await asyncio.wait({self.active}, timeout=10)
                else:
                    await asyncio.sleep(10)
