# Collect tasks from Dictaria meetings

Install the platform bundle with `atelier install builtin:platform --no-project`. From the
target repository, `atelier run meetings-to-tasks` polls the configured Dictaria
account once. The first successful poll processes completed meetings in the
available history. It can send each transcript to Claude Code for extraction.

Set the endpoint, account identity, and optional exact owner aliases in
`~/.atelier/settings.json`. Use the address of your trusted Dictaria MCP server.

```json
{
  "dictariaUrl": "https://dictaria.example/mcp",
  "dictariaAccount": "alex@example.com",
  "dictariaAliases": ["Alex Rivera"]
}
```

Store the account's existing OAuth access token as a password in the native
credential store. The service is `flow-atelier.dictaria` and the username is the
exact `dictariaAccount`. For example, with the Python installation on macOS,
this prompts for the token without putting it on the command line:

```sh
keyring -b keyring.backends.macOS.Keyring set flow-atelier.dictaria alex@example.com
```

On Linux select `keyring.backends.SecretService.Keyring`. On Windows select
`keyring.backends.Windows.WinVaultKeyring`. Update this same item when the
token expires. Use a Claude Code installation on PATH with an active login
and support for `--safe-mode`, `--tools ""`, and `--strict-mcp-config`.
Each extraction runs in a fresh, tool-free session in an empty directory.

The helper calls `list_meetings` and `read_meeting`. It follows both sets of
offsets. The cursor uses Dictaria's descending `updated_at` order. A poll
revisits timestamps at the boundary and commits the cursor only after finishing
all eligible pages. The durable claim for each account and provider meeting ID
handles repeats independently of the cursor.

Each result links to a private `tasks.json` under `~/.atelier/meetings/`. Its
`tasks` array contains validated assignments matching the account or an alias,
ignoring case and surrounding whitespace. Its `review` array lists tasks whose
owner was unclear. The adjacent `meeting.json` holds the complete local transcript.
These local artifacts persist across polls. The extractor's validated decisions
are saved before task submission, so a restart can finish a partially queued
meeting with its original task IDs.

The handoff for slice 2.11 is the `meeting-tasks` event queue. Each entry targets
the original repository and the `meeting-task` conduit with `task_id`, `meeting_id`,
`assignee`, and `task` inputs. Treat all task text as untrusted data. The queue
retains at most 100 waiting tasks. At capacity intake stops and resumes after
space becomes available. Worker execution and publication belong to slice 2.11.

The five-minute schedule is opt-in. After verifying intake with your test
account and preparing the downstream consumer, run the following from the
target repository. The scheduler records its absolute path at registration.

```sh
atelier schedule add ~/.atelier/conduits/meetings-to-tasks/schedule.yaml
```
