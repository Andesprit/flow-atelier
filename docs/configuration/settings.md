# Settings and environment variables

## Precedence

Atelier reads settings in this order, from highest to lowest priority.

1. Explicit CLI options for commands that take them, and values supplied by the
   application when it constructs its settings.
2. Environment variables exported to the Atelier process.
3. The project settings file, `.atelier/settings.json`.
4. The working directory's `.env`.
5. The user settings file, `~/.atelier/settings.json`.
6. Secret files, if a caller configured a secrets directory, then built-in defaults.

Both JSON files are optional and accept camelCase and snake_case keys. For
example, `{"defaultTimeout": 120, "defaultMaxConcurrency": 2}` sets defaults
for conduits that omit those fields. Shared conduits use the current project's
defaults. Use JSON arrays or objects for list and map values in the environment
and in `.env`.

Export `ATELIER_ATELIER_DIR` or `ATELIER_GLOBAL_ATELIER_DIR` to change the
project or user root. That also changes which `settings.json` Atelier reads.
Atelier reports forbidden settings from project or user files on stderr and
omits their values.

## Project settings

These are the keys allowed in project `.atelier/settings.json` and `.env`.
They can also be set in user settings or the process environment. Use the
`ATELIER_*` spelling in `.env` and the process environment.

| JSON key | Environment variable | Default | Purpose |
| --- | --- | --- | --- |
| `defaultTimeout` | `ATELIER_DEFAULT_TIMEOUT` | `3600` | Seconds per task when the conduit omits its timeout. |
| `defaultMaxConcurrency` | `ATELIER_DEFAULT_MAX_CONCURRENCY` | `3` | Maximum number of tasks that can run at once when the conduit omits its limit. |
| `doneMarker` | `ATELIER_DONE_MARKER` | `[ATELIER_DONE]` | Completion marker for interactive agent turns. |
| `loopHistoryLimit` | `ATELIER_LOOP_HISTORY_LIMIT` | `10` | Maximum number of prior iterations in `{{loop.history}}`. Zero or a negative value means unlimited. |
| `loopHistoryEntryChars` | `ATELIER_LOOP_HISTORY_ENTRY_CHARS` | `40000` | Maximum characters per iteration in `{{loop.history}}`. Zero or a negative value means unlimited. |
| `usageLimitMaxWait` | `ATELIER_USAGE_LIMIT_MAX_WAIT` | `21600` | Maximum seconds to wait for a task's agent usage limit to reset. Use `0` to fail immediately. |
| `agentPermissions` | `ATELIER_AGENT_PERMISSIONS` | Unset, which uses `full` and prints a notice | Default permission level for agent tasks. A task can specify its own level. |

A project file can set agent permissions to `edits` or `read-only`. It cannot
raise a `read-only` setting from another source. See [agent permissions](security.md).

## Process environment

Export these variables in the shell or set them in the service environment.

| Environment variable | Default | Purpose |
| --- | --- | --- |
| `ATELIER_ATELIER_DIR` | `./.atelier` | Project store. |
| `ATELIER_GLOBAL_ATELIER_DIR` | `~/.atelier` | User store for shared conduits, schedules, and run history. |
| `ATELIER_API_TOKEN` | Empty | Bearer token for the HTTP and WebSocket API. Empty disables token authentication on loopback. |
| `ATELIER_SERVE_URL` | `http://127.0.0.1:8000` | Server address the CLI uses when it prints a run-page link. |
| `ATELIER_HARNESSES` | `{}` | Map of custom ACP harness names to command argument arrays. |
| `ATELIER_CLAUDE_LAUNCH_CMD` | `[]` | Override the Claude Code launch command. An empty array uses its adapter default. |
| `ATELIER_CODEX_LAUNCH_CMD` | `[]` | Override the Codex launch command. An empty array uses its adapter default. |
| `ATELIER_OPENCODE_LAUNCH_CMD` | `[]` | Override the opencode launch command. An empty array uses its adapter default. |
| `ATELIER_COPILOT_LAUNCH_CMD` | `[]` | Override the Copilot launch command. An empty array uses its adapter default. |
| `ATELIER_CURSOR_LAUNCH_CMD` | `[]` | Override the Cursor launch command. An empty array uses its adapter default. |

See [AI harnesses](agent-logins.md#optional-ai-harnesses) for launch commands,
models, and login setup.

## User settings

Set these keys in `~/.atelier/settings.json`. Where an environment variable is
listed, it overrides the user file. A dash indicates that the process
environment ignores that setting.

| JSON key | Environment variable | Default | Purpose |
| --- | --- | --- | --- |
| `cleanupPeriodDays` | `ATELIER_CLEANUP_PERIOD_DAYS` | Unset | Age in days for [automatic cleanup](#automatic-cleanup). |
| `vapidSubject` | `ATELIER_VAPID_SUBJECT` | Unset | Web Push contact in `mailto:` or HTTPS form. |
| `stepInstructions` | `ATELIER_STEP_INSTRUCTIONS` | Empty `project_rules` map and `skills` list | [Shared rules and skills](../guides/packages.md#shared-agent-instructions) for agent tasks. |
| `dictariaAccount` | — | Unset | Dictaria account whose token is in the OS keychain. |
| `dictariaAliases` | — | `[]` | Additional names the meeting intake recognizes as yours. |
| `dictariaUrl` | — | Unset | Override the Dictaria service URL. |
| `meetingRepositories` | — | `{}` | Map resolved project paths to GitHub repositories for meeting tasks. |
| `draftCiDisabledRepositories` | — | `[]` | Repositories configured for draft publication with CI disabled. |
| `webhookSources` | — | `{}` | Map error source IDs to projects and conduits. |
| `webhookSecrets` | — | `{}` | Map error source IDs to HMAC signing secrets. |
| `runCaps` | — | `{}` | Map event run categories to positive machine-wide concurrency limits. |
| `errorPrProjects` | — | `{}` | Trusted repository destinations and test commands for error recipes. |
| `deploymentTargets` | — | `{}` | Approved deployment targets keyed by resolved project path and target alias. |
| `notificationRecipient` | — | Unset | Fixed WhatsApp recipient in E.164 format. |
| `notificationProvider` | — | Unset | Set to `twilio` to enable the local WhatsApp sender. |

## Automatic cleanup

Automatic cleanup is off by default. To enable it, set `cleanupPeriodDays` in
`~/.atelier/settings.json` (for example, `{"cleanupPeriodDays": 30}`).
`ATELIER_CLEANUP_PERIOD_DAYS` in the process environment overrides that setting.
The scheduler sweeps global run history at startup and every 24 hours. A server
running without its own scheduler performs the sweep on the same interval.
To disable cleanup, leave the environment variable unset and omit
`cleanupPeriodDays` or set it to `null` in user settings.

A run tree becomes eligible when every member has finished or crashed and
all are at least that many days old. Finished runs use their finish time.
Crashed runs use their last heartbeat, falling back to the start time. A
value of `0` expires all eligible run history at the next sweep.
Negative values are rejected. Cleanup removes journal records, tool steps and
saved outputs. Active writers, pending questions, unmigrated project records
and project worktrees are kept.
