# Quickstart

```bash
atelier init                                # creates .atelier/conduits/hello/ (and explain/, pair/)
atelier run hello --input name=world        # runs it
atelier status latest                       # shows progress of the newest run
atelier list flows --conduit hello          # lists previous runs
```

`atelier init` writes a one-line `hello` conduit that only runs a
shell command, so this works end-to-end before you install any AI
tool. Look for `hello world` and a flow ID. When it finds Claude Code or
Codex on your `PATH`, it also writes the read-only starters `explain` and
`pair`. In `explain`, Claude Code (or Codex if installed alone) reads the
project and writes a brief for a newcomer. In `pair`, that agent proposes a
change and revises it after review. With both installed, Codex supervises
and reviews. With one, the same harness fills both roles. [Interaction policies](../interaction.md)

Every task in both starters declares `permissions: read-only`. By default,
if a harness cannot enforce that level, the task stops before sending the
prompt and reports `permissions read-only cannot be enforced` with the reason.
Run `atelier init` again after installing an agent to add any missing starters.
When upgrading from 0.14.1, `atelier init` adds read-only permissions to
`explain` and `pair` if they match the stock 0.14.1 files exactly. It
preserves the agents, layout and line endings (LF or CRLF) and prints
`upgraded .atelier/conduits/<name>/conduit.yaml to read-only`. If an edited
starter has writable agent tasks, Atelier keeps the file and prints
`warning .atelier/conduits/<name>/conduit.yaml has writable agent tasks.`
It follows with `Add or set 'permissions: read-only' on every agent task in this file.`

For the first agent run, start `atelier serve --no-scheduler` in one
terminal. In another terminal in the same project, run
`atelier check pair --probe && atelier run pair`. Follow the printed run-page link.

Give one agent its skills, a goal and one checkable result. Split for clean
context, parallel work, an independent test or review, or a different permission
level or agent. [Ask one agent or compose one repeatable task](../first-workflow.md).

