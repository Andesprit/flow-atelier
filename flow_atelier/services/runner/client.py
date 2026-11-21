"""Versioned HTTPS client. Redirects and ambient proxy credentials are never used."""

import http.client
import json
import re
import ssl
from pathlib import Path
from urllib.parse import urlsplit

from pydantic import ValidationError

from flow_atelier.schemas.runner import ENDPOINTS, Wire
from flow_atelier.services.runner.privacy import outbound


class RunnerResponseError(ValueError):
    """A local HTTP diagnostic safe to display without the response body."""


class Client:
    def __init__(self, origin: str, state_dir: Path):
        url = urlsplit(origin)
        if (url.scheme != "https" or not url.hostname or url.username is not None
                or url.path not in ("", "/") or url.query or url.fragment):
            raise ValueError("runner requires an HTTPS origin")
        self.host, self.port = url.hostname, url.port
        self.origin = origin.rstrip("/")
        self.state_dir = state_dir

    def post(self, route: str, message: Wire, token: str) -> Wire:
        request, response = ENDPOINTS[route]
        if type(message) is not request or not re.fullmatch(r"[A-Za-z0-9._~-]{1,512}", token):
            raise ValueError("invalid runner request or credential")
        message = outbound(request.model_validate(message), self.state_dir, token)
        context = ssl.create_default_context()
        conn = http.client.HTTPSConnection(self.host, self.port, timeout=15, context=context)
        try:
            conn.request("POST", f"/runner/v1/{route}", message.model_dump_json().encode(),
                         {"Content-Type": "application/json", "Authorization": f"Bearer {token}"})
            reply = conn.getresponse()
            if reply.status in (401, 403):
                raise PermissionError("runner credential refused")
            if reply.status >= 500 or reply.status == 429:
                raise ConnectionError("control plane temporarily unavailable")
            if reply.status != 200:
                raise RunnerResponseError(f"runner request rejected (HTTP {reply.status})")
            data = reply.read(65537)
            if len(data) > 65536:
                raise RunnerResponseError("oversized runner response")
            try:
                return response.model_validate(json.loads(data))
            except (ValueError, ValidationError):
                raise RunnerResponseError("invalid runner response") from None
        finally:
            conn.close()
