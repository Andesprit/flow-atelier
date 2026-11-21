"""Read-only intake contract for a single authenticated meeting account."""
from collections.abc import AsyncIterator
from datetime import datetime
from typing import Protocol
from uuid import UUID

from pydantic import AwareDatetime, BaseModel


class MeetingSummary(BaseModel):
    process_id: UUID
    updated_at: AwareDatetime
    status: str


class MeetingReader(Protocol):
    def meetings(self, since: datetime | None) -> AsyncIterator[MeetingSummary]: ...

    async def read(self, meeting_id: UUID) -> dict: ...
