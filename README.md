![flow-atelier](./atelier.jpg)

# Flow Atelier

**Workflows and loops, configured in one simple YAML file.** The steps are
shell commands, AI coding agents, and human approvals. Run them from your
terminal or a local visual editor.

Flow Atelier is a workflow runner whose tasks can hand work to Claude Code,
Codex, Gemini, opencode, Copilot, Cursor, or any of the ~40 agents in the
[ACP registry](https://agentclientprotocol.com/get-started/registry) — each
driven through *your* existing login for that tool. No API keys are
configured, stored, or proxied by flow-atelier.

There is no SDK to learn and no code to write: a workflow is a plain YAML file
you (or an agent) can read end to end and edit. Steps run in parallel where their dependencies allow,
**loop** until output matches or while it keeps matching, retry on failure,
branch on what a previous step printed, pause for a typed human answer, and
write every run to disk so you can replay it.

```yaml
name: ci
description: Run the test suite until it passes
tasks:
  - run_until_green:
      description: retry the tests until they pass, up to 5 times
      task: "make test"
      tool: tool:bash
      depends_on: []
      repeat: 5                   # loop this task up to 5 times...
      until: output.match(PASS)   # ...stopping as soon as the output matches
```

Save that as `.atelier/conduits/ci/conduit.yaml` and run `atelier run ci`.

```bash
curl -fsSL https://raw.githubusercontent.com/Andesprit/flow-atelier/main/install.sh | bash
atelier init                             # writes a hello-world conduit
atelier run hello --input name=world     # runs it
atelier serve                            # opens the visual editor on :8000
```



## What it looks like

The designer lays a conduit out by dependency depth, so each column is a set of
steps that can run at the same time:

![Designer](./docs/img/designer.jpg)

Runs stream to the dashboard, including the human-approval gates:

![Dashboard](./docs/img/dashboard.jpg)

## How it compares


|                   | Flow Atelier                                            | n8n / Zapier                   | Prefect / Dagster / Airflow | GitHub Actions                  |
| ----------------- | ------------------------------------------------------- | ------------------------------ | --------------------------- | ------------------------------- |
| Runs where        | your machine, local-first                               | hosted / self-hosted server    | scheduler + workers         | CI runners                      |
| AI steps          | a first-class task type, using your own agent CLI login | LLM API nodes, you supply keys | you write the client code   | you write the client code       |
| Human-in-the-loop | built in, mid-DAG, blocks the run                       | via external forms/webhooks    | not really                  | manual approval on environments |
| Workflow format   | one simple YAML file per conduit                        | JSON built in a GUI            | Python                      | YAML                            |
| Loops & retries   | `repeat` / `until` / `while` / `retries` on any task    | loop and wait nodes in the GUI | Python control flow         | matrix builds; no retry-until   |
| State             | plain files under `.atelier/`                           | a database                     | a database                  | opaque to you                   |


Pick Flow Atelier when the work is a repeatable recipe you want AI agents to
execute on your own machine, with you in the loop at the points that matter.
Pick the others when you need multi-tenant hosting, distributed workers, or
enterprise scheduling — flow-atelier deliberately does none of that.

> **Note on scope.** flow-atelier runs shell commands and AI agents on the
> machine it is installed on. It is a local developer tool, not a hosted
> multi-user service. See [Security](#security) before exposing it on a network.



## Why processes, not agents

Flow Atelier is built on a simple premise: the world doesn't run on people, it runs on processes that people execute.
Ask someone to make anything, for example: the best yogurt in the world, what a person will do is that they'll research, experiment, and produce something average. The same is true of an AI agent. But give that person a clear, step-by-step recipe — and keep refining it over time — and you can reach the best yogurt in the world.
That's what Flow Atelier does. We help you build simple, repeatable instructions for getting something done the same way every time, then let anyone improve on them. It mirrors how real businesses actually work. Coca-Cola and Pepsi don't differ because their managers, lawyers, or engineers are fundamentally different people — they differ because their processes are different.
Flow Atelier gives you the tool to design and refine those processes. The difference: instead of people executing them, AI agents do the work.

## The words we use

A few terms show up everywhere in this document:

- **Conduit** — a recipe. An ordered set of steps written in a single
YAML file.
- **Task** — one step in that recipe. A task runs a shell command,
asks a person a question, calls an AI tool, or runs another conduit.
- **Flow** — one run of a conduit. Every run is saved to disk, so you
can always go back and see exactly what happened.
- **Harness** — an AI coding tool (Claude Code, Codex, opencode,
Copilot, Cursor) that a task can hand work to.
- **HITL** — "human in the loop": a step that pauses to ask a person a
typed question, then continues with the answer.



## What can you build with it?

Any pipeline that can be described as an ordered sequence or graph of
steps. The two examples further down — a one-line greeter and a deploy
pipeline with a human approval gate — illustrate two possible shapes;
they are not prescriptive templates.

A non-exhaustive list of things people have built:

- **Chatbots and AI agents** that chain multiple turns of conversation
with retries, branches, and fallbacks.
- **Multi-AI pipelines** in which one assistant drafts a specification,
a second produces a plan, and a third executes it — or in which two
assistants review the same change and a third synthesizes their
feedback.
- **CI/CD-style pipelines** that clone a repository, run tests with
retry, request an AI code review, ask a human for confirmation, and
then deploy or roll back based on the result.
- **Scheduled jobs** such as daily reports, weekly syncs, or one-shot
reminders at a specific time.
- **Polling loops with retry and backoff** that call an endpoint until
it returns a success status or a rate limit lifts.
- **Human-in-the-loop automations** — flows that pause to ask the
operator a typed question and resume with the answer.
- **Reusable building blocks** — one conduit invoking another, so a
`deploy` conduit written once can be called from many higher-level
pipelines.
- **Pure-shell automation with no AI at all** — flow-atelier works as
a general-purpose task runner in this mode.

If a task can be described as a sequence or graph of steps, it can be
written as a conduit.

## How a conduit runs

You write a conduit YAML file and put it in
`.atelier/conduits/<name>/conduit.yaml`. When you run
`atelier run <name>`, flow-atelier:

1. Reads the YAML.
2. Looks at each task's `depends_on` list to figure out which tasks can
  start now and which have to wait.
3. Starts every ready task at the same time, up to a configurable
  limit (`max_concurrency`).
4. For each task, picks an executor based on the `tool:` field:

  | Tool                  | What it runs                                                         |
  | --------------------- | -------------------------------------------------------------------- |
  | `tool:bash`           | a shell command                                                      |
  | `tool:hitl`           | prompts a human on the terminal for one or more named answers        |
  | `tool:conduit`        | another conduit, as a nested run                                     |
  | `harness:claude-code` | Claude Code (via the [ACP](https://agentclientprotocol.com) adapter) |
  | `harness:codex`       | OpenAI Codex (via the ACP adapter)                                   |
  | `harness:opencode`    | [opencode](https://opencode.ai)                                      |
  | `harness:copilot`     | GitHub Copilot CLI                                                   |
  | `harness:cursor`      | Cursor CLI                                                           |

5. Saves everything to disk under `.atelier/flows/<flow_id>/` — what
  ran, what each task printed, whether it succeeded, when it
   finished.

Every AI harness uses that tool's **own login** that lives on your
machine. flow-atelier never sees, stores, or proxies any credentials.

## Install



### One-command install (no Python needed)

The quickest way. The script downloads a prebuilt `atelier` binary into
`~/.atelier/bin`, verifies its SHA-256 checksum against the published
release, and adds it to your `PATH`. It is safe to re-run to upgrade.

**macOS (Apple Silicon) / Linux:**

```bash
curl -fsSL https://raw.githubusercontent.com/LGuillermoAngaritaG/flow-atelier/main/install.sh | bash
```

**Windows (PowerShell):**

```powershell
irm https://raw.githubusercontent.com/LGuillermoAngaritaG/flow-atelier/main/install.ps1 | iex
```

Prebuilt binaries are published for **Linux x86_64**, **macOS arm64
(Apple Silicon)**, and **Windows x86_64**. Intel Macs are not supported
(no Rosetta fallback exists for an arm64 binary). Open a new terminal
after installing so the updated `PATH` takes effect.

### Install with uv (for Python users)

If you have Python 3.13+ and [uv](https://docs.astral.sh/uv/), you can
install from PyPI instead:

```bash
uv tool install flow-atelier
uv tool upgrade flow-atelier      # upgrade later
uv tool uninstall flow-atelier    # remove
```

Either way, you end up with an `atelier` command on your `PATH`.

### Optional: AI harnesses

You only need the AI tools you actually plan to use. If you never use AI
in your conduits, you can skip this entire section.

**flow-atelier does not install agents and does not manage their logins.**
You install the agent you want and log into it with its own CLI; then you
point flow-atelier at its command, either by name or by argv. There is no
bundled installer, no download manager, and no credential handling here.

What flow-atelier does do is run the command you selected, exactly as that
agent documents it. For agents distributed through `npx` or `uvx`, the
documented command fetches the package on first use — that is the agent's
own distribution mechanism doing its normal thing, the same as running the
command yourself in a shell. Agents distributed as a binary are never
downloaded; you install those, and flow-atelier runs what it finds on
PATH.

An AI task names its agent and nothing else:

```yaml
- review:
    description: review the diff
    task: "review the working tree and list any bugs"
    tool: harness:gemini
    depends_on: []
```

The names come from the [ACP registry](https://agentclientprotocol.com/get-started/registry),
a snapshot of which ships with flow-atelier. To see what you can type and
what already works on your machine:

```bash
atelier harness list           # every agent, and whether it runs here
atelier harness list --ready   # just the ones you can use right now
atelier harness sync           # refresh the list from the ACP registry
```

Roughly 40 agents are listed, including `harness:claude-code`,
`harness:codex`, `harness:gemini`, `harness:copilot`, `harness:cursor`,
`harness:opencode`, `harness:qwen-code`, `harness:goose` and
`harness:amp-acp`. A name is only the launch command that agent
documents; the `via` column says how it starts:

- `npx` / `uvx` — the agent's own package manager fetches it on first
run, at the version the registry pins. Needs Node.js or uv on PATH.
- `binary` — you install the agent's CLI, and flow-atelier runs it from
PATH. `atelier harness list` names the missing binary when it isn't there.

Either way, logging in is yours to do, with that agent's own CLI.

#### Checking a harness before you use it

```bash
atelier harness check gemini
atelier harness check --cmd "/opt/my-agent --acp"
```

This starts the agent, completes the ACP handshake, opens a session and
stops. No prompt is sent, so it costs no tokens. It reports one of:

- **ok** — with the agent's name and version, the ACP version, and the
  session modes it offers.
- **not found on PATH** — install the agent yourself, then re-check.
- **started but did not speak ACP** — usually the wrong entry point;
  many CLIs need an `--acp` flag.
- **could not open a session** — usually not logged in. The check lists
  the auth methods the agent advertises, and you log in with that
  agent's own CLI.

Failures exit non-zero and include the tail of the agent's own stderr,
which is where a failing agent explains itself.

For an agent the registry doesn't list — something private, a fork, a
local build — give flow-atelier its command and it becomes a first-class
harness:

```bash
ATELIER_HARNESSES='{"mine":["/opt/my-agent","--acp"]}'   # tool: harness:mine
```

To pin one of `claude-code`, `codex`, `opencode`, `copilot` or `cursor`
to a specific argv, the matching `ATELIER_*_LAUNCH_CMD` variable still
overrides the registry (see `.env.example`).

## Quickstart

```bash
atelier init                                # creates .atelier/conduits/hello/
atelier run hello --input name=world        # runs it
atelier status latest                       # shows progress of the newest run
atelier list flows --conduit hello          # lists previous runs
```

`atelier init` writes a one-line `hello` conduit that only runs a
shell command, so this works end-to-end before you install any AI
tool.

## Examples

The two conduits below are **illustrative, not prescriptive**. A
conduit can have one step or fifty, and any combination of shell, AI,
and human steps. The samples show one minimal conduit and one larger
one to demonstrate the range; the conduits you write will look
nothing like them.

### A simple conduit (`hello`)

The one-task conduit that `atelier init` creates. It runs a single
shell command:

```yaml
name: hello
description: Say hello
inputs:
  name: Who to greet
tasks:
  - greet:
      description: greet someone
      task: "echo hello {{inputs.name}}"
      tool: tool:bash
      depends_on: []
```

Run it with `atelier run hello --input name=world`.

### A bigger conduit (`deploy_pipeline`)

A six-step pipeline that combines shell commands, an AI review, a
human approval gate, retry loops, conditional branches, and a nested
sub-conduit. It illustrates what is possible — a chatbot, a daily
report, or an agent loop would look entirely different.

```yaml
name: deploy_pipeline           # must match the folder name
description: Build test deploy
timeout: 3600                   # seconds per task, default 3600
max_concurrency: 3              # max tasks running in parallel, default 3

inputs:
  repo_url: The git repo URL
  branch: Branch to deploy
  env: Target environment

tasks:
  - clone_repo:
      description: Clone
      task: "git clone -b {{inputs.branch}} {{inputs.repo_url}} /tmp/build"
      tool: tool:bash
      depends_on: []

  - run_tests:
      description: Run tests
      task: "cd /tmp/build && make test"
      tool: tool:bash
      depends_on: [clone_repo]
      repeat: 3                          # try up to 3 times
      until: output.match(PASS)        # ...stopping early on success

  - code_review:
      description: AI review
      task: |
        Review /tmp/build/src for security issues.
        End your response with exactly one of:
        VERDICT: APPROVE
        VERDICT: REJECT
      tool: harness:claude-code
      depends_on: [clone_repo]
      interactive: false

  - approve:
      description: human gate
      task: "I need a final confirmation"
      tool: tool:hitl
      depends_on:
        - run_tests
        - code_review.output.match(VERDICT:\s*APPROVE)
      inputs:
        confirm: "Type 'yes' to approve deploy"
        reason: "Short reason for the decision"

  - deploy:
      description: Run deploy sub-conduit
      task: deploy_to_env
      tool: tool:conduit
      depends_on: [approve]
      inputs:
        target_env: "{{inputs.env}}"
        build_path: /tmp/build

  - rollback:
      description: Rollback if review rejected
      task: "make rollback"
      tool: tool:bash
      depends_on:
        - code_review.output.not_match(VERDICT:\s*APPROVE)
```

Step by step:

- `clone_repo` runs first because nothing depends on it.
- `run_tests` and `code_review` both wait on `clone_repo`, then run
in parallel.
- `run_tests` retries up to 3 times, stopping as soon as the output
contains `PASS`.
- `code_review` asks Claude Code to review the code and end with
either `VERDICT: APPROVE` or `VERDICT: REJECT`.
- `approve` only runs if Claude approved (`...match(VERDICT:\s*APPROVE)`).
It asks the human two typed questions on the terminal.
- `deploy` only runs after the human approves, and calls another
conduit (`deploy_to_env`) as a nested run.
- `rollback` only runs if Claude rejected. The two branches are
mutually exclusive — the unmet branch is silently skipped, not
failed.



## Conduit reference

A conduit has a `name`, a short `description`, an optional `inputs`
map, and a `tasks` list. Each task has a `name`, a `task` body, a
`tool` value, and a `depends_on` list.

### Templating

- `{{inputs.<name>}}` — a conduit input or HITL answer.
- `{{<task_name>.output}}` — the printed output of an earlier task.
The earlier task must appear in `depends_on`.
- `{{loop.previous}}` — this task's output from its previous loop
iteration (empty before the first iteration completes). Only valid on
a looping task (`repeat > 1`).
- `{{loop.history}}` — every prior iteration of this task, rendered as
numbered blocks. Only valid on a looping task (`repeat > 1`).

A missing `{{inputs.x}}` fails the task immediately; a reference to a
task that was skipped or hasn't completed skips the referencing task.
`atelier run` rejects an `--input` key the conduit neither declares nor
references, so a mistyped key fails before the run starts.

### Conditional dependencies

```
<task>.output.match(<regex>)        # dependency met if regex matches
<task>.output.not_match(<regex>)    # dependency met if regex does NOT match
```

The regex is everything between the leftmost `(` and the last `)`.
Python's `re.search` is used.

Quotes around the regex are optional and stripped when present, so
`output.match(PASS)` and `output.match("PASS")` behave identically. To match a
literal quote character, escape it — `output.match(\"PASS\")` looks for `"PASS"`
*with* the quotes.

If a condition is not met, the task is **skipped**, not failed.
Anything that depends on a skipped task is also skipped.

### Loops (`repeat` + `until` / `while`)

A task with `repeat > 1` can break out of its loop early:

```
until: output.match(<regex>)       # break as soon as an output matches
until: output.not_match(<regex>)   # break as soon as no output matches
while: output.match(<regex>)       # loop while an output matches; break otherwise
while: output.not_match(<regex>)   # loop while no output matches; break otherwise
```

Set at most one of `until` / `while`. The first iteration always runs
before the predicate is checked.

For `tool:conduit` loops, the predicate sees **every nested sub-task
output of that iteration** and fires on any match.

```yaml
- retry_while_rate_limited:
    tool: tool:bash
    task: 'curl -s -o body -w "%{http_code}" https://api/x'
    repeat: 10
    while: output.match(^429$)

- run_until_test_passes:
    tool: tool:conduit
    task: build_and_test
    repeat: 5
    until: output.match(PASS)
```



### Retries and per-task timeout

- `retries: <n>` — if a task *fails*, re-run it up to `n` more times
(default `0`). This is different from `repeat`, which loops a task
that is *succeeding*.
- `timeout: <seconds>` — override the per-task time limit for one task.
When omitted, the conduit-level `timeout` applies.



### Asking a human (`tool:hitl`)

A `tool:hitl` task declares its own `inputs: {name: description}`
map. At runtime flow-atelier prints the prompt, asks for each input
by name on the terminal, and saves the answers so downstream tasks
can use them as `{{inputs.<name>}}`.

### Long AI conversations (`interactive: true`)

When a harness task sets `interactive: true`, flow-atelier appends
this line to every message it sends to the AI:

> When — and only when — you are completely finished, output the exact
> token `[ATELIER_DONE]` to signal completion.

Then it keeps the conversation open: the AI replies, flow-atelier
streams the reply to your terminal, and if the AI didn't write
`[ATELIER_DONE]` yet, flow-atelier asks **you** for the next message
to send back. The loop ends when `[ATELIER_DONE]` shows up.

How that next message reaches flow-atelier depends on where the run
started. On the terminal it reads one line from stdin — typed at the
`› ` cursor, or piped in for scripted runs. Under `atelier serve` the
same conversation travels over `/ws/run-conduit`: the agent's prose is
streamed to the client as it is written, and the client sends the reply
back on the same socket.

If the AI asks for permission to run a tool, you'll see a numbered
menu on the terminal; your choice is sent back as the answer.

Non-interactive tasks run one turn and stop.

For a direct interactive Claude conversation without writing a conduit,
use `atelier ask`:

```bash
atelier ask "Help me write a specification" --path /absolute/path/to/project
```

`--path` is required and becomes Claude's working directory. Claude's
questions are read from stdin just like any other interactive harness task.
The resulting flow is still recorded under the `.atelier/flows/` directory
from which you invoked `atelier`.

#### Interactive turns over the WebSocket

Three envelopes carry the conversation. They are additive — a client
that ignores them still runs flows exactly as before.

| From   | `type`                | Fields                                    |
| ------ | --------------------- | ----------------------------------------- |
| server | `agent_message`       | `flow_id`, `task`, `text`                 |
| server | `agent_input_request` | `flow_id`, `task`, `request_id`, `prompt` |
| client | `agent_input_answer`  | `flow_id`, `request_id`, `answer`         |

`agent_message` is one chunk of agent prose, sent as the agent writes
it, so the client can show the question before the request to answer it
arrives. `agent_input_request` is the turn being handed back to you.

The `request_id` from a request must be echoed verbatim in the answer.
That is what allows several interactive tasks in one flow to be waiting
at the same time: each reply goes to the prompt that asked for it, not
to whichever prompt happens to be pending. An answer carrying an
unknown or already-used `request_id` comes back as an `error` envelope.

The socket is only the local transport between the UI and the
`atelier serve` process on your machine. The harness itself still runs
as that agent's own CLI under your existing login for it, so an
interactive conversation over the WebSocket needs no LLM API key —
exactly as on the terminal.

## Where conduits live

Conduits can live in two places:

- **Project**: `./.atelier/conduits/` — scaffolded by `atelier init`.
- **Global**: `~/.atelier/conduits/` — shared across all projects.

When you run a conduit, flow-atelier checks the project folder first,
then the global folder. A project-level conduit silently overrides a
global one with the same name.

Flows are **always project-local** — every `atelier run` writes its
flow folder under `.atelier/flows/` in the current working directory.

## Commands

```
# authoring
atelier init
atelier create <name> [--description <text>]           # scaffold a new empty conduit
atelier check [<conduit>]                              # validate conduit(s) without running
atelier plan <conduit>                                 # print the DAG as ordered waves, run nothing

# <flow_id> below accepts a unique prefix, or 'latest' for the most recently started flow
# running
atelier run <conduit> [--input key=value ...] [--show-steps/--hide-steps]
atelier ask <query> --path <directory>                 # interactive Claude session
atelier run --resume <flow_id>                         # resume a failed/crashed flow
atelier run --again <flow_id>                          # fresh run reusing a past flow's inputs
atelier stop <flow_id>                                 # gracefully halt a running flow

# inspecting
atelier status <flow_id>
atelier logs <flow_id> [--task <name>] [--follow] [--json]
atelier outputs <flow_id> [--task <name>] [--json]    # read back a finished flow's results
atelier timing <flow_id> [--json]                      # per-task duration, slowest first
atelier list conduits
atelier list flows [--conduit <name>]
atelier rm <flow_id> [--force] [--yes]                 # delete one flow run
atelier prune [--conduit <name>] [--older-than <days>] [--keep <n>]   # bulk-delete old flows

# sharing conduits (see "Installing conduit packages" below)
atelier add <source> [--ref <git-ref>] [--project] [--force]
atelier update <package>                               # re-fetch and re-install from source
atelier remove <package>                               # uninstall a package's conduits

# scheduling
atelier schedule add <file.{json,yaml}>
atelier schedule list [--json]
atelier schedule remove <id-or-name>
atelier schedule run-now <id-or-name>
atelier scheduler start [--reload-interval 30] [--log-level INFO]
atelier scheduler status [--json]

# HTTP + WebSocket server
atelier serve [--host 127.0.0.1] [--port 8000] \
              [--reload-interval 30] [--cors-origin URL]* \
              [--log-level INFO]
```



## Installing conduit packages

A conduit is just a folder, so conduits are shareable. `atelier add`
installs them from a git repo or a local path:

```bash
atelier add owner/repo                  # GitHub shorthand
atelier add https://github.com/owner/repo.git
atelier add ./some/local/package
atelier add owner/repo --ref v1.2.0     # pin a branch, tag, or commit
```

You are asked whether to install globally (`~/.atelier`) or into the
current project (`./.atelier`); `--project` / `--no-project` answers that
up front. An existing conduit of the same name is **skipped**, not
overwritten, unless you pass `--force`.

> **Conduits are code.** A conduit can run any shell command on your
> machine the moment you `atelier run` it. Read a package before you
> install it, and pin `--ref` for anything you don't control.

A package is any repo with its conduits under `.atelier/conduits/` and an
`atelier-package.yaml` at the root:

```yaml
name: my-conduits        # letters, digits, _ and - only
version: 1
conduits:
  - deploy
  - nightly_report
```

Each listed name must be a directory under `.atelier/conduits/`. The whole
directory is copied, so helper scripts and templates next to
`conduit.yaml` travel with it. Without a manifest, flow-atelier discovers
conduits by scanning that directory and warns that it did so. Schedules
are never installed — they hold machine-specific state.

`atelier update <package>` re-fetches from the recorded source and
re-installs. `atelier remove <package>` deletes only the conduits that
install actually wrote, so a conduit that was skipped on collision is
left alone.

## Running on a schedule

`atelier scheduler` runs conduits on a wall-clock schedule. Each
schedule is one YAML file under `.atelier/schedules/<name>.yaml`. The
daemon is one foreground process you can put under `systemd`,
`launchd`, or any supervisor.

To register a schedule, write a YAML file like the one below and run
`atelier schedule add <file>`:

```yaml
conduit_name: report
inputs:
  date: today
run_path: /abs/path
schedule:
  mode: recurring
  name: weekday mornings
  days: [1, 2, 3, 4, 5]
  times: ["06:00", "12:00"]
```

`days` are `1=Mon` .. `7=Sun`; `times` are `"HH:mm"` 24-hour strings.
One-shots use `mode: once` with a `run_at` ISO datetime instead of
`days` / `times`. Fixed intervals use `mode: interval` with
`every_minutes` (e.g. `every_minutes: 30` for every half hour, `120`
for every two hours) — these repeat forever. `atelier schedule add`
also accepts the same shape in JSON if you prefer that format. Like
`atelier run --input`, it rejects an `inputs` key the conduit neither
declares nor references, so a mistyped key fails at install time instead
of silently running with the default on every fire.

- New or removed schedules are picked up on the next reload tick
(default 30s).
- One-shot schedules remember they fired, so a daemon restart never
re-runs them.
- Each schedule runs at most one instance at a time; missed fires
are coalesced.
- `atelier schedule run-now <id-or-name>` fires a schedule
immediately, bypassing the daemon.



## HTTP API (`atelier serve`)

`atelier serve` boots a single process that hosts both the HTTP /
WebSocket API and the scheduler daemon. It is the entry point the
Flow Atelier visual frontend connects to.


| Method   | Path                  | Notes                                     |
| -------- | --------------------- | ----------------------------------------- |
| `GET`    | `/conduits`           | List conduits                             |
| `GET`    | `/conduits/:name`     | Read one                                  |
| `POST`   | `/conduits`           | Create (201 on success, 409 on collision) |
| `PATCH`  | `/conduits/:name`     | Partial update                            |
| `DELETE` | `/conduits/:name`     | Delete                                    |
| `POST`   | `/conduits/open-path` | Reveal flow run path in OS file explorer  |
| `POST`   | `/tasks/run`          | Run an ad-hoc one-task conduit            |
| `GET`    | `/schedules`          | List active schedules                     |
| `POST`   | `/schedules`          | Create                                    |
| `DELETE` | `/schedules/:id`      | Soft-delete                               |
| `GET`    | `/flows`              | List prior flows                          |
| `GET`    | `/flows/:id/logs`     | Per-flow log entries                      |
| `WS`     | `/ws/run-conduit`     | Run flows, HITL + interactive AI turns    |


Binds to `127.0.0.1:8000` by default; pass `--host 0.0.0.0` to expose
on the LAN — which requires `ATELIER_API_TOKEN`, see [Security](#security).
`--cors-origin` is repeatable.

Conduits and flows resolve exactly as they do on the CLI — `./.atelier`
first, then `~/.atelier` — so the conduits `atelier init` created in the
directory you started the server from are the ones the UI shows.
Schedules are the exception: they live in `~/.atelier/schedules/`, since
one daemon serves every project.

## Security

The API runs shell commands on the machine hosting it, so treat reaching it as
equivalent to a shell on that machine.

**On loopback (the default).** `atelier serve` binds `127.0.0.1:8000` and needs
no token. Two guards keep a web page you happen to visit from driving it:

- **Origin.** CORS is restricted to localhost origins, never `*`.
- **Host.** Only `localhost`, `127.0.0.1`, and `::1` are accepted as the `Host`
header. This is what stops DNS rebinding, where an attacker's page resolves
its own hostname to `127.0.0.1` so the browser treats the request as
same-origin and sends no `Origin` for CORS to reject. Requests carrying any
other `Host` get `400 Invalid host header`.

**Anywhere else.** Before binding to a non-loopback address, set
`ATELIER_API_TOKEN`. Every REST request then needs
`Authorization: Bearer <token>` and WebSocket connections need `?token=<token>`;
build the UI with a matching `VITE_API_TOKEN` so it can reach the authenticated
API.

`atelier serve` **refuses to start** on a non-loopback host when
`ATELIER_API_TOKEN` is unset. This used to be a warning that scrolled past in
the same second the port opened, so an existing `--host 0.0.0.0` setup with no
token will now stop rather than serve:

```console
$ atelier serve --host 0.0.0.0
error: refusing to serve on non-loopback host '0.0.0.0' without
ATELIER_API_TOKEN. Anyone who can reach this address could run shell
commands via the API. Set ATELIER_API_TOKEN, or bind 127.0.0.1 (the default).
```

Binding a specific host also adds that host to the accepted `Host` values; a
wildcard bind (`--host 0.0.0.0`) cannot know which names reach it, so it accepts
any `Host` and relies on the token — which is why the token is mandatory there
rather than merely advised.

**Tool arguments reach your terminal.** `atelier run` prints the argument that
identifies each tool call — the bash command, the file path, the search
pattern — so the run is readable. Credential-shaped values (`Bearer <token>`,
`sk-`/`ghp_`/`xox`-prefixed keys, `--password`/`TOKEN=` flags) are masked as
`***` on the way to the screen. This is a heuristic that reduces casual
leakage, not a guarantee: it will miss a secret that does not look like one.
The recorded logs under `.atelier/flows/<id>/` keep the **unredacted** text, so
treat that directory as sensitive and check what you are pasting before sharing
a terminal transcript.

**Conduits are code.** See the warning under
[Installing conduit packages](#installing-conduit-packages): running a conduit
runs whatever shell commands it contains.

## Folder layout

The `.atelier` directory lives in the working directory where
`atelier` is invoked.

```
.atelier/
├── conduits/
│   └── <conduit_name>/conduit.yaml
├── schedules/
│   └── <schedule_name>.yaml                # one YAML file per schedule
├── scheduler_state.json                    # fired-once markers
└── flows/
    └── <flow_id>/                          # <YYYYMMDD>_<uuid8>_<conduit>
        ├── input.yaml                      # the inputs this run was given
        ├── logs.jsonl                      # append-only log, one JSON object per line
        ├── progress.json                   # live per-task status
        ├── outputs.yaml                    # per-task outputs (written as tasks finish)
        └── flows/
            └── <child_flow_id>/...         # nested tool:conduit runs
```



## Contributing

For test instructions, the project layout, and internal architecture
notes, see [DEVELOPMENT.md](./DEVELOPMENT.md).
