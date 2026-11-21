"""Atelier runtime settings and file-source trust boundaries."""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

from pydantic import Field, PositiveInt, SecretStr, field_validator
from pydantic.alias_generators import to_snake
from pydantic.fields import FieldInfo
from pydantic_settings import (
    BaseSettings,
    DotEnvSettingsSource,
    EnvSettingsSource,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
)

from flow_atelier.core.step_instructions import StepInstructions
from flow_atelier.schemas.conduit import HARNESS_NAME_RE, AgentPermissions

# Settings that decide what Atelier executes, where it looks for shared
# conduits, or which credentials and servers it uses. A `.env` file in the
# working folder belongs to whatever repository is checked out there, so a
# cloned repository could otherwise run its own command as the user before
# any agent reads a file. These come from the real environment only.
ENVIRONMENT_ONLY = frozenset({
    "claude_launch_cmd",
    "codex_launch_cmd",
    "opencode_launch_cmd",
    "copilot_launch_cmd",
    "cursor_launch_cmd",
    "harnesses",
    "global_atelier_dir",
    "api_token",
    "serve_url",
})

USER_FILE_ONLY = frozenset({"webhook_sources", "webhook_secrets", "run_caps"})

# New fields are user-only until explicitly allowed in project files.
PROJECT_KEYS = frozenset({
    "atelier_dir", "default_timeout", "default_max_concurrency", "done_marker",
    "loop_history_limit", "loop_history_entry_chars", "usage_limit_max_wait", "agent_permissions",
})
_REPORTED: set[tuple[str, str]] = set()


def _read_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except ValueError as exc:
        raise ValueError(f"{path}: invalid JSON ({exc})") from exc
    if not isinstance(data, dict):
        raise ValueError(f"{path}: expected a JSON object")
    return data


class _FileSettings(PydanticBaseSettingsSource):
    """Load files after the trusted constructor and environment select the roots."""

    def __init__(self, dotenv: DotEnvSettingsSource) -> None:
        super().__init__(dotenv.settings_cls)
        self.dotenv = dotenv

    def get_field_value(self, field: FieldInfo, field_name: str) -> tuple[Any, str, bool]:
        return None, field_name, False  # Values are loaded together in __call__.

    def __call__(self) -> dict[str, Any]:
        trusted = self.current_state
        user_dir = Path(trusted.get("global_atelier_dir", Path.home() / ".atelier"))
        user_path = user_dir / "settings.json"
        values: dict[str, Any] = {}

        def merge(data: dict[str, Any], source: str, *, project: bool, prefix: str = "") -> None:
            for raw, value in data.items():
                if prefix and (value is None or not raw.lower().startswith(prefix.lower())):
                    continue
                key = to_snake(raw[len(prefix):])
                allowed = key in (PROJECT_KEYS if project else self.settings_cls.model_fields)
                if project and key == "agent_permissions":
                    # Neither file may raise a tighter permission from another source.
                    read_only = "read-only" in (values.get(key), trusted.get(key))
                    allowed = value == "read-only" or (value == "edits" and not read_only)
                if allowed and key not in ENVIRONMENT_ONLY:
                    values[key] = value
                else:
                    label = ascii(raw.upper() if prefix else raw)[1:-1]
                    report = (str(Path(source).absolute()), label)
                    if report not in _REPORTED:
                        _REPORTED.add(report)
                        print(f"[flow-atelier] ignored {label} from {source}", file=sys.stderr)

        merge(_read_json(user_path), str(user_path), project=False)
        # Filter the original strings before pydantic decodes complex fields. Even
        # a malformed, forbidden launch command must leave the trusted command usable.
        env_file = self.dotenv.env_file
        source = "./.env" if env_file == ".env" else str(env_file)
        merge(dict(self.dotenv.env_vars), source, project=True, prefix=self.dotenv.env_prefix)
        project_dir = Path(trusted.get("atelier_dir", Path.cwd() / ".atelier"))
        project_path = project_dir / "settings.json"
        if project_path != user_path:
            merge(_read_json(project_path), str(project_path), project=True)
        return values


class AtelierSettings(BaseSettings):
    """Configuration shared by the CLI, server and scheduler."""

    model_config = SettingsConfigDict(
        env_prefix="ATELIER_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        hide_input_in_errors=True,
    )

    atelier_dir: Path = Field(
        default_factory=lambda: Path.cwd() / ".atelier",
        description="Base directory holding conduits/ and flows/",
    )
    global_atelier_dir: Path = Field(
        default_factory=lambda: Path.home() / ".atelier",
        description="Global directory holding shared conduits/ (no flows).",
    )
    default_timeout: PositiveInt = 3600
    default_max_concurrency: PositiveInt = 3
    cleanup_period_days: int = Field(default=30, ge=0)
    dictaria_account: str | None = None
    webhook_sources: dict[str, dict[str, str]] = Field(
        default_factory=dict, description="Per-source project and conduit for signed errors.",
    )
    webhook_secrets: dict[str, SecretStr] = Field(
        default_factory=dict, description="Per-source HMAC keys from user settings.",
    )
    run_caps: dict[str, PositiveInt] = Field(
        default_factory=dict, description="Machine-wide concurrent event runs by category.",
    )
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
    usage_limit_max_wait: int = Field(
        default=21600,
        description=(
            "Seconds one task may wait for its agent's usage limit to reset "
            "(env ATELIER_USAGE_LIMIT_MAX_WAIT). A turn refused for usage is "
            "retried after the reset the agent states, or every 30 minutes "
            "when it states none, without spending the task's retries. "
            "0 fails the task at once."
        ),
    )
    agent_permissions: AgentPermissions | None = Field(
        default=None,
        description=(
            "What agent tasks may do without asking (env "
            "ATELIER_AGENT_PERMISSIONS): 'full', 'edits' (read and change "
            "files, no commands) or 'read-only'. A task's own 'permissions' "
            "wins. Unset = full, with a one-line notice on each run."
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
    step_instructions: StepInstructions = Field(default_factory=StepInstructions)
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
            "commands print <serve_url>/runs/<flow_id> when this server can load "
            "the run; set this when you serve on another host or port."
        ),
    )
    notification_recipient: str | None = Field(
        default=None, description="Fixed WhatsApp recipient in E.164 format."
    )
    notification_provider: str | None = Field(
        default=None, description="Set to twilio to enable the local WhatsApp sender."
    )

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: EnvSettingsSource,
        dotenv_settings: DotEnvSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        """Read the working folder's `.env` through :class:`_ProjectDotEnv`.

        :param settings_cls: this class.
        :param init_settings: values passed to the constructor.
        :param env_settings: the process environment.
        :param dotenv_settings: the working folder's `.env`.
        :param file_secret_settings: secrets files.
        :returns: the sources, highest priority first.
        """
        # Drop user-file-only values before the environment source decodes its JSON.
        for key in USER_FILE_ONLY:
            name = env_settings.env_prefix + key
            env_settings.env_vars.pop(name if env_settings.case_sensitive else name.lower(), None)

        def other_file_secrets() -> dict[str, Any]:
            return {k: v for k, v in file_secret_settings().items() if k not in USER_FILE_ONLY}

        return (
            init_settings,
            env_settings,
            _FileSettings(dotenv_settings),
            other_file_secrets,
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
