"""Atelier runtime settings (env-driven)."""
from __future__ import annotations

from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from flow_atelier.schemas.conduit import HARNESS_NAME_RE


class AtelierSettings(BaseSettings):
    """Environment-driven configuration."""

    model_config = SettingsConfigDict(
        env_prefix="ATELIER_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    atelier_dir: Path = Field(
        default_factory=lambda: Path.cwd() / ".atelier",
        description="Base directory holding conduits/ and flows/",
    )
    global_atelier_dir: Path = Field(
        default_factory=lambda: Path.home() / ".atelier",
        description="Global directory holding shared conduits/ (no flows).",
    )
    default_timeout: int = 3600
    default_max_concurrency: int = 3
    loop_history_limit: int = Field(
        default=10,
        description=(
            "Max prior iterations rendered by {{loop.history}}. "
            "Values <= 0 mean unlimited."
        ),
    )
    loop_history_entry_chars: int = Field(
        default=40000,
        description=(
            "Max characters per {{loop.history}} entry; longer entries keep "
            "head and tail around a truncation marker. Values <= 0 mean "
            "unlimited."
        ),
    )
    claude_launch_cmd: list[str] = Field(
        default_factory=list,
        description=(
            "Override argv for the harness:claude-code ACP agent. "
            "Empty = use the bundled default (@agentclientprotocol/claude-agent-acp)."
        ),
    )
    codex_launch_cmd: list[str] = Field(
        default_factory=list,
        description=(
            "Override argv for the harness:codex ACP agent. "
            "Empty = use the bundled default (@zed-industries/codex-acp)."
        ),
    )
    opencode_launch_cmd: list[str] = Field(
        default_factory=list,
        description=(
            "Override argv for the harness:opencode ACP agent. "
            "Empty = use the bundled default (opencode acp)."
        ),
    )
    copilot_launch_cmd: list[str] = Field(
        default_factory=list,
        description=(
            "Override argv for the harness:copilot ACP agent. "
            "Empty = use the bundled default (copilot --acp)."
        ),
    )
    cursor_launch_cmd: list[str] = Field(
        default_factory=list,
        description=(
            "Override argv for the harness:cursor ACP agent. "
            "Empty = use the bundled default (@blowmage/cursor-agent-acp)."
        ),
    )
    harnesses: dict[str, list[str]] = Field(
        default_factory=dict,
        description=(
            "Extra ACP agents, as name -> argv. Each is registered as "
            "'harness:<name>' and driven by the same generic ACP executor as "
            "the bundled five, so any ACP-speaking agent works without a code "
            'change. Example: ATELIER_HARNESSES=\'{"gemini": ["npx", "-y", '
            '"gemini-acp"]}\'. A name matching a bundled harness overrides it.'
        ),
    )
    done_marker: str = "[ATELIER_DONE]"
    api_token: str = Field(
        # Empty, never a literal: a shipped default token is a published
        # shared credential, and a truthy default also silently disables the
        # "serving without auth" warning in `atelier serve`.
        default="",
        description=(
            "Optional bearer token for the HTTP/WS API (env ATELIER_API_TOKEN). "
            "When set, every REST request must send 'Authorization: Bearer "
            "<token>' and WS connections must pass '?token=<token>'. "
            "Empty = no auth (local trust)."
        ),
    )
    serve_url: str = Field(
        default="http://127.0.0.1:8000",
        description=(
            "Where `atelier serve` answers (env ATELIER_SERVE_URL). Flow-running "
            "commands print each run's page as <serve_url>/runs/<flow_id>; set "
            "this when you serve on another host or port."
        ),
    )

    @field_validator("harnesses")
    @classmethod
    def _harnesses_usable(cls, v: dict[str, list[str]]) -> dict[str, list[str]]:
        """Reject harness entries no conduit could ever reference or run.

        A name outside the ``harness:<name>`` grammar registers an executor
        key that :class:`TaskDefinition <flow_atelier.schemas.conduit.TaskDefinition>`
        refuses to parse, and an empty argv has nothing to spawn. Both would
        otherwise surface much later as a confusing "no executor registered".

        :param v: the configured name -> argv map.
        :returns: the validated map unchanged.
        """
        for name, argv in v.items():
            if not HARNESS_NAME_RE.match(name):
                raise ValueError(
                    f"invalid harness name {name!r}: only lowercase letters, "
                    "digits and hyphens are allowed"
                )
            if not argv:
                raise ValueError(f"harness {name!r}: argv must not be empty")
        return v
