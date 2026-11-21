"""Authenticated browser push registration and run-link resolution."""

from __future__ import annotations

import re
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, HTTPException, Response
from py_vapid import Vapid
from pydantic import BaseModel, Field, field_validator

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
        Vapid.from_raw_public(value.encode("ascii"))
        return value


class Subscription(Device):
    keys: Keys


def store(atelier: Atelier = Depends(get_atelier)) -> WebPush:
    return WebPush(atelier.settings.global_atelier_dir)


@router.get("/key")
def key(push: WebPush = Depends(store)) -> dict[str, str]:
    return {"key": push.public_key()}


@router.post("/subscriptions", status_code=204)
def subscribe(payload: Subscription, push: WebPush = Depends(store)) -> Response:
    push.subscribe(payload.endpoint, payload.keys.model_dump())
    return Response(status_code=204)


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
