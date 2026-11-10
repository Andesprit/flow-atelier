"""``/schedules`` REST routes."""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, status

from flow_atelier.core.atelier import Atelier
from flow_atelier.schemas.api import CreateScheduleInput, ScheduledJob
from flow_atelier.services.api.base import get_atelier

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/schedules", tags=["schedules"])


@router.get("", response_model=list[ScheduledJob])
async def list_schedules(atelier: Atelier = Depends(get_atelier)) -> list[ScheduledJob]:
    """Return every persisted :class:`ScheduledJob`.

    :param atelier: injected :class:`Atelier` facade.
    :returns: the scheduled jobs.
    """
    return atelier.list_schedules()


@router.post("", status_code=status.HTTP_201_CREATED, response_model=ScheduledJob)
async def create_schedule(
    payload: CreateScheduleInput, atelier: Atelier = Depends(get_atelier)
) -> ScheduledJob:
    """Create a scheduled job and hot-sync the embedded daemon if attached.

    :param payload: parsed :class:`CreateScheduleInput` body.
    :param atelier: injected :class:`Atelier` facade.
    :returns: the created :class:`ScheduledJob`.
    """
    try:
        job = atelier.create_schedule(payload)
    except (OSError, ValueError) as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    # Hot-register with the embedded daemon if one is attached.
    daemon = getattr(atelier, "scheduler_daemon", None)
    if daemon is not None:
        try:
            await daemon.sync()
        except Exception:  # noqa: BLE001 — daemon must survive sync errors
            logger.warning(
                "scheduler sync after create failed (schedule %s)",
                job.id,
                exc_info=True,
            )
    return job


@router.delete("/{schedule_id}", response_model=ScheduledJob)
async def delete_schedule(
    schedule_id: str, atelier: Atelier = Depends(get_atelier)
) -> ScheduledJob:
    """Delete a scheduled job and hot-sync the embedded daemon if attached.

    :param schedule_id: identifier of the scheduled job from the URL path.
    :param atelier: injected :class:`Atelier` facade.
    :returns: the deleted :class:`ScheduledJob`.
    """
    try:
        job = atelier.delete_schedule(schedule_id)
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    daemon = getattr(atelier, "scheduler_daemon", None)
    if daemon is not None:
        try:
            await daemon.sync()
        except Exception:  # noqa: BLE001
            logger.warning(
                "scheduler sync after delete failed (schedule %s)",
                schedule_id,
                exc_info=True,
            )
    return job
