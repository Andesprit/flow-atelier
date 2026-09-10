"""Request/response DTOs for the FastAPI HTTP layer (snake_case).

Mirrors the SPEC §5–§6 frontend contract. Each DTO is a thin Pydantic v2
shape; complex validation lives on the underlying domain models
(:class:`Conduit`, :class:`TaskDefinition`, :class:`LogEntry`).
"""
from __future__ import annotations

import re
from datetime import datetime
from typing import Any, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from flow_atelier.schemas.conduit import Conduit, InputSpec, TaskDefinition
from flow_atelier.schemas.interaction import InteractionPolicy
from flow_atelier.schemas.log import LogEntry

_HHMM_RE = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")


class CreateConduitInput(Conduit):
    """Body for ``POST /conduits``: a full conduit definition."""


class UpdateConduitInput(BaseModel):
    """Body for ``PATCH /conduits/:name``: partial update over a conduit."""

    model_config = ConfigDict(extra="forbid")

    name: str | None = None
    description: str | None = None
    timeout: int | None = None
    max_concurrency: int | None = None
    # Accepts both shapes `Conduit` accepts: the plain-string shorthand and the
    # full {description, default} object. Reads return the object form, so a
    # string-only annotation made every edit-and-save round trip 422 for any
    # conduit that declares inputs. `update_conduit` re-validates through
    # `Conduit`, which normalizes either shape.
    inputs: dict[str, InputSpec | str] | None = None
    tasks: list[TaskDefinition] | None = None
    interaction: InteractionPolicy | None = None


class ConduitDTO(Conduit):
    """Response shape for conduit reads — the on-disk model plus ``run_path``."""

    # Response-only: never read from or written to conduit.yaml. A run needs a
    # working directory, and the server is the only side that knows where it
    # was started, so it hands the UI a default instead of leaving an empty box
    # the user must hand-type an absolute path into.
    run_path: str = ""

    @classmethod
    def from_conduit(cls, conduit: Conduit, run_path: str) -> ConduitDTO:
        """Build a DTO from a stored conduit plus the server's default run path.

        :param conduit: the conduit as read from disk.
        :param run_path: working directory a run should default to.
        :returns: the populated :class:`ConduitDTO`.
        """
        return cls.model_validate({**conduit.model_dump(), "run_path": run_path})


class OpenPathInput(BaseModel):
    """Body for ``POST /conduits/open-path``: open a flow's run path."""

    model_config = ConfigDict(extra="forbid")

    # Accepted but unused server-side; the bundled frontend still transmits it,
    # so removing it under extra="forbid" would 422 the live open-path button.
    conduit_name: str
    run_path: str


class OpenPathOutput(BaseModel):
    """Response shape for ``POST /conduits/open-path``."""

    opened: bool


class FlowLogsOutput(BaseModel):
    """Response shape for ``GET /flows/:flow_id/logs``.

    ``logs`` spans the flow and all of its descendants (each entry tagged with
    ``extra["flow_id"]``); ``children`` lists only the flow's direct children.
    """

    run_path: str | None = None
    logs: list[LogEntry] = Field(default_factory=list)
    children: list[str] = Field(default_factory=list)


class RunTaskInput(BaseModel):
    """Body for ``POST /tasks/run``: an ad-hoc one-task conduit."""

    model_config = ConfigDict(extra="forbid")

    name: str
    description: str = ""
    task: str
    tool: str
    run_path: str


class RunTaskOutput(BaseModel):
    """Response shape for ``POST /tasks/run``.

    ``success`` is not inferable from the rest: a run that died before its
    first task logged anything returns an empty ``logs`` and a blank
    ``flow_id``, which is byte-identical to a task that produced no output.
    """

    flow_id: str
    logs: list[LogEntry] = Field(default_factory=list)
    success: bool = True
    error: str = ""


