# Prepare drafts from Dictaria meetings

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
  "dictariaAliases": ["Alex Rivera"],
  "draftCiDisabledRepositories": ["example/private-repo"],
  "meetingRepositories": {"/absolute/path/to/project": "example/private-repo"},
  "runCaps": {"meeting-tasks": 2}
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
If you set `cleanupPeriodDays`, daily cleanup removes raw transcripts after
that many days. Automatic cleanup is off by default.
Invalid or unsuccessful extractions are retried once per poll for three attempts.
The summary records the meeting ID and reason while other meetings continue.
After the third failure the intake marks that meeting failed. The extractor's validated decisions
are saved before task submission, so a restart can finish a partially queued
meeting with its original task IDs.

## Task workers and draft publication

Use a Linux or macOS runner with the supported Codex OS sandbox, an authenticated
GitHub CLI, and the fixed-recipient WhatsApp sender. The repository path in
`meetingRepositories` must match the checkout's resolved path and its GitHub
origin. Set up notifications in user settings. [WhatsApp sender setup](guides/whatsapp.md)

Workers start as assignments arrive. The default machine-wide limit is two.
Each task gets a separate scratch branch containing the files tracked on local
`main`. Use a checkout whose tracked contents are suitable for the worker.
The worker receives
the selected assignment as untrusted text. Its filesystem and network policy
and empty MCP server list are checked before the turn begins. The source repo
and the launcher's credential directories stay outside its workspace.

Open the run's `summary.md` to find each task's draft document, message draft,
proposed patch and draft PR. Draft files live under the child run's `drafts/`
directory in local run output. At 30 minutes the poll stops starting work,
preserves available draft files, and reports partial and queued tasks.
Later polls consume waiting work. The standalone `atelier meetings-intake`
command reads and queues assignments without starting workers.

The publisher checks GitHub's repository visibility and restricts writes to
the exact `atelier/meetings/<task-id>` branch of the configured private repo.
Before acknowledging this repository in `draftCiDisabledRepositories`, exclude
`atelier/*` branches and their PRs from all CI and hosting integrations, including
preview deployments and `pull_request_target`. Draft commits carry `[skip ci]` for
GitHub Actions push and pull_request workflows. Other integrations can still run
worker-written code unless configured to exclude those branches.
Draft snapshots prepared by older versions without the skip marker remain available for
local review. The publisher refuses to push them.
The publisher rejects file deletions, special files, changes to hidden paths, and a remote
`main` that has moved past the draft's parent. Completed local drafts survive a
publication failure, so a later poll can reuse the same snapshot. To retry a
queued worker, run `atelier run meetings-to-tasks` in its original repository.
The final
notification reports counts and remote draft PR URLs to the configured owner.
Message drafts for meeting participants remain files for review.

The five-minute schedule is opt-in. After verifying intake with your test
account, sandbox and private repository, run the following from the
target repository. The scheduler records its absolute path at registration.

```sh
atelier schedule add ~/.atelier/conduits/meetings-to-tasks/schedule.yaml
```
