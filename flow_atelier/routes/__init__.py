"""FastAPI routers.

Each module mounts a single router for one resource. The
:func:`register_routes` helper attaches each router to the app, kept in
this module so the app factory and ``atelier serve`` command share one
mount point.
"""
from __future__ import annotations

from fastapi import Depends, FastAPI


def register_routes(app: FastAPI) -> None:
    """Register every resource router on the given FastAPI app.

    :param app: FastAPI application that receives the routers.
    """
    from flow_atelier.routes import conduits, flows, flows_ws, schedules, tasks, ws
    from flow_atelier.services.api.base import require_token

    # REST routers enforce the optional bearer token; the WS routes check
    # their ?token= query param themselves (browser WS cannot set headers).
    deps = [Depends(require_token)]
    app.include_router(conduits.router, dependencies=deps)
    app.include_router(schedules.router, dependencies=deps)
    app.include_router(tasks.router, dependencies=deps)
    app.include_router(flows.router, dependencies=deps)
    app.include_router(ws.router)
    app.include_router(flows_ws.router)
