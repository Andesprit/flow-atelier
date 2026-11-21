# Running unattended on Cloud Run

A conduit that runs overnight with no one at the keyboard needs three
things a laptop provides for free: a logged-in Claude Code and Codex, a
checkout to work in, and somewhere to keep the run's history. This guide
sets up all three as a Cloud Run job. The image and entry point it uses are
in [`examples/cloud-run/`](examples/cloud-run/).

## How the pieces fit

| Piece | Where it comes from |
|---|---|
| Claude Code login | `CLAUDE_CODE_OAUTH_TOKEN`, a Secret Manager secret exposed as an env var |
| Codex login | a logged-in `auth.json`, a secret mounted as a file and copied to a writable `CODEX_HOME` |
| Repository | cloned at start with `GITHUB_TOKEN` |
| Flow history | `.atelier/flows` copied into a mounted Cloud Storage bucket at the end |
| Agent adapters | installed into the image with `atelier harness install`, so no task waits on npx |

flow-atelier never handles these credentials. It passes its environment to
each agent, and each agent reads its own login, exactly as on a laptop.
Before you run agents on a schedule, check each provider's terms for
unattended use: see
[Agent logins and provider terms](guide.md#agent-logins-and-provider-terms).

## 1. Create the logins

**Claude Code.** On a machine where you can sign in, run:

```bash
claude setup-token
```

It prints a long-lived OAuth token for your Claude subscription. Store it:

```bash
printf %s "$TOKEN" | gcloud secrets create claude-oauth-token --data-file=-
```

**Codex.** Sign in once with `codex login` on any machine, then store the
file it wrote:

```bash
gcloud secrets create codex-auth --data-file="$HOME/.codex/auth.json"
```

Codex refreshes its token and rewrites `auth.json` while it runs. A mounted
secret is read-only, so the entry point copies it to a writable
`CODEX_HOME` first. The refreshed copy dies with the container, so if a
later job fails its Codex check, sign in again and add a new secret version.
To bill an API key instead of a ChatGPT plan, run
`printenv OPENAI_API_KEY | codex login --with-api-key` and store that
`auth.json` the same way.

**GitHub.** A fine-grained token with contents read and write on the
repository the conduit works in:

```bash
printf %s "$GITHUB_TOKEN" | gcloud secrets create github-token --data-file=-
```

## 2. Build the image

```bash
cd docs/examples/cloud-run
gcloud builds submit --tag "$REGION-docker.pkg.dev/$PROJECT/atelier/atelier"
```

The image runs the agents as a non-root user, because Claude Code refuses
to skip permission prompts as root and an unattended run approves every
tool call. Pin a release with `--build-arg ATELIER_SPEC=flow-atelier==X.Y.Z`
for reproducible images.

## 3. Create the job

```bash
gcloud run jobs create goal-loop \
  --image "$REGION-docker.pkg.dev/$PROJECT/atelier/atelier" \
  --region "$REGION" \
  --task-timeout 12h --max-retries 0 \
  --cpu 2 --memory 4Gi \
  --set-env-vars "REPO=owner/project,CONDUIT=goal_loop,ATELIER_ARGS=--input hours=8,CODEX_AUTH_FILE=/secrets/codex/auth.json,STATE_DIR=/state" \
  --set-secrets "CLAUDE_CODE_OAUTH_TOKEN=claude-oauth-token:latest,GITHUB_TOKEN=github-token:latest,/secrets/codex/auth.json=codex-auth:latest" \
  --add-volume name=state,type=cloud-storage,bucket="$BUCKET" \
  --add-volume-mount volume=state,mount-path=/state
```

Give the job's service account `roles/secretmanager.secretAccessor` on the
three secrets and object read and write on the bucket. Keep
`--max-retries 0`: a retried task starts a new run in a fresh container,
while flow-atelier already waits out usage limits and retries tasks inside
the run.

Run it once by hand, then schedule it with Cloud Scheduler if it should
recur:

```bash
gcloud run jobs execute goal-loop --region "$REGION" --wait
```

## What the entry point checks

Before the conduit starts, the entry point runs
`atelier harness check claude-code --prompt` and the same for Codex. Each
sends a one-line prompt and requires an answer, so an expired token, a
used-up plan or a model the adapter cannot reach fails the job in its first
minute instead of at the first agent task. A failed check prints what the
agent said and exits non-zero.

## When an agent runs out of usage

A subscription limit reached mid-run does not fail the job. The task waits
for the reset and tries the same turn again; see
[When an agent runs out of usage](guide.md#when-an-agent-runs-out-of-usage).
Set the job's `--task-timeout` long enough to cover a wait, or the
container is stopped while the run is waiting.

## Reading a run afterwards

Use `atelier run --json` in a job script to capture the exact flow it started.
Progress and recovery hints stream to stderr. Stdout contains one JSON object
with `flow_id` and `status`, including when a task fails. A completed run
exits 0, a failed run exits 1, and a run stopped by `atelier stop` exits 143.
If the command rejects the request before execution starts, stdout is empty
and stderr carries a JSON error.

```bash
rc=0
result=$(atelier run "$CONDUIT" --json) || rc=$?
if [ -n "$result" ]; then
    printf '%s\n' "$result" >&2
    flow_id=$(jq -er '.flow_id' <<<"$result")
    if [ "$rc" -eq 0 ]; then
        atelier outputs "$flow_id" --json
    else
        atelier diagnose "$flow_id"
    fi
fi
exit "$rc"
```

`--resume` returns the resumed flow's ID. `--again` returns a new one.
Save the returned ID with the job logs so you can inspect that run after
copying the flows back.

```bash
gcloud storage cp -r "gs://$BUCKET/flows" .atelier/
atelier status "$flow_id"
atelier timing "$flow_id"
```

## Streaming a run's logs

From a checkout with access to the flow's live store, capture each recorded
step as it arrives, including steps from a task that stops before it returns.

```bash
atelier logs "$flow_id" --jsonl --follow > run.jsonl
```

The stream replays existing records first. Each stdout line is one JSON
object. A `step` carries `task`, `iteration` and a nested `step` with the
event details. An `entry` carries the completed iteration's result, including
its exit code and collected output. Steps appear as separate records only.
Read the top-level `kind` to distinguish them.

The last line has `kind: "end"`, the resolved `flow_id` and a `status`.
The reader exits 0 when the flow is `completed`, `failed`, `stopped` or `crashed`.
Ctrl-C ends the reader with status `interrupted` and exit code 130. The
flow keeps running. An invalid request writes a JSON error to stderr and
leaves stdout empty. During follow, warnings also appear on stderr as they occur.
A stream that encounters a read error can end before its final record.

Omit `--follow` to take a snapshot. Its end record reports the observed
status, even for an empty flow (`running` while active, `unknown` if the
progress file is missing). Use `--task NAME` to select a task or `--show
steps` to select only step records. The default is `--show all`. Selecting
`stdout` or `stderr` includes matching live steps and that channel from
completed entries. `--show output` selects the completed task output.
For a snapshot, `--last N` keeps the last N matching data records plus the
end record. For an array of completed log entries, use `atelier logs
"$flow_id" --json`.
