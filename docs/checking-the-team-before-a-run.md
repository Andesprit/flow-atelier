# Checking the whole agent team before a run

A workflow that spreads work across Claude Code, Codex and Gemini has three
ways to be ready and one way to be sorry: the agent you set up last week is
logged out, and you find out four minutes into a run, after the first agent
has already done its work.

`atelier check <name> --probe` asks all of them at once, before anything
starts. This is a 3-minute exercise: compose a two-agent workflow, see the
difference between a static check and a startup check, and put the startup
check in front of the run so a bad setup costs nothing.

You should have done [Orchestrating several agents in one
workflow](multi-agent-workflow.md) first.

## Static check, startup check

`atelier check triage` reads files. It answers *is this workflow well
formed, and is each agent's command installed on this machine* — no process
is started, and it is instant.

`atelier check triage --probe` answers a different question: *will these
agents actually start*. It launches each distinct agent the workflow would
use, completes the ACP handshake, opens a session and stops. No prompt is
sent. Opening the session is the point — that is the step that fails when
you are not logged in, which is exactly the setup Atelier reports and never
performs for you.

What a passing probe proves is startup, and only startup. It is not a
promise about authentication at prompt time, about quota, about your access
to a particular model, or about the quality of what comes back.

## Before you start

Two agents you are already logged into. The guide uses `claude-code` and
`codex`; any two names from `atelier list harnesses` work.

```bash
atelier --version || exit 1
```

The blocks below are Bash — macOS, Linux, WSL or Git Bash. They quote every
path and gate every line, so you can paste them into a script without adding
error handling of your own.

## 1. A disposable workspace

```bash
work_dir="$(mktemp -d)/agent team" || exit 1
mkdir -p "$work_dir" || exit 1
cd "$work_dir" || exit 1
echo "working in: $work_dir"
```

## 2. Compose a two-agent workflow

```bash
atelier compose triage \
  -s "claude-code=Propose one concrete fix. Say what you would change and why." \
  -s "codex=Review the proposal above. Name what breaks, or say it is sound." \
  || exit 1
```

That wrote a conduit.yaml and nothing else. `step_1` drafts, `step_2`
reviews.

## 3. Ask the files, then ask the agents

```bash
atelier check triage || exit 1
atelier check triage --probe --timeout 60 || exit 1
```

The first command is the check you already know. The second one starts both
agents. Its report names every task under the agent answerable for it:

```text
triage [project] — OK
    requires --input: brief
    team (root scope): 2 agent task(s) on 2 configuration(s)
    harness:claude-code (1.2s) — ok — reachable and ACP-compatible
      · triage.step_1
    harness:codex (1.4s) — ok — reachable and ACP-compatible
      · triage.step_2
```

A workflow that calls other workflows is checked root-only unless you add
`--recursive`, and the report says so rather than leave you to assume. Two
tasks on the same agent are one check; the same agent pinned to two
different models is two, because a model can be refused on its own.

`--timeout` bounds each agent separately. An agent that hangs ends its own
check and does not hold up the rest of the team.

`--probe --json` gives a coding agent the same report as one document —
scope, every tool, per-task attribution, stage and timing — and still exits
non-zero when an agent is unusable.

## 4. Put the check in front of the run

Today Codex is rate-limited, so both steps go to Claude Code. The mapping is
the same one `atelier run` takes, so what you check is what you run:

```bash
atelier check triage --probe --agent step_2=claude-code --timeout 60 \
  && atelier run triage --agent step_2=claude-code \
       --input brief="The nightly billing job is late. Constraint: budget=0." \
  || exit 1
```

If the probe fails, `&&` stops there: nothing is prepared, no flow is
created, and the diagnostic names the task and the agent to fix. If it
passes, the run starts with the agents that just answered.

The check does not remember the mapping. `--agent` is applied in memory for
that one command — the conduit.yaml is untouched, and the next command needs
the same `--agent` again. That is why both halves of the block carry it.

## 5. Read the result back

```bash
flow_id=$(atelier wait latest --timeout 5) || exit 1
atelier outputs "$flow_id" --task step_2 || exit 1
```

## What the probe costs

No prompt is ever sent, so no tokens are spent on completions. Two honest
caveats: an agent distributed through `npx` or `uvx` downloads its package
on first launch, and opening a session is a real request to the provider,
which can show up in its logs or counters.
