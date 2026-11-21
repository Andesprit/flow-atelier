"""Projection from local records to public v1 metadata. Free text stays local."""

import json
import os
import re
import socket
from pathlib import Path

from flow_atelier.schemas.runner import Folder, Poll, Records, Wire

_TOKEN = re.compile(
    r"gh[pousr]_|github_pat_|sk[-_]|xox[baprs]-|AKIA[A-Z0-9]{16}|ASIA[A-Z0-9]{16}|"
    r"AIza[\w-]{20}|eyJ[\w-]{8}|glpat-|npm_|pypi-|Bearer\s|-----BEGIN ",
    re.IGNORECASE,
)


def private_values(root: Path, bearer: str) -> set[str]:
    """Load local redactions at both capture and replay so additions take effect immediately."""
    values = {value for name, value in os.environ.items()
              if len(value) >= 8 or re.search(r"TOKEN|KEY|SECRET|PASS|AUTH|CRED", name, re.I)}
    values.add(bearer)
    for filename in (root.parent / "settings.json", root / "redact.json"):
        try:
            obj = json.loads(filename.read_text(encoding="utf-8"))
        except FileNotFoundError:
            continue
        if filename.parent == root and not (
            isinstance(obj, list) and all(isinstance(item, str) for item in obj)
        ):
            raise ValueError("runner/redact.json must contain a list of strings")
        pending = [obj]
        while pending:
            item = pending.pop()
            if isinstance(item, str):
                values.add(item)
            elif isinstance(item, list):
                pending.extend(item)
            elif isinstance(item, dict):
                pending.extend(item.values())
    return values - {""}


def public_name(value: object, secrets: set[str], *, label: bool = False) -> str | None:
    pattern = r"[A-Za-z0-9 _-]{1,80}" if label else r"[A-Za-z0-9_-]{1,100}"
    if not isinstance(value, str) or not re.fullmatch(pattern, value) or _TOKEN.search(value):
        return None
    host = socket.gethostname().lower().split(".")[0]
    if (host and host in value.lower()) or any(secret in value for secret in secrets):
        return None
    return value


def outbound(message: Wire, root: Path, bearer: str) -> Wire:
    if not isinstance(message, (Poll, Records)):
        return message
    secrets = private_values(root, bearer)
    if isinstance(message, Poll):
        return Poll(claim=message.claim, folders=[
            Folder(id=f.id, label=public_name(f.label, secrets, label=True) or f.id,
                   conduits=[n for n in f.conduits if public_name(n, secrets)])
            for f in message.folders
        ])
    return Records(records=[
        record.model_copy(update={"task": public_name(record.task, secrets)})
        for record in message.records
    ])
