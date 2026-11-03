"""``/conduits`` REST routes."""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import Response

from flow_atelier.cli.rendering.render import format_conduit_error
from flow_atelier.core.atelier import Atelier
from flow_atelier.schemas.api import (
    ConduitDTO,
    CreateConduitInput,
    OpenPathInput,
    OpenPathOutput,
    UpdateConduitInput,
)
from flow_atelier.schemas.conduit import CONDUIT_NAME_RE
from flow_atelier.services.api.base import get_atelier

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/conduits", tags=["conduits"])


def _default_run_path(atelier: Atelier) -> str:
    """Working directory a run should default to: the project `.atelier/` holds.

    Matches the CLI, where `atelier run` always writes flows under the
    `.atelier/` of the directory it was invoked from.

    :param atelier: the bound facade.
    :returns: absolute path as a string.
    """
    return str(atelier.settings.atelier_dir.parent)


@router.get("", response_model=list[ConduitDTO])
async def list_conduits(atelier: Atelier = Depends(get_atelier)) -> list[ConduitDTO]:
    """List every conduit visible to the facade (project + global).

    A conduit whose file does not load is left out and logged, so one bad
    ``conduit.yaml`` cannot fail the whole list.

    :param atelier: injected :class:`Atelier` facade.
    :returns: :class:`ConduitDTO` for each readable conduit.
    """
    out: list[ConduitDTO] = []
    for name in atelier.list_conduits():
        try:
            conduit = atelier.store.read_conduit(name)
        except FileNotFoundError:
            continue
        except Exception as e:  # noqa: BLE001 — one broken file must not fail the list
            logger.warning(
                "skipping unreadable conduit %s: %s", name, format_conduit_error(e)
            )
            continue
        out.append(ConduitDTO.from_conduit(conduit, _default_run_path(atelier)))
    return out


@router.get("/{name}", response_model=ConduitDTO)
async def get_conduit(name: str, atelier: Atelier = Depends(get_atelier)) -> ConduitDTO:
    """Return one conduit by name, 404 if missing or 422 if its file is broken.

    :param name: conduit identifier from the URL path.
    :param atelier: injected :class:`Atelier` facade.
    :returns: the matching :class:`ConduitDTO`.
    """
    # The store raises ValueError for ``..`` or a path separator, which would
    # read as a broken conduit. An inline loop body is not served on its own.
    if not CONDUIT_NAME_RE.fullmatch(name):
        raise HTTPException(status_code=404, detail=f"conduit not found: {name}")
    try:
        conduit = atelier.store.read_conduit(name)
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    except (OSError, ValueError) as e:
        raise HTTPException(status_code=422, detail=format_conduit_error(e)) from e
    return ConduitDTO.from_conduit(conduit, _default_run_path(atelier))


@router.post("", status_code=status.HTTP_201_CREATED, response_model=ConduitDTO)
async def create_conduit(
    payload: CreateConduitInput, atelier: Atelier = Depends(get_atelier)
) -> ConduitDTO:
    """Create a new conduit, mapping known errors to HTTP status codes.

    :param payload: parsed :class:`CreateConduitInput` body.
    :param atelier: injected :class:`Atelier` facade.
    :returns: the created :class:`ConduitDTO`.
    """
    try:
        conduit = atelier.create_conduit(payload)
    except FileExistsError as e:
        raise HTTPException(status_code=409, detail=str(e)) from e
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    return ConduitDTO.from_conduit(conduit, _default_run_path(atelier))


@router.patch("/{name}", response_model=ConduitDTO)
async def update_conduit(
    name: str,
    payload: UpdateConduitInput,
    atelier: Atelier = Depends(get_atelier),
) -> ConduitDTO:
    """Patch an existing conduit, mapping known errors to HTTP status codes.

    :param name: conduit identifier from the URL path.
    :param payload: parsed :class:`UpdateConduitInput` body.
    :param atelier: injected :class:`Atelier` facade.
    :returns: the updated :class:`ConduitDTO`.
    """
    try:
        conduit = atelier.update_conduit(name, payload)
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    except FileExistsError as e:
        raise HTTPException(status_code=409, detail=str(e)) from e
    except (OSError, ValueError) as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    return ConduitDTO.from_conduit(conduit, _default_run_path(atelier))


@router.delete("/{name}")
async def delete_conduit(name: str, atelier: Atelier = Depends(get_atelier)) -> Response:
    """Delete a conduit by name, returning 204 on success or 404 if missing.

    :param name: conduit identifier from the URL path.
    :param atelier: injected :class:`Atelier` facade.
    :returns: empty 204 response.
    """
    try:
        deleted = atelier.delete_conduit(name)
    except ValueError as e:
        # ``..`` or a path separator: the store refuses it, so no such conduit.
        raise HTTPException(status_code=404, detail=f"conduit not found: {name}") from e
    if not deleted:
        raise HTTPException(status_code=404, detail=f"conduit not found: {name}")
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/open-path", response_model=OpenPathOutput)
async def open_path(
    payload: OpenPathInput, atelier: Atelier = Depends(get_atelier)
) -> OpenPathOutput:
    """Open a path in the host OS file explorer.

    :param payload: parsed :class:`OpenPathInput` body.
    :param atelier: injected :class:`Atelier` facade.
    :returns: whether the path was opened.
    """
    return OpenPathOutput(opened=atelier.open_conduit_path(payload.run_path))
