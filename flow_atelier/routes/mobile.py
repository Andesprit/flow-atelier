"""Project-scoped, server-owned launches for the local mobile dashboard."""
from __future__ import annotations

import logging
from pathlib import Path

from anyio.abc import TaskStatus
from fastapi import APIRouter, Depends, HTTPException, Request

from flow_atelier.core.atelier import Atelier
from flow_atelier.core.settings import AtelierSettings
from flow_atelier.modules.engine import check_unknown_inputs
from flow_atelier.routes.conduits import list_conduits
from flow_atelier.schemas.api import ConduitDTO
from flow_atelier.schemas.ws import RunMessage
from flow_atelier.services.api.base import get_atelier
from flow_atelier.services.executor.prompt_sink import AskTurnSink

router = APIRouter(prefix="/mobile", tags=["mobile"])
logger = logging.getLogger(__name__)


def _project(base: Atelier, path: str) -> Atelier:
    folder = Path(path)
    if not folder.is_absolute() or not folder.is_dir():
        raise HTTPException(422, "Choose an existing absolute folder on the Atelier server.")
    folder = folder.resolve()
    settings = AtelierSettings(
        atelier_dir=folder / ".atelier", global_atelier_dir=base.settings.global_atelier_dir,
        _env_file=folder / ".env",
    )
    runner = Atelier(settings=settings, prompt_sink=AskTurnSink())
    # A detached run has no terminal to read from, including in child conduits.
    runner.executors.pop("tool:hitl")
    return runner


@router.get("/projects")
def projects(atelier: Atelier = Depends(get_atelier)) -> list[str]:
    current = str(atelier.settings.atelier_dir.parent.resolve())
    known = {run.project for run in atelier.list_prior_flows() if run.project}
    return [current, *sorted(known - {current})]


@router.get("/conduits")
async def project_conduits(
    project: str, atelier: Atelier = Depends(get_atelier),
) -> list[ConduitDTO]:
    return await list_conduits(_project(atelier, project))


@router.post("/runs", status_code=202)
async def launch(
    payload: RunMessage, request: Request, atelier: Atelier = Depends(get_atelier),
) -> dict[str, str]:
    runner = _project(atelier, payload.run_path)

    async def execute(*, task_status: TaskStatus[str]) -> None:
        started = False

        def on_start(flow_id: str) -> None:
            nonlocal started
            if not started:  # Child runs share this callback.
                started = True
                task_status.started(flow_id)

        try:
            conduit = runner.store.read_conduit(payload.conduit_name)
            check_unknown_inputs(conduit, payload.inputs, subject="mobile launch")
            await runner.engine.run(
                conduit, payload.inputs, on_flow_started=on_start, show_steps=False,
                working_dir=runner.settings.atelier_dir.parent,
            )
        except Exception:
            if not started:
                raise
            logger.exception("Mobile run failed after launch")

    try:
        flow_id = await request.app.state.mobile_runs.start(execute)
    except FileNotFoundError as exc:
        raise HTTPException(404, "Conduit not found in the selected project") from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    return {"flow_id": flow_id}

