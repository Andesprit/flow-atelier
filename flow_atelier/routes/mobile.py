"""Project-scoped, server-owned launches for the local mobile dashboard."""
from __future__ import annotations

import logging
from pathlib import Path
from time import monotonic
from uuid import UUID

from anyio import CancelScope
from anyio.abc import TaskStatus
from fastapi import APIRouter, Depends, HTTPException, Request, Response

from flow_atelier.core.atelier import Atelier
from flow_atelier.core.settings import AtelierSettings
from flow_atelier.modules.engine import check_unknown_inputs
from flow_atelier.routes.conduits import list_conduits
from flow_atelier.schemas.api import ConduitDTO, InputAnswer, InputRequest
from flow_atelier.schemas.ws import RunMessage
from flow_atelier.services.api.base import get_atelier
from flow_atelier.services.api.run_inputs import RunScope, ServerHitlExecutor, ServerPromptSink
from flow_atelier.services.executor.prompt_sink import AskTurnSink

router = APIRouter(prefix="/mobile", tags=["mobile"])
logger = logging.getLogger(__name__)


def _project(base: Atelier, path: str, scope: RunScope | None = None) -> Atelier:
    folder = Path(path)
    if not folder.is_absolute() or not folder.is_dir():
        raise HTTPException(422, "Choose an existing absolute folder on the Atelier server.")
    folder = folder.resolve()
    settings = AtelierSettings(
        atelier_dir=folder / ".atelier", global_atelier_dir=base.settings.global_atelier_dir,
        _env_file=folder / ".env",
    )
    if scope is not None:
        # People answer through /mobile/runs/{flow_id}/inputs, in child conduits too.
        runner = Atelier(settings=settings, prompt_sink=ServerPromptSink(scope))
        runner.executors["tool:hitl"] = ServerHitlExecutor(scope)
        return runner
    runner = Atelier(settings=settings, prompt_sink=AskTurnSink())
    # A detached run has no terminal to read from, including in child conduits.
    runner.executors.pop("tool:hitl")
    return runner


@router.get("/projects")
def projects(atelier: Atelier = Depends(get_atelier)) -> list[str]:
    return sorted({str(path) for path in atelier.store.flow_projects(global_only=True).values()})


@router.get("/conduits")
async def project_conduits(
    project: str, atelier: Atelier = Depends(get_atelier),
) -> list[ConduitDTO]:
    return await list_conduits(_project(atelier, project))


class MobileLaunch(RunMessage):
    launch_id: UUID | None = None
    confirmed_folder: str | None = None
    # The client answers human-input steps and agent questions through
    # /mobile/runs/{flow_id}/inputs, so the run may wait for them.
    interactive: bool = False


@router.post("/runs", status_code=202)
async def launch(
    payload: MobileLaunch, request: Request, atelier: Atelier = Depends(get_atelier),
) -> dict[str, str]:
    state = request.app.state
    async with state.launch_lock:
        now = monotonic()
        state.launches = {key: entry for key, entry in state.launches.items() if entry[0] > now}
        if previous := state.launches.get(payload.launch_id):
            if previous[1] != payload:
                raise HTTPException(409, "This launch ID was already used for another request.")
            return {"flow_id": previous[2]}
        scope = RunScope(state.run_inputs) if payload.interactive else None
        runner = _project(atelier, payload.run_path, scope)
        folder = str(runner.settings.atelier_dir.parent)
        if payload.confirmed_folder != payload.run_path and folder not in projects(atelier):
            raise HTTPException(422, "Confirm this folder to run its own recipes.")
        with CancelScope(shield=True):
            flow_id = await _start(payload, runner, request, scope)
            if payload.launch_id:
                state.launches[payload.launch_id] = (monotonic() + 300, payload, flow_id)
        return {"flow_id": flow_id}


async def _start(
    payload: MobileLaunch, runner: Atelier, request: Request, scope: RunScope | None,
) -> str:

    async def execute(*, task_status: TaskStatus[str]) -> None:
        started = False

        def on_start(flow_id: str) -> None:
            nonlocal started
            if not started:  # Child runs share this callback.
                started = True
                if scope is not None:
                    scope.root = flow_id
                task_status.started(flow_id)

        try:
            conduit = runner.store.read_conduit(payload.conduit_name)
            check_unknown_inputs(conduit, payload.inputs, subject="mobile launch")
            if scope is None and any(task.tool == "tool:hitl" for task in conduit.tasks):
                raise ValueError("Use the interactive dashboard for human-input steps.")
            await runner.engine.run(
                conduit, payload.inputs, on_flow_started=on_start, show_steps=False,
                working_dir=runner.settings.atelier_dir.parent,
            )
        except Exception:
            if not started:
                raise
            logger.exception("Mobile run failed after launch")

    try:
        return await request.app.state.mobile_runs.start(execute)
    except FileNotFoundError as exc:
        raise HTTPException(404, "Conduit not found in the selected project") from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@router.get("/inputs")
def pending_inputs(request: Request, flow_id: str | None = None) -> list[InputRequest]:
    """What server-owned runs are waiting for, oldest first."""
    return request.app.state.run_inputs.pending(flow_id)


@router.post("/runs/{flow_id}/inputs/{request_id}", status_code=204)
async def answer_input(
    flow_id: str, request_id: str, payload: InputAnswer, request: Request,
) -> Response:
    """Answer one waiting request; the run continues with it."""
    try:
        request.app.state.run_inputs.answer(flow_id, request_id, payload)
    except KeyError as exc:
        raise HTTPException(
            404, "Nothing is waiting under this ID: it was answered, or the run ended."
        ) from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    return Response(status_code=204)
