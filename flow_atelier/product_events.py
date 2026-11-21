"""Structured Cloud Run application events; safe to vendor into a Python application."""

import hashlib
import hmac
import json
import os
import re
import sys
import time
from urllib.request import HTTPRedirectHandler, Request, build_opener


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args):
        return None


def _post(url: str, body: bytes, headers: dict[str, str]) -> None:
    request = Request(url, data=body, headers=headers, method="POST")
    with build_opener(_NoRedirect()).open(request, timeout=5) as response:
        if response.status != 202:
            raise RuntimeError("Error receiver did not accept the report")


def emit(severity: str, event: str, message: str) -> bool:
    """Write one JSON log line and send signed error events to the configured receiver."""
    if severity not in {"INFO", "WARNING", "ERROR"} or not re.fullmatch(r"[a-z0-9_-]{1,80}", event):
        raise ValueError("expected INFO, WARNING or ERROR and a stable application event key")
    target = os.environ["ATELIER_PRODUCT_TARGET"]
    record = {
        "severity": severity,
        "target": target,
        "fingerprint": f"{target}/{event}",
        "message": message[:2000],
    }
    body = json.dumps(record, ensure_ascii=True, separators=(",", ":")).encode()
    print(body.decode(), flush=True)
    if severity != "ERROR":
        return True
    try:
        stamp = str(int(time.time()))
        signature = hmac.digest(
            os.environ["ATELIER_ERROR_KEY"].encode(), stamp.encode() + b"." + body, hashlib.sha256
        ).hex()
        _post(
            os.environ["ATELIER_ERROR_URL"],
            body,
            {
                "Content-Type": "application/json",
                "X-Atelier-Timestamp": stamp,
                "X-Atelier-Signature": "sha256=" + signature,
            },
        )
    except (KeyError, OSError, ValueError, RuntimeError):
        print(
            json.dumps(
                {
                    "severity": "WARNING",
                    "target": target,
                    "message": "Could not deliver the application error report",
                    "fingerprint": record["fingerprint"],
                }
            ),
            file=sys.stderr,
            flush=True,
        )
        return False
    return True
