"""Signed error intake, served independently of the Atelier API and dashboard."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import re
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from starlette.concurrency import run_in_threadpool

from flow_atelier.core.settings import AtelierSettings
from flow_atelier.schemas.conduit import CONDUIT_NAME_RE
from flow_atelier.services.store.claims import Claim, ClaimQueueFull, EventClaims

MAX_REPORT_BYTES = 64 * 1024


def _fingerprint(report: object) -> list:
    if not isinstance(report, dict):
        raise HTTPException(422, "Expected an error report object")
    fingerprint = report.get("fingerprint")
    if isinstance(fingerprint, str) and fingerprint.strip():
        return ["fingerprint", fingerprint]
    exception = report.get("exception_class")
    frames = report.get("frames")
    if isinstance(exception, str) and exception.strip() and isinstance(frames, list) and frames:
        stable = []
        for frame in frames:
            if not isinstance(frame, dict) or not all(
                isinstance(frame.get(k), str) and frame[k].strip() for k in ("module", "function")
            ):
                raise HTTPException(422, "Each frame needs a module and function")
            stable.append([frame["module"], frame["function"]])
        return ["stack", exception, stable]
    raise HTTPException(422, "Expected fingerprint or exception_class and frames")


async def _signed_body(request: Request, secret: bytes) -> bytes:
    length = request.headers.get("content-length")
    if length is not None:
        if not re.fullmatch(r"[0-9]+", length):
            raise HTTPException(400, "Invalid content length")
        digits = length.lstrip("0") or "0"
        if len(digits) > len(str(MAX_REPORT_BYTES)) or int(digits) > MAX_REPORT_BYTES:
            raise HTTPException(413, "Report too large")
    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > MAX_REPORT_BYTES:
            raise HTTPException(413, "Report too large")
        body.extend(chunk)
    stamp = request.headers.get("x-atelier-timestamp", "")
    if not re.fullmatch(r"[0-9]{1,12}", stamp) or abs(int(time.time()) - int(stamp)) > 300:
        raise HTTPException(401, "Invalid timestamp")
    expected = "sha256=" + hmac.digest(secret, stamp.encode() + b"." + body, "sha256").hex()
    supplied = request.headers.get("x-atelier-signature", "")
    if not hmac.compare_digest(expected.encode(), supplied.encode()):
        raise HTTPException(401, "Invalid signature")
    return bytes(body)


def create_webhook_app(settings: AtelierSettings) -> FastAPI:
    """Build the loopback relay target with only its signed POST route."""
    sources = {}
    for source, config in settings.webhook_sources.items():
        project = Path(config.get("project", ""))
        conduit = config.get("conduit", "")
        secret = settings.webhook_secrets.get(source)
        if not (
            project.is_absolute()
            and project.is_dir()
            and CONDUIT_NAME_RE.fullmatch(conduit)
            and secret
            and secret.get_secret_value()
        ):
            raise ValueError(f"Invalid project, conduit or missing secret for webhook {source!r}")
        sources[source] = project.resolve(), conduit, secret.get_secret_value().encode()
    claims = EventClaims(settings.global_atelier_dir, "errors")

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        stopped = asyncio.Event()
        async with asyncio.TaskGroup() as tasks:
            tasks.create_task(claims.dispatch(settings.run_caps.get("errors", 2), stopped))
            try:
                yield
            finally:
                stopped.set()

    app = FastAPI(
        openapi_url=None, docs_url=None, redoc_url=None, lifespan=lifespan, redirect_slashes=False
    )

    @app.post("/hooks/errors/{source_id}", status_code=202)
    async def report_error(source_id: str, request: Request) -> Claim:
        if source_id not in sources:
            raise HTTPException(404, "Unknown webhook source")
        project, conduit, secret = sources[source_id]
        body = await _signed_body(request, secret)
        try:
            report = json.loads(body.decode("utf-8"))
        except (ValueError, RecursionError):
            raise HTTPException(400, "Invalid JSON")
        identity = [source_id, str(project), _fingerprint(report)]
        key = hashlib.sha256(json.dumps(identity, separators=(",", ":")).encode()).hexdigest()
        # Even a signed application error can contain attacker-controlled shell or template text.
        inputs = {"report_b64": base64.b64encode(body).decode("ascii")}
        try:
            return await run_in_threadpool(claims.claim, key, project, conduit, inputs)
        except ClaimQueueFull:
            raise HTTPException(429, "Error queue full", headers={"Retry-After": "5"})

    return app
