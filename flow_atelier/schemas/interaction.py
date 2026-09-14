"""Conduit-wide routing of harness questions and permission requests."""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class SupervisorConfig(BaseModel):
    """Harness and instructions used to answer on the user's behalf."""

    model_config = ConfigDict(extra="forbid")
    tool: str = Field(pattern=r"^harness:[a-z0-9][a-z0-9-]*$")
    instructions: str = ""
    max_replies: int = Field(default=8, ge=1)
    timeout: int = Field(default=120, ge=1)
    max_context_chars: int = Field(default=200_000, ge=1)


class InteractionPolicy(BaseModel):
    """Independent reply and approval policies; defaults preserve existing runs."""

    model_config = ConfigDict(extra="forbid")
    questions: Literal["human", "supervisor", "hybrid"] = "human"
    permissions: Literal["approve_all", "human", "supervisor", "hybrid"] = "approve_all"
    supervisor: SupervisorConfig | None = None

    @model_validator(mode="after")
    def require_supervisor(self) -> InteractionPolicy:
        """Reject supervised modes without a harness.

        :returns: the validated policy.
        """
        if (
            {self.questions, self.permissions} & {"supervisor", "hybrid"}
            and self.supervisor is None
        ):
            raise ValueError("supervisor configuration is required for supervisor/hybrid modes")
        return self


class SupervisorDecision(BaseModel):
    """Strict response contract, additionally checked against the current request."""

    model_config = ConfigDict(extra="forbid")
    action: Literal["answer", "escalate"]
    reply: str = ""
    question: str = ""
    option_id: str | None = None
