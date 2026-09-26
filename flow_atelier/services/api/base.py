"""Abstract HTTP server contract + DI helpers."""
from __future__ import annotations

import secrets
from abc import ABC, abstractmethod
from collections.abc import Iterable
from urllib.parse import urlsplit

from fastapi import FastAPI, HTTPException, Request, WebSocket

from flow_atelier.core.atelier import Atelier

_LOOPBACK = {"localhost", "127.0.0.1", "::1"}


class ApiServerBase(ABC):
    """Builds a FastAPI app bound to a single :class:`Atelier` instance."""

    @abstractmethod
    def create_app(
        self,
        atelier: Atelier,
        *,
        cors_origins: Iterable[str] | None = None,
        api_token: str | None = None,
        allowed_hosts: Iterable[str] | None = None,
    ) -> FastAPI:
        """Return a configured :class:`FastAPI` instance.

        :param atelier: facade to bind via dependency injection
        :param cors_origins: explicit CORS origins; ``None`` means
            localhost-only origins
        :param api_token: bearer token required on every request when set;
            ``None`` disables auth (local trust)
        :param allowed_hosts: accepted ``Host`` header values; ``None`` means
            loopback only (blocks DNS rebinding)
        """


def get_atelier(request: Request) -> Atelier:
    """FastAPI dependency: returns the :class:`Atelier` bound to the app.

    :param request: incoming FastAPI request whose app holds the facade
    """
    return request.app.state.atelier


def require_token(request: Request) -> None:
    """FastAPI dependency: enforce the bearer token when one is configured.

    :param request: incoming request whose app may hold ``state.api_token``
    :raises HTTPException: 401 when a token is set and the header is wrong
    """
    token = getattr(request.app.state, "api_token", None)
    if not token:
        return
    auth = request.headers.get("authorization", "")
    if not secrets.compare_digest(auth, f"Bearer {token}"):
        raise HTTPException(status_code=401, detail="invalid or missing API token")


def origin_allowed(websocket: WebSocket) -> bool:
    """Return whether the page that opened ``websocket`` may use it.

    CORS does not apply to WebSockets, and the ``Host`` pin does not help
    either: a page on any site can open ``ws://127.0.0.1:8000/...`` and the
    browser sends a loopback ``Host``. Only ``Origin`` names the page. A client
    that sends none is not a browser page and passes; a page must be local, a
    configured CORS origin, or served by this server.

    :param websocket: the incoming connection.
    :returns: ``True`` when the connection may proceed.
    """
    origin = websocket.headers.get("origin")
    if not origin:
        return True
    parts = urlsplit(origin)
    if parts.hostname in _LOOPBACK:
        return True
    if origin in getattr(websocket.app.state, "cors_origins", []):
        return True
    return parts.netloc == websocket.headers.get("host", "")
