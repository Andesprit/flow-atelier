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

Copy the flows back and use the usual commands:

```bash
gcloud storage cp -r "gs://$BUCKET/flows" .atelier/
atelier status latest
atelier timing latest
```