The [first workflow guide](../first-workflow.md#try-failure-and-recovery-with-bash) includes a Bash exercise that builds a
three-step conduit, fails it on purpose, diagnoses it from saved history, and
shows what `--resume` keeps that `--again` redoes.

To combine a fix/test loop, swappable agents, and parallel reviews in one
file, use `atelier create ship --template fix-loop` and follow the
[loop, agents, and graph walkthrough](../loop-agents-graph.md).

## Your first AI workflow: review what you staged

`atelier create --template code-review` writes a two-step conduit that
captures the patch you have staged and hands it to Claude Code for a
review. Nothing runs at creation time — you get an ordinary YAML file to
read, run, and edit.

```bash
atelier harness check claude-code   # confirms the agent starts and you're logged in
atelier harness check claude-code --prompt   # also sends a one-line prompt (a few tokens)
atelier create my-review --template code-review
atelier check my-review             # validates it and confirms the harness is usable
atelier plan my-review              # prints the two waves, runs nothing
git add -p                          # stage the changes you want reviewed
atelier run my-review
atelier outputs latest --task review
```

You need a Git repository, `bash`, and Claude Code installed and logged in
(see [Optional: AI harnesses](../configuration/agent-logins.md#optional-ai-harnesses) for its launcher
prerequisites). The review covers **only what is staged** — `git diff
--cached`. Unstaged edits and untracked files are left out until you
`git add` them, and an empty index skips the review step instead of
calling the agent.

The workflow only reads your repository: it never stages, commits, or
edits anything for you. The prompt tells the agent to analyse rather than
act, which is an instruction to the agent, not a sandbox around it.

If a run fails, `atelier status latest` and `atelier logs latest` say which
step broke and what it printed. `latest` means the most recently started
run in this project; pass the printed flow id instead when several runs
overlap. To change the prompt, the harness, or the diff range, edit
`.atelier/conduits/my-review/conduit.yaml` — it is a normal conduit, and
`atelier show my-review` prints the exact prompt it will send.

## Your first multi-agent workflow: chain or panel

`atelier compose` turns a brief and a list of agents into one conduit, with
no YAML to write. Each `--step` is one agent; by default each hands its
result to the next.

Every line below gates the next with `|| exit 1`, so a logged-out agent or a
name already taken stops here instead of running something else:

```bash
atelier harness check claude-code || exit 1
atelier harness check codex || exit 1
atelier compose triage --permissions read-only \
  -s "claude-code=Propose one concrete fix. Say what you would change and why." \
  -s "codex=Review the proposal above. Name what breaks, or say it is sound." \
  || exit 1
atelier check triage || exit 1
atelier run triage --input brief="The nightly billing job is late." --json > triage.json || exit 1
flow_id=$(python3 -c 'import json; print(json.load(open("triage.json"))["flow_id"])') || exit 1
atelier outputs "$flow_id" --task step_2 || exit 1
```

`harness check` opens a session and closes it without sending a prompt, so it
costs no tokens. `triage` has to be a name no conduit of yours uses yet:
compose never overwrites one, and without the gate the rest of the block
would run whatever workflow already had that name.

`--parallel` runs the agents at the same time from the same brief instead,
and `--synthesize <harness>=<prompt>` adds one final step that reads every
result. Composition writes the file and stops: no agent starts until you
run it, and the conduit it writes is an ordinary one to read and edit.

[Orchestrating several agents in one workflow](../multi-agent-workflow.md)
walks the whole path — compose, inspect, run, read back, recover — for both
shapes.

## Running one workflow with different agents

The agent a step names is a default. `--agent <task>=<harness>` on `run` and
`plan` runs one task on another agent for one invocation, leaving the file
alone, and a resume reuses that choice or replaces it for the step that failed:

```bash
atelier plan triage --agent step_2=claude-code   # preview; starts nothing
atelier run triage --agent step_2=claude-code --input brief="..."
atelier status <flow_id>                         # which agent ran which task
atelier run --resume <flow_id> --agent step_2=codex
```

[Running one workflow with different
agents](../reusing-a-workflow-with-other-agents.md) walks it end to end.

## Checking the whole agent team before a run

`atelier check <name>` reads files. `atelier check <name> --probe` asks the
agents: it starts every distinct agent the workflow would use, opens a
session with each and stops, so a logged-out or missing agent is found
before the first task rather than four minutes into a run. No prompt is
sent. It takes the same `--agent <task>=<harness>` mapping as `run`, so the
team you check is the team you run:

```bash
atelier check triage --probe --recursive --timeout 60   # the whole call tree
atelier check triage --probe --agent step_2=claude-code \
  && atelier run triage --agent step_2=claude-code --input brief="..."
```

Each distinct `harness:<name>[:<model>[:<effort>]]` is started once and its
verdict is attributed to every task it answers for; failures are collected
across the team instead of stopping at the first. `--probe --json` emits the
same report as one document for a script or a coding agent. Passing proves
startup only — not prompt-time authentication, quota, model access or output
quality — and nothing is remembered: `--agent` applies to that one command.

[Checking the whole agent team before a
run](../checking-the-team-before-a-run.md) walks it end to end.

## Two coding agents changing the same code at once

Tasks of a run share one working directory, which is fine while the agents are
only reading. When they edit, `--worktree <task>` gives each selected task its
own detached Git worktree, cut from your checkout's current commit before
anything starts — so two agents can rewrite the same file and both results
survive for you to compare:

```bash
atelier plan candidates --worktree step_1 --worktree step_2   # preview; cuts nothing
atelier run candidates --worktree step_1 --worktree step_2 --input brief="..."
atelier status <flow_id>                       # the path each task worked in
atelier run --resume <flow_id>                 # continues in those same checkouts
```

The checkouts are kept after the run and `atelier rm` never deletes one.
Nothing is merged, reset or pushed for you, untracked and ignored files are not
copied, and a worktree is filesystem separation, not a sandbox. [Two agents
changing the same code at
once](../parallel-agents-in-separate-checkouts.md) walks it end to end.

If a run fails, see [Recovering a failed workflow](../recovering-a-failed-workflow.md).
