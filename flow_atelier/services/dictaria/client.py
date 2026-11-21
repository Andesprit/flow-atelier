"""Dictaria's list_meetings and read_meeting over MCP Streamable HTTP."""
import json
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from typing import Literal
from urllib.parse import urlsplit
from uuid import UUID

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.types import TextContent
from pydantic import BaseModel, StrictInt

from flow_atelier.services.dictaria.base import MeetingSummary

PAGE_SIZE = 50
SERVICE = "flow-atelier.dictaria"


def access_token(account: str) -> str:
    """Use the native credential store directly, including on hosts with keyring plugins."""
    if sys.platform == "darwin":
        from keyring.backends.macOS import Keyring
    elif sys.platform == "win32":
        from keyring.backends.Windows import WinVaultKeyring as Keyring
    elif sys.platform.startswith("linux"):
        from keyring.backends.SecretService import Keyring
    else:
        raise ValueError("no supported OS keychain for Dictaria")
    try:
        token = Keyring().get_password(SERVICE, account)
    except Exception:
        raise ValueError("could not read the Dictaria login from the OS keychain") from None
    if not token:
        raise ValueError("Dictaria login missing from the OS keychain")
    return token


class ListPage(BaseModel):
    meetings: list[MeetingSummary]
    next_offset: StrictInt | None


class ReadPage(BaseModel):
    process_id: UUID
    title: str | None = None
    overview: str = ""
    transcript_page: str
    next_offset: StrictInt | None


class Dictaria:
    def __init__(self, session: ClientSession) -> None:
        self.session = session

    async def _call(self, name: Literal["list_meetings", "read_meeting"], args: dict) -> dict:
        result = await self.session.call_tool(name, args)
        if not result.isError:
            if result.structuredContent is not None:
                return result.structuredContent
            if len(result.content) == 1 and isinstance(result.content[0], TextContent):
                return json.loads(result.content[0].text)
        raise ValueError(f"Dictaria could not return {name}; check the login and meeting access")

    async def meetings(self, since: datetime | None) -> AsyncIterator[MeetingSummary]:
        offset = 0
        while True:
            page = ListPage.model_validate(await self._call(
                "list_meetings", {"offset": offset, "limit": PAGE_SIZE},
            ))
            count = len(page.meetings)
            expected = offset + count if count == PAGE_SIZE else None
            if (count > PAGE_SIZE or page.next_offset != expected
                    or (expected is not None and expected > 10_000)):
                raise ValueError("incomplete Dictaria meeting list")
            for row in page.meetings:
                if since is None or row.updated_at >= since:
                    yield row
            # Dictaria sorts by updated_at descending. Revisit ties at the watermark.
            if page.next_offset is None or (since and page.meetings[-1].updated_at < since):
                return
            offset = page.next_offset

    async def read(self, meeting_id: UUID) -> dict:
        parts = []
        offset = 0
        while True:
            page = ReadPage.model_validate(await self._call(
                "read_meeting", {"process_id": str(meeting_id), "transcript_offset": offset},
            ))
            if page.process_id != meeting_id:
                raise ValueError("Dictaria returned a different meeting")
            if offset and not page.transcript_page:
                raise ValueError("missing Dictaria transcript page")
            parts.append(page.transcript_page)
            if page.next_offset is None:
                return {"process_id": str(meeting_id), "title": page.title,
                        "overview": page.overview, "transcript": "".join(parts)}
            if not page.transcript_page or page.next_offset != offset + len(page.transcript_page):
                raise ValueError("incomplete Dictaria transcript")
            offset = page.next_offset


@asynccontextmanager
async def connect(url: str, account: str) -> AsyncIterator[Dictaria]:
    endpoint = urlsplit(url)
    local = endpoint.scheme == "http" and endpoint.hostname in {"127.0.0.1", "::1"}
    if (not endpoint.hostname or endpoint.username is not None or endpoint.query
            or endpoint.fragment or (endpoint.scheme != "https" and not local)):
        raise ValueError("use an HTTPS Dictaria MCP URL, or HTTP on loopback")
    headers = {"Authorization": "Bearer " + access_token(account)}
    try:
        async with (
            httpx.AsyncClient(headers=headers, timeout=30, trust_env=False) as http,
            streamable_http_client(url, http_client=http) as (read, write, _),
            ClientSession(read, write, read_timeout_seconds=timedelta(seconds=30)) as session,
        ):
            await session.initialize()
            yield Dictaria(session)
    except* httpx.HTTPStatusError as errors:
        raise ValueError("Dictaria HTTP request failed; check the login and MCP URL") from errors
