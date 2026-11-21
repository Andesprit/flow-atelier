"""Authenticated browser push registration and run-link resolution."""

from __future__ import annotations

import re
from importlib.util import find_spec
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import AnyHttpUrl, BaseModel, Field, ValidationError, field_validator

from flow_atelier.core.atelier import Atelier
from flow_atelier.services.api.base import get_atelier
from flow_atelier.services.web_push import WebPush

router = APIRouter(prefix="/push", tags=["push"])


class Device(BaseModel):
    endpoint: str = Field(max_length=4096)

    @field_validator("endpoint")
    @classmethod
    def push_provider(cls, value: str) -> str:
        url = urlsplit(value)
        host = url.hostname or ""
        trusted = host in {
            "fcm.googleapis.com",
            "updates.push.services.mozilla.com",
        } or re.fullmatch(r"(?:[a-z0-9-]+\.)+(?:push\.apple\.com|notify\.windows\.com)", host)
        if (
            not trusted
            or url.scheme != "https"
            or url.netloc != host
            or not url.path.strip("/")
            or url.fragment
        ):
            raise ValueError("Unsupported browser push endpoint")
        return value


class Keys(BaseModel):
    p256dh: str = Field(pattern=r"^B[A-Za-z0-9_-]{86}$")
    auth: str = Field(pattern=r"^[A-Za-z0-9_-]{22}$")

    @field_validator("p256dh")
    @classmethod
    def valid_point(cls, value: str) -> str:
        push_installed()
        from py_vapid import Vapid

        Vapid.from_raw_public(value.encode("ascii"))
        return value


class Subscription(Device):
    keys: Keys


def push_installed() -> None:
    if find_spec("pywebpush") is None:
        raise HTTPException(503, "Install flow-atelier[push] on the server to enable Web Push.")


def store(atelier: Atelier = Depends(get_atelier)) -> WebPush:
    return WebPush(atelier.settings.global_atelier_dir)


@router.get("/key")
def key(push: WebPush = Depends(store)) -> dict[str, str]:
    push_installed()
    return {"key": push.public_key()}


@router.post("/subscriptions", status_code=204)
def subscribe(payload: Subscription, request: Request, push: WebPush = Depends(store)) -> Response:
    try:
        origin = AnyHttpUrl(request.headers.get("origin") or str(request.base_url))
    except ValidationError as exc:
        raise HTTPException(422, "Invalid subscription origin.") from exc
    if origin.username or origin.password or origin.path != "/" or origin.query or origin.fragment:
        raise HTTPException(422, "Invalid subscription origin.")
    subject = str(origin).rstrip("/") if origin.scheme == "https" else None
    if not subject and not request.app.state.atelier.settings.vapid_subject:
        raise HTTPException(422, "Subscribe over HTTPS or configure vapidSubject in user settings.")
    push.subscribe(payload.endpoint, payload.keys.model_dump(), subject)
    return Response(status_code=204)


@router.post("/status")
def delivery_status(payload: Device, push: WebPush = Depends(store)) -> dict[str, str | None]:
    push_installed()
    return {"last_error": push.last_error(payload.endpoint)}


@router.delete("/subscriptions", status_code=204)
def unsubscribe(payload: Device, push: WebPush = Depends(store)) -> Response:
    push.unsubscribe(payload.endpoint)
    return Response(status_code=204)


@router.get("/runs/{token}")
def resolve_notice(token: str, push: WebPush = Depends(store)) -> dict[str, str]:
    flow_id = push.resolve(token)
    if not flow_id:
        raise HTTPException(404, "This completion link has expired.")
    return {"flow_id": flow_id}
