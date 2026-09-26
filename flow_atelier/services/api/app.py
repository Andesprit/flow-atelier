"""FastAPI app factory.

Builds a FastAPI instance bound to a single :class:`Atelier` facade.
Routes are mounted from :mod:`flow_atelier.routes`. Business logic lives on the
facade; routes do at most: validate → call facade → serialize.
"""
from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from starlette.datastructures import Headers
from starlette.responses import PlainTextResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from flow_atelier import __version__
from flow_atelier.core.atelier import Atelier
from flow_atelier.services.api.base import ApiServerBase

# Bundled SPA assets. Lives inside the package (flow_atelier/dist/) so it ships
# in the wheel and is found both from a source checkout and an installed wheel.
_STATIC_DIR = Path(__file__).resolve().parents[2] / "dist"

# Host header values that legitimately reach a loopback-bound server. CORS
# alone cannot defend this API: in a DNS-rebinding attack the attacker's page
# resolves its *own* hostname to 127.0.0.1, so the browser treats the request
# as same-origin and never sends an Origin the CORS layer could reject. Pinning
# Host is what actually closes it — without this, any page a user visits while
# `atelier serve` runs can POST /tasks/run and execute shell commands.
LOOPBACK_HOSTS = ["localhost", "127.0.0.1", "::1"]


def _header_host(raw: str) -> str:
    """Extract the hostname from a ``Host`` header value.

    Strips the port and, for IPv6 literals, the surrounding brackets:
    ``[::1]:8000`` -> ``::1``.

    :param raw: raw ``Host`` header value, possibly empty.
    :returns: the lowercased hostname, or ``""`` when unparseable.
    """
    try:
        return (urlsplit(f"//{raw}").hostname or "").lower()
    except ValueError:
        return ""


class HostHeaderMiddleware:
    """Reject requests whose ``Host`` header is not explicitly allowed.

    Stands in for Starlette's ``TrustedHostMiddleware``, which parses the
    header as ``host.split(":")[0]``. That turns ``[::1]:8000`` into ``[``,
    so an IPv6 loopback entry can never match and the server answers every
    request to ``http://[::1]:port`` with ``400 Invalid host header`` —
    including the WebSocket the UI depends on. :func:`urlsplit` handles the
    bracketed form, and does not depend on a quirk of the installed
    Starlette.

    A rejected WebSocket is closed with a policy-violation code rather than
    handed an HTTP response, which is not valid on a ``websocket`` scope.
    """

    def __init__(self, app: ASGIApp, allowed_hosts: Iterable[str]) -> None:
        """Wrap ``app``, admitting only ``allowed_hosts``.

        :param app: the downstream ASGI application.
        :param allowed_hosts: accepted hostnames; ``"*"`` admits everything.
        """
        self.app = app
        self.allowed = {h.lower() for h in allowed_hosts}
        self.allow_any = "*" in self.allowed

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Pass the request through when its ``Host`` is allowed, else reject.

        :param scope: ASGI connection scope.
        :param receive: ASGI receive channel.
        :param send: ASGI send channel.
        """
        if self.allow_any or scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return
        if _header_host(Headers(scope=scope).get("host", "")) in self.allowed:
            await self.app(scope, receive, send)
            return
        if scope["type"] == "websocket":
            await send({"type": "websocket.close", "code": 1008})
            return
        await PlainTextResponse("Invalid host header", status_code=400)(
            scope, receive, send
        )


class FastApiServer(ApiServerBase):
    """Default :class:`ApiServerBase` implementation."""

    def create_app(
        self,
        atelier: Atelier,
        *,
        cors_origins: Iterable[str] | None = None,
        api_token: str | None = None,
        allowed_hosts: Iterable[str] | None = None,
    ) -> FastAPI:
        """Build the FastAPI app, attach CORS, and register all routes.

        :param atelier: facade exposed via ``app.state.atelier``
        :param cors_origins: explicit CORS origins; ``None`` means
            localhost-only origins (the API can run shell commands, so a
            wildcard would let any webpage drive it cross-origin)
        :param api_token: bearer token required on every request when set;
            ``None`` disables auth (local trust)
        :param allowed_hosts: accepted ``Host`` header values; ``None`` means
            loopback only. See :data:`LOOPBACK_HOSTS` for why this matters.
        """
        app = FastAPI(title="flow-atelier", version=__version__)
        app.state.atelier = atelier
        app.state.api_token = api_token or ""
        app.state.cors_origins = list(cors_origins or [])

        if cors_origins:
            app.add_middleware(
                CORSMiddleware,
                allow_origins=list(cors_origins),
                allow_credentials=True,
                allow_methods=["*"],
                allow_headers=["*"],
            )
        else:
            app.add_middleware(
                CORSMiddleware,
                allow_origin_regex=r"http://(localhost|127\.0\.0\.1)(:\d+)?",
                allow_credentials=True,
                allow_methods=["*"],
                allow_headers=["*"],
            )

        # Added last, so it is the outermost layer: `add_middleware` prepends,
        # and the Host check has to run before CORS gets to answer a preflight
        # on behalf of a host we do not accept.
        app.add_middleware(
            HostHeaderMiddleware,
            allowed_hosts=list(allowed_hosts) if allowed_hosts else LOOPBACK_HOSTS,
        )

        # Routes mount themselves below — imported here to avoid
        # circular imports between routes and the app factory.
        from flow_atelier.routes import register_routes

        register_routes(app)

        # SPA static files — registered last so API/WS routes take priority.
        if _STATIC_DIR.is_dir():

            app.mount(
                "/assets",
                StaticFiles(directory=_STATIC_DIR / "assets"),
                name="spa-assets",
            )

            @app.get("/favicon.svg")
            async def _favicon():
                return FileResponse(_STATIC_DIR / "favicon.svg")

            @app.get("/{full_path:path}")
            async def _spa_fallback(full_path: str):
                return FileResponse(_STATIC_DIR / "index.html")

        return app
