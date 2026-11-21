"""Authenticated dashboard conversations. Request IDs make turn retries idempotent."""

import asyncio
import json
import re
import time
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from flow_atelier.services.api.chat import ChatSession, available
from flow_atelier.services.api.chat_cli import cli

router = APIRouter(prefix="/chat", tags=["chat"])


class NewChat(BaseModel):
    harness: str


class Turn(BaseModel):
    request_id: UUID
    text: str = Field(min_length=1, max_length=64_000)
    source: str | None = Field(None, pattern=r"^[0-9a-f]{64}$")


def session(request: Request, chat_id: str) -> ChatSession:
    chat = request.app.state.chats.get(chat_id)
    if chat is None:
        raise HTTPException(410, "Session unavailable. Start a fresh chat.")
    chat.last_seen = time.monotonic()
    return chat


@router.get("/harnesses")
async def harnesses(request: Request):
    return sorted(available(request.app.state.atelier))


@router.get("/history")
async def history(request: Request, source: str | None = None):
    if not request.app.state.local_history:
        raise HTTPException(403, "History is available only on a loopback server")
    settings = request.app.state.atelier.settings
    if source is not None and not re.fullmatch(r"[0-9a-f]{64}", source):
        raise HTTPException(422, "Invalid session source ID")
    args = [source] if source else []
    try:
        return json.loads(
            await cli(
                settings.atelier_dir.resolve().parent,
                settings.global_atelier_dir,
                "history",
                *args,
                "--json",
            )
        )
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@router.post("")
async def create(body: NewChat, request: Request):
    atelier = request.app.state.atelier
    executor = available(atelier).get(body.harness)
    if executor is None:
        raise HTTPException(422, "Harness unavailable")
    chats = request.app.state.chats
    for key, old in list(chats.items()):
        if not old.task and not old.jobs and time.monotonic() - old.last_seen > 3600:
            chats.pop(key, None)
            await old.close()
    if len(chats) >= 32:
        raise HTTPException(429, "Close an existing chat before starting another")
    chat = ChatSession(
        body.harness,
        executor,
        atelier.settings.atelier_dir.resolve().parent,
        atelier.settings.global_atelier_dir,
    )
    request.app.state.chats[chat.id] = chat
    return chat.view()


@router.get("/{chat_id}")
async def snapshot(after: int = -1, chat: ChatSession = Depends(session)):
    if after == chat.revision:
        chat.changed.clear()
        try:
            await asyncio.wait_for(chat.changed.wait(), 15)
        except TimeoutError:
            pass
    return chat.view()


@router.post("/{chat_id}/turn")
async def turn(body: Turn, request: Request, chat: ChatSession = Depends(session)):
    if body.source and not request.app.state.local_history:
        raise HTTPException(403, "Local history is unavailable")
    try:
        chat.submit(str(body.request_id), body.text, body.source)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    return chat.view()


@router.post("/{chat_id}/cancel")
async def cancel(chat: ChatSession = Depends(session)):
    await chat.cancel()
    return chat.view()


@router.post("/{chat_id}/confirm/{token}")
async def confirm(token: str, chat: ChatSession = Depends(session)):
    try:
        chat.confirm(token)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    return chat.view()


@router.delete("/{chat_id}")
async def close(request: Request, chat: ChatSession = Depends(session)):
    if chat.jobs:
        raise HTTPException(409, "A confirmed operation is still running")
    request.app.state.chats.pop(chat.id, None)
    await chat.close()
    return {}
