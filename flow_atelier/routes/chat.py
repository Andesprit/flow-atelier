"""Authenticated dashboard conversations. Request IDs make turn retries idempotent."""

import asyncio
import time
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from flow_atelier.services.api.chat import ChatSession, available

router = APIRouter(prefix="/chat", tags=["chat"])


class NewChat(BaseModel):
    harness: str


class Turn(BaseModel):
    request_id: UUID
    text: str = Field(min_length=1, max_length=64_000)


def session(request: Request, chat_id: str) -> ChatSession:
    chat = request.app.state.chats.get(chat_id)
    if chat is None:
        raise HTTPException(410, "Session unavailable. Start a fresh chat.")
    chat.last_seen = time.monotonic()
    return chat


@router.get("/harnesses")
async def harnesses(request: Request):
    return sorted(available(request.app.state.atelier))


@router.post("")
async def create(body: NewChat, request: Request):
    atelier = request.app.state.atelier
    executor = available(atelier).get(body.harness)
    if executor is None:
        raise HTTPException(422, "Harness unavailable")
    chats = request.app.state.chats
    for key, old in list(chats.items()):
        if not old.task and time.monotonic() - old.last_seen > 3600:
            chats.pop(key, None)
            await old.close()
    if len(chats) >= 32:
        raise HTTPException(429, "Close an existing chat before starting another")
    chat = ChatSession(body.harness, executor, atelier.settings.atelier_dir.resolve().parent)
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
async def turn(body: Turn, chat: ChatSession = Depends(session)):
    try:
        chat.submit(str(body.request_id), body.text)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    return chat.view()


@router.post("/{chat_id}/cancel")
async def cancel(chat: ChatSession = Depends(session)):
    await chat.cancel()
    return chat.view()


@router.delete("/{chat_id}")
async def close(request: Request, chat: ChatSession = Depends(session)):
    request.app.state.chats.pop(chat.id, None)
    await chat.close()
    return {}