class ScheduleConfig(BaseModel):
    """Inline schedule configuration — recurring (days×times) or one-shot."""

    model_config = ConfigDict(extra="forbid")

    mode: Literal["recurring", "once", "interval"]
    name: str = ""
    days: list[int] | None = None
    times: list[str] | None = None
    run_at: datetime | None = None
    every_minutes: int | None = None
    timezone: str | None = None

    @field_validator("timezone")
    @classmethod
    def _validate_timezone(cls, v: str | None) -> str | None:
        """Reject a timezone string ``ZoneInfo`` cannot construct.

        :param v: IANA timezone name to validate, or ``None``.
        :returns: the validated name unchanged, or ``None`` if not provided.
        """
        if v is None:
            return v
        try:
            ZoneInfo(v)
        except (ZoneInfoNotFoundError, ValueError) as e:
            raise ValueError(f"invalid timezone: {v!r}") from e
        return v

    @field_validator("days")
    @classmethod
    def _validate_days(cls, v: list[int] | None) -> list[int] | None:
        """Ensure each entry in ``days`` is an ISO 8601 day-of-week (1..7).

        :param v: list of day numbers to validate, or ``None``.
        :returns: the validated list unchanged, or ``None`` if not provided.
        """
        if v is None:
            return v
        for d in v:
            if d < 1 or d > 7:
                raise ValueError(
                    f"day must be ISO 8601 day-of-week (1..7), got {d!r}"
                )
        return v

    @field_validator("times")
    @classmethod
    def _validate_times(cls, v: list[str] | None) -> list[str] | None:
        """Ensure each entry in ``times`` matches the 24h ``HH:mm`` format.

        :param v: list of time strings to validate, or ``None``.
        :returns: the validated list unchanged, or ``None`` if not provided.
        """
        if v is None:
            return v
        for t in v:
            if not _HHMM_RE.match(t):
                raise ValueError(
                    f"time must be 'HH:mm' 24h string, got {t!r}"
                )
        return v

    @field_validator("every_minutes")
    @classmethod
    def _validate_every_minutes(cls, v: int | None) -> int | None:
        """Ensure ``every_minutes`` is a positive count of minutes.

        :param v: interval in minutes to validate, or ``None``.
        :returns: the validated value unchanged, or ``None`` if not provided.
        """
        if v is not None and v < 1:
            raise ValueError(f"every_minutes must be >= 1, got {v!r}")
        return v

    @field_validator("run_at", mode="before")
    @classmethod
    def _parse_iso(cls, v: Any) -> Any:
        """Parse an ISO 8601 string into a ``datetime``, accepting a trailing ``Z``.

        :param v: raw value; parsed when it is a string, otherwise returned as-is.
        :returns: a ``datetime`` when ``v`` is a valid ISO string, otherwise ``v`` unchanged.
        """
        if isinstance(v, str):
            try:
                return datetime.fromisoformat(v.replace("Z", "+00:00"))
            except ValueError as e:
                raise ValueError(f"invalid ISO 8601 datetime: {v!r}") from e
        return v

    @model_validator(mode="after")
    def _check_mode_fields(self) -> ScheduleConfig:
        """Ensure required fields are present for the selected ``mode``.

        :returns: the validated ``ScheduleConfig`` instance.
        """
        if self.mode == "recurring":
            if not self.days:
                raise ValueError("recurring schedule requires at least one day")
            if not self.times:
                raise ValueError("recurring schedule requires at least one time")
        elif self.mode == "interval":
            if self.every_minutes is None:
                raise ValueError("interval schedule requires every_minutes")
        else:  # once
            if self.run_at is None:
                raise ValueError("once schedule requires run_at")
        return self


class CreateScheduleInput(BaseModel):
    """Body for ``POST /schedules``."""

    model_config = ConfigDict(extra="forbid")

    conduit_name: str
    inputs: dict[str, Any] = Field(default_factory=dict)
    run_path: str
    schedule: ScheduleConfig

    @model_validator(mode="after")
    def _reject_past_run_at(self) -> CreateScheduleInput:
        """Reject a one-shot whose ``run_at`` is already in the past.

        A past ``run_at`` yields a ``DateTrigger`` that never fires, so the
        schedule installs cleanly but becomes a zombie indistinguishable from
        one that already ran. The check lives here, not on
        :class:`ScheduleConfig`, so persisted :class:`ScheduledJob` records
        (whose ``run_at`` is naturally in the past once fired) still load.

        :returns: the validated input instance.
        """
        run_at = self.schedule.run_at
        if self.schedule.mode == "once" and run_at is not None:
            now = (
                datetime.now(run_at.tzinfo)
                if run_at.tzinfo is not None
                else datetime.now()
            )
            if run_at <= now:
                raise ValueError("once schedule run_at must be in the future")
        return self


class ScheduleRunRecord(BaseModel):
    """One durable record of a scheduler fire: when, outcome, and the flow id.

    ``flow_id`` is nullable because a fire can fail before ``on_flow_started``
    ever runs (e.g. a conduit read error), so there is no run to point at.
    """

    model_config = ConfigDict(extra="forbid")

    ran_at_iso: str
    status: Literal["succeeded", "failed"]
    flow_id: str | None = None


class ScheduledJob(BaseModel):
    """Server-side representation of a persisted schedule (per SPEC §7)."""

    model_config = ConfigDict(extra="forbid")

    id: str
    conduit_name: str
    inputs: dict[str, Any] = Field(default_factory=dict)
    run_path: str
    schedule: ScheduleConfig
    created_at: int
    runs_completed: int = 0


class PriorFlow(BaseModel):
    """List entry for ``GET /flows``."""

    model_config = ConfigDict(extra="forbid")

    flow_id: str
    conduit_name: str
    started_at: str | None = None
    finished_at: str | None = None
    status: str
