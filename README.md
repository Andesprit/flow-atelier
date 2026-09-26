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

Once a day the binary checks GitHub for a newer release and, if one
exists, says so on stderr. Nothing is installed until you run
`atelier self-update`. Set `ATELIER_NO_UPDATE_CHECK=1` to silence the
check.

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
atelier list harnesses           # every agent, and whether it runs here
atelier list harnesses --ready   # just the ones you can use right now
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
PATH. `atelier list harnesses` names the missing binary when it isn't there.

Either way, logging in is yours to do, with that agent's own CLI.

#### Picking a model

A third segment names the model, spelled the way the agent lists it:

```yaml
    tool: harness:codex:gpt-5.1-codex
    tool: harness:claude-code:claude-sonnet-4-5
```

flow-atelier selects it on the ACP session before the first prompt. A
model the agent does not offer fails the task at once and prints the
models it does offer. `atelier harness check <name>` lists them too, as
`models:`. Without the suffix the agent runs on its own default.

#### Picking a reasoning effort

A fourth segment names how hard that model should think, again spelled
the way the agent lists it:

```yaml
    tool: harness:codex:gpt-5.6-sol:high
    tool: harness:claude-code:opus[1m]:xhigh
```

Effort only comes after a model, because which efforts exist is a
property of the model: the agent is asked for the model first, and the
efforts it offers *for that model* are what the value is checked
against. An effort that model does not offer fails the task before the
prompt is sent, and prints the ones it does offer.

Leave the segment off and the model keeps whatever effort it defaults
to — flow-atelier sends nothing, so your own agent configuration stands.

To see the choices for a given model, no prompt and no tokens:

```bash
atelier harness check codex             # efforts for its default model
atelier harness check codex:gpt-5.6-sol # efforts for that model
```

#### Checking a harness before you use it

```bash
atelier harness check gemini
atelier harness check codex:gpt-5.6-sol
atelier harness check --cmd "/opt/my-agent --acp"
```

This starts the agent, completes the ACP handshake, opens a session and
stops. No prompt is sent, so it costs no tokens. A `:<model>` or
`:<model>:<effort>` suffix is resolved the way a run resolves it, so
what the check reports is what a task naming that same tool would get.
It reports one of:

- **ok** — with the agent's name and version, the ACP version, the
  session modes it offers, and the models and reasoning efforts it
  offers for the model in force.
- **not found on PATH** — install the agent yourself, then re-check.
- **started but did not speak ACP** — usually the wrong entry point;
  many CLIs need an `--acp` flag.
- **could not open a session** — usually not logged in. The check lists
  the auth methods the agent advertises, and you log in with that
  agent's own CLI.
- **not usable — model/effort not offered** — the session opened fine;
  the suffix named something the agent does not have. The message lists
  what it does.

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

Ready for a real one? [Your first workflow: run it, break it, recover
it](docs/first-workflow.md) is a 5-minute Bash-only exercise that builds a
three-step conduit, fails it on purpose, diagnoses it from saved history, and
shows what `--resume` keeps that `--again` redoes.

### Your first AI workflow: review what you staged

`atelier create --template code-review` writes a two-step conduit that
captures the patch you have staged and hands it to Claude Code for a
review. Nothing runs at creation time — you get an ordinary YAML file to
read, run, and edit.

```bash
atelier harness check claude-code   # confirms the agent starts and you're logged in
atelier create my-review --template code-review
atelier check my-review             # validates it and confirms the harness is usable
atelier plan my-review              # prints the two waves, runs nothing
git add -p                          # stage the changes you want reviewed
atelier run my-review
atelier outputs latest --task review
```

You need a Git repository, `bash`, and Claude Code installed and logged in
(see [Optional: AI harnesses](#optional-ai-harnesses) for its launcher
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

### Reading a conduit before you run it

Conduits arrive from `atelier init`, `atelier create`, a teammate's
repository, or an installed package. `atelier show` prints one without
running it — no task starts, no agent is launched, no run is recorded.

```bash
atelier list conduits --json          # what is installed here
atelier show hello                    # the exact YAML that would run
atelier show hello --json             # the same thing, normalized, for tools
atelier check hello                   # is it valid, and is its agent usable?
atelier run hello --input name=world  # run it
```

`atelier show hello` writes the file's own text to stdout — comments,
templates and multiline prompts exactly as written — and the source and
path to stderr, so `atelier show hello > copy.yaml` gives you a clean
copy. A project conduit shadows a global one of the same name here just
as it does at run time.

`--json` answers the question a script or a coding agent actually has:
how do I call this? (`conduit` is abbreviated below — the real output
carries the whole definition.)

```json
{
  "source": "project",
  "path": "/home/you/project/.atelier/conduits/hello/conduit.yaml",
  "conduit": { "name": "hello", "inputs": { "name": { "description": "Who to greet", "default": null } }, "tasks": [] },
  "accepted_inputs": ["name"],
  "required_inputs": ["name"]
}
```

- `conduit` is the whole definition — every task body, tool, dependency,
  loop and default — normalized, but with templates left unresolved.
- `accepted_inputs` is every key the conduit can use, including keys only
  referenced as `{{inputs.x}}` in a task and keys forwarded to a nested
  conduit.
- `required_inputs` is the subset you must pass: the declared inputs whose
  `default` is `null`. An input with `default: ""` is optional — an empty
  string is still a default.

So the `--input` flags a run needs are one command away:

```bash
atelier show hello --json | jq -r '.required_inputs[]'   # jq is optional
```

These are the conduit's *declarations*, not a promise that every template
resolves or that its agent is installed — `atelier check` still answers
that. `show` reads whichever copy would run even when that copy is
broken, so you can see the mistake; `--json` refuses to guess and exits
non-zero instead.

### Passing a file as an input

A brief, a spec, a failing build log — the material a workflow needs is
usually already a file. `--input-file key=path` hands that file's text to
one input, so you never paste a document into the command line:

```bash
atelier init
printf 'Ada' > name.txt
atelier run hello --input-file name=name.txt
atelier outputs latest
```

`--input-file` reads the file as UTF-8 and passes the text through
unchanged — every character, every trailing newline, no stripping, no
YAML parsing, no template expansion of anything inside it. Relative paths
are resolved from wherever you typed the command. Both flags mix freely,
so the long thing comes from a file and the short settings stay literal:

```bash
atelier run my-review --input-file brief=SPEC.md --input tone=blunt
```

Use `atelier show <conduit> --json` to see which keys a conduit accepts.
A key can be given by `--input` **or** `--input-file`, never both: a
repeated key is a usage error rather than one value silently winning. A
missing file, a directory, non-UTF-8 bytes or a malformed pair fails
before the run starts, so nothing is recorded. `-` is rejected too —
stdin stays free for the questions a `tool:hitl` step asks you.

The loaded text is saved with the run like any other input, not a
reference to the file. `atelier run --again <flow_id>` therefore replays
the text the run actually used even if you have since edited or deleted
the source file; pass `--input-file` again to feed it a new version.
`--resume` is refused with `--input-file`, because resuming continues a
flow's saved inputs rather than starting a new run.

File contents are ordinary input values: a conduit that drops an input
into a `tool:bash` command interpolates it the same way it interpolates
anything else. `--input-file` is a convenience for passing documents, not
a sandbox and not secret storage.

### Writing a conduit with your editor's help

`atelier schema` prints the JSON Schema of a `conduit.yaml`, generated
from the models the version you have installed actually loads. Save it
next to your workflows and an editor with a YAML language server will
complete the field names and underline the mistakes as you type:

```bash
atelier init
atelier schema > .atelier/conduit.schema.json
```

Then make this the first line of `.atelier/conduits/hello/conduit.yaml`:

```yaml
# yaml-language-server: $schema=../../conduit.schema.json
```

The path is relative to the conduit file, so `../../` lands on
`.atelier/`. You need an editor with the YAML language server for this —
the VS Code **YAML** extension, or the same server through your own
LSP client. Nothing is installed for you and nothing is sent anywhere.

Now break something on purpose:

```yaml
# yaml-language-server: $schema=../../conduit.schema.json
name: hello
description: Say hello
max_concurrency: 0
```

The editor marks `0` with *Value is below the minimum of 1* before you
run anything. Delete the line and the mark goes; type `re` inside a task
body and it offers `repeat`, `retries`, `retry_backoff`, `until`,
`while` and the rest. Both shorthands are covered, so `name: Who to
greet` under `inputs:` and `- greet:` with the body indented under it
are as valid to the editor as the long forms. Then the usual sequence:

```bash
atelier check hello
atelier run hello --input name=world
atelier outputs latest --task greet
```

A coding agent does not need the file at all — `atelier schema` on its
own is the whole vocabulary, from the version that is installed.

**It checks shape, not meaning.** A missing `tool`, a `tasks:` that is
not a list, a `max_concurrency: 0` — yes. A `depends_on` naming a task
that does not exist, two tasks with the same name, a loop predicate that
will not parse, a template that resolves to nothing, an agent you never
installed — no. `atelier check <name>` still owns all of that, and it is
still the thing to run before a real workflow. Regenerate the file after
upgrading Atelier; the schema describes the version that wrote it.

### A field Atelier does not know is an error

A `conduit.yaml` may only use the fields Atelier defines. A misspelled
one used to be dropped in silence, which is the worst possible outcome:
the file checks, plans and runs, but not as the workflow you wrote.

```yaml
name: typo_demo
description: two tasks that are meant to run in order
tasks:
  - name: prepare
    description: write the sentinel
    task: "printf 'prepared\n' > sentinel.txt"
    tool: tool:bash
  - name: consume
    description: read the sentinel back
    task: "cat sentinel.txt"
    tool: tool:bash
    depend_on: [prepare]      # typo: the field is depends_on
```

Before, `consume` loaded with no dependencies at all and raced `prepare`
for a file that did not exist yet. Now:

```bash
atelier check typo_demo
```

```
typo_demo [project] — FAIL: tasks[1].depend_on: Extra inputs are not permitted
```

Correct it to `depends_on:` and the same file checks, plans `consume`
into the second wave, and runs. The API rejects the same fields, so the
designer and a coding agent posting JSON get the identical answer.

**If you kept your own notes inside a conduit:** anything Atelier does
not define was already being discarded on load, so it never reached a
run — but it is now an error rather than a silent drop. Move it to a
YAML comment or a file beside the conduit. And if you exported
`conduit.schema.json` before upgrading, run `atelier schema` again:
the old copy still accepts the typo your editor should now be
underlining.

### Checking conduits from a script or an agent

`atelier check --json` answers the same question as `atelier check`, in a
form a program can act on. It writes one array to stdout, one object per
conduit it checked, and exits 1 if any of them failed:

```json
[
  {
    "name": "hello",
    "source": "project",
    "path": "/home/you/project/.atelier/conduits/hello/conduit.yaml",
    "ok": true,
    "error": null,
    "required_inputs": ["name"]
  }
]
```

- `path` is the file to open to fix the problem — the copy that would
  actually run, so a broken project conduit is reported instead of the
  working global one it shadows. It is `null` only when the path could
  not be resolved at all.
- `ok` is the verdict. Branch on it; the wording of `error` is for a
  human to read and may change.
- `required_inputs` is the `--input` keys a run needs, and only appears
  when the check passed — a conduit that failed to load has no
  trustworthy input list, so it is `null`, never `[]`.

Exit status and stdout carry different information, so read both:

```bash
atelier check --json > check-report.json   # exit 1 when a conduit failed
status=$?

python3 - <<'PY'
import json
for row in json.load(open("check-report.json")):
    if not row["ok"]:
        print(row["path"], "->", row["error"])
PY
exit $status
```

Exit 1 with a report on stdout means "checked everything, some failed".
Exit 1 with *empty* stdout means the check never started — an unknown
conduit name, or a store that could not be read — explained on stderr.
An empty `[]` with exit 0 means no conduits are installed here, which is
not proof that anything was validated.

With no name it checks every conduit **including the global ones** in
`~/.atelier/conduits/`, so a report can name a file outside this project.
Results depend on this machine: a conduit that needs an agent CLI you
have not installed fails here and passes where it is installed. Readiness
means the tool is available, not that it is logged in or that the run
will succeed.

So a coding agent can repair a workflow without a human reading the
terminal: `atelier schema` for the vocabulary, write the YAML,
`atelier check <name> --json`, open the `path` it returns, fix the
`error`, check again, and run it once `ok` is true. Nothing runs during a
check — no task, no agent session, no recorded flow.

### Checking a workflow that calls other workflows

A `tool:conduit` step runs another conduit by name, and that child can call
one of its own. Plain `atelier check` stops at the conduit you named: a child
is only loaded once the run reaches that step, so a missing or broken child
surfaces *after* the earlier steps have already done their work.
`atelier check <name> --recursive` follows those calls first.

Build a two-level workflow in a throwaway directory — a shell step that
prepares something, then a call to a `summary` conduit that does not exist
yet:

```bash
workspace="$(mktemp -d)/composed demo"
mkdir -p "$workspace/.atelier/conduits/report" && cd "$workspace"

cat > .atelier/conduits/report/conduit.yaml <<'YAML'
name: report
description: Prepare a measurement, then hand it to the summary conduit
tasks:
  - name: prepare
    description: record that preparation happened, and measure something
    task: "echo prepared >> preparation.log && echo 42"
    tool: tool:bash
    depends_on: []
  - name: summarise
    description: turn the measurement into a summary
    task: summary
    tool: tool:conduit
    depends_on: [prepare]
    inputs:
      finding: "{{prepare.output}}"
YAML
```

The parent file itself is fine, so the ordinary check passes and the
recursive one does not:

```bash
atelier check report              # OK - nothing is wrong with this file
atelier check report --recursive  # FAIL, exit 1 - `summary` is missing
```

The failure names the step that makes the call and the name it could not
resolve:

```
report [project] — FAIL: report.summarise -> summary — conduit not found
```

Neither check ran anything: there is no `preparation.log` and no recorded
flow. Write the child, then gate the run on a passing recursive check:

```bash
mkdir -p .atelier/conduits/summary
cat > .atelier/conduits/summary/conduit.yaml <<'YAML'
name: summary
description: Write a one-line summary of a finding
inputs:
  finding:
    description: what the caller measured
tasks:
  - name: write
    description: write the summary line
    task: "echo summary of {{inputs.finding}}"
    tool: tool:bash
    depends_on: []
YAML

atelier check report --recursive \
  && atelier run report \
  && flow_id=$(atelier wait latest --timeout 60) \
  && atelier outputs "$flow_id" --task summarise
```

The last line prints `summary of 42` — the child's result, read back from
the parent's saved run — and `preparation.log` holds exactly one line,
because the two failed checks never executed a step.

#### The call's arguments, not only the child's name

A called conduit receives exactly what its calling task forwards under
`inputs:`. It does **not** inherit the caller's inputs, so that one map is
the whole interface between the two files — and `--recursive` checks it.

Extend the same example: give `summary` a second input with a default, and
let the caller misspell it.

```yaml
name: summary
description: Write a one-line summary of a finding
inputs:
  finding:
    description: what the caller measured
  tone:
    description: how the summary should read
    default: calm
tasks:
  - name: write
    description: write the summary line
    task: "echo {{inputs.tone}} summary of {{inputs.finding}}"
    tool: tool:bash
    depends_on: []
```

```yaml
name: report
description: Prepare a measurement, then hand it to the summary conduit
tasks:
  - name: prepare
    description: record that preparation happened, and measure something
    task: "echo prepared >> preparation.log && echo 42"
    tool: tool:bash
    depends_on: []
  - name: summarise
    description: turn the measurement into a summary
    task: summary
    tool: tool:conduit
    depends_on: [prepare]
    inputs:
      finding: "{{prepare.output}}"
      tonee: direct
```

The engine drops a key the child cannot use, so this run would have
succeeded and written a `calm` summary — the default — while the author
believed they had asked for `direct`. `atelier check report --recursive`
fails instead, on one line naming the call, the child's file, the bad key
and the nearest one it does know:

```
report [project] — FAIL: report.summarise -> summary (/tmp/demo/.atelier/conduits/summary/conduit.yaml) — task 'summarise' has unknown inputs: ['tonee'] (did you mean 'tone' for 'tonee'?); 'summary' accepts inputs: ['finding', 'tone']
```

Rename `tonee` to `tone` and the check passes. Delete the `finding:` line
from the corrected file and it fails again, because `finding` has no
default and nothing else can supply it:

```
report [project] — FAIL: report.summarise -> summary (...) — task 'summarise' supplies no value for required inputs: ['finding']; add them under the task's own 'inputs:' map, which is all a called conduit receives
```

Both used to be found only by running the workflow — the second after
`prepare` had already appended to `preparation.log`, the first not at all.

What the flag does and does not tell you:

- The report still has one row per conduit you selected, with the same
  `--json` keys. A nested failure sets that row's `ok` to false and puts the
  calling chain, the child's file and the real diagnostic into its `error`.
- `required_inputs` stays the **root's** inputs. `summary` declares `finding`
  with no default, but the calling task supplies it, so `atelier run report`
  needs no `--input`.
- Binding **names** are checked: a forwarded key the child can neither
  declare nor reference, and a child input declared with no default that the
  call leaves out, both fail. Binding **values** are not. A `{{...}}` you
  forward is accepted without being resolved, and a key the child only
  *references* — supplied at run time by a loop, a human answer or an
  upstream output — is neither required nor rejected here.
- Every `tool:conduit` step is inspected, including one a condition would
  skip at runtime.
- A target assembled from a template (`task: "{{inputs.which}}"`) cannot be
  resolved without running the workflow, so `--recursive` fails and says so.
  Check that child by its real name instead, or leave the flag off.
- A conduit that calls itself, or a loop between two conduits, is reported
  as a cycle instead of recursing — as is a chain deeper than the engine's
  nesting limit.
- Passing still only means "these definitions load and their tools are on
  this machine". It is not a promise that the run will succeed.

### Waiting for a run from another terminal or agent

A run started in another terminal, by the dashboard, or by the scheduler is
an ordinary saved flow, so a second session can join it and use its results:

```bash
flow_id=$(atelier wait latest --timeout 60) && atelier outputs "$flow_id" --json
```

`atelier wait` watches one run's saved progress and turns the outcome into an
exit status, so nothing has to poll, re-read `status --json`, or scrape the
terminal:

| exit | what it means                                                                   |
| ---- | ------------------------------------------------------------------------------- |
| 0    | the run saved `completed`; stdout holds the resolved flow id and nothing else     |
| 1    | it failed, was stopped, its runner died, or its progress could not be read        |
| 124  | the timeout expired while it was still running                                    |
| 130  | you pressed Ctrl-C                                                                |
| 2    | `--timeout` was not a positive number of seconds                                  |

Success prints the id it resolved, and the next command should use that id.
Resolving `latest` a second time can land on a newer run that started while
you were waiting.

Waiting only watches. A timeout or a Ctrl-C ends your *observation*, not the
run: the other process keeps going and `atelier status <flow_id>` still finds
it. `wait` never starts, stops, resumes or edits anything. A run paused on a
human gate (`tool:hitl`) counts as still running, so it times out rather than
failing.

Failures name the state and point at `atelier status` and `atelier logs`; a
run whose runner died also suggests `atelier run --resume`.

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
`atelier run` rejects an `--input` or `--input-file` key the conduit
neither declares nor references, so a mistyped key fails before the run
starts.

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

Tool permission requests are automatically approved by default. Add a
conduit-level `interaction` policy to choose human or supervisor decisions.

Non-interactive tasks run one turn and stop.

For a direct interactive agent conversation without writing a conduit,
use `atelier ask` (Claude Code by default; `--harness <name>` picks
another agent from `atelier list harnesses`):

```bash
atelier ask "Help me write a specification" --path /absolute/path/to/project
```

`--path` is required and becomes Claude's working directory. Claude's
questions are read from stdin just like any other interactive harness task.
The resulting flow is still recorded under the `.atelier/flows/` directory
from which you invoked `atelier`.

#### Conduit-wide human and supervisor policies

Choose the behavior once for every harness task in a conduit:

```yaml
interaction:
  questions: hybrid       # human | supervisor | hybrid
  permissions: approve_all # approve_all | human | supervisor | hybrid
  supervisor:
    tool: harness:codex
    instructions: |
      Follow existing project conventions.
      Escalate scope changes and unspecified product preferences.
```

`human` always asks you. `supervisor` always delegates and fails if the
supervisor cannot decide. `hybrid` lets it answer or escalate to you.
`approve_all` automatically allows tool permissions. The supervisor receives
the full worker session exposed through ACP, including prior replies and tool
activity. Answers are attributed and recorded.

Adding `interaction` enables conversations for all harness tasks. Omitting it
preserves existing task-level `interactive` behavior and automatic permissions.
The designer exposes these controls in the conduit panel. Explicit `tool:hitl`
gates remain human; nested conduits use their own policy.

See [interaction policies](docs/interaction.md) for limits, failure handling,
permission boundaries, and the complete configuration.

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
atelier create <name> [--description <text>] [--template hello|code-review]
                                                       # scaffold a starter conduit
atelier check [<conduit>] [--json] [--recursive]        # validate conduit(s) without running
                                                       # --recursive also checks the conduits they call
atelier plan <conduit> [--json]                        # print the DAG as ordered waves, run nothing
atelier show <conduit> [--json]                        # print its definition and inputs, run nothing
atelier schema                                         # print the conduit.yaml JSON Schema for your editor

# <flow_id> below accepts a unique prefix, or 'latest' for the most recently started flow
# running
atelier run <conduit> [--input key=value ...] [--input-file key=path ...]
                      [--show-steps/--hide-steps]
                                                       # --input-file loads a UTF-8 text file into one input
atelier ask <query> --path <directory> [--harness <name>]   # interactive agent session (default: claude-code)
atelier run --resume <flow_id>                         # resume a failed/crashed flow
atelier run --again <flow_id>                          # fresh run reusing a past flow's inputs
atelier stop <flow_id>                                 # gracefully halt a running flow

# inspecting
atelier status <flow_id>
atelier wait <flow_id> [--timeout 60]                  # block until it finishes; exit 0 only if it did
atelier logs <flow_id> [--task <name>] [--follow] [--json]
atelier outputs <flow_id> [--task <name>] [--json]    # read back a finished flow's results
atelier timing <flow_id> [--json]                      # per-task duration, slowest first
atelier list conduits
atelier list flows [--conduit <name>]
atelier list schedules [--json]
atelier list harnesses [--ready] [--json]
atelier rm <flow_id> [--force] [--yes]                 # delete one flow run
atelier prune [--conduit <name>] [--older-than <days>] [--keep <n>]   # bulk-delete old flows

# sharing conduits (see "Installing conduit packages" below)
atelier install <source> [--ref <git-ref>] [--project] [--force]
atelier update <package>                               # re-fetch and re-install from source
atelier uninstall <package>                            # delete a package's conduits

# scheduling
atelier schedule add <file.{json,yaml}>
atelier schedule rm <id-or-name>
atelier schedule run-now <id-or-name>
atelier schedule history <id-or-name>
atelier schedule daemon [--reload-interval 30] [--log-level INFO]

# HTTP + WebSocket server
atelier serve [--host 127.0.0.1] [--port 8000] \
              [--reload-interval 30] [--cors-origin URL]* \
              [--log-level INFO]

# maintenance
atelier self-update                                    # prebuilt binary only; uv installs use `uv tool upgrade`
```



## Installing conduit packages

A conduit is just a folder, so conduits are shareable. `atelier install`
installs them from a git repo or a local path:

```bash
atelier install owner/repo                  # GitHub shorthand
atelier install https://github.com/owner/repo.git
atelier install ./some/local/package
atelier install owner/repo --ref v1.2.0     # pin a branch, tag, or commit
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
re-installs. `atelier uninstall <package>` deletes only the conduits that
install actually wrote, so a conduit that was skipped on collision is
left alone.

## Running on a schedule

`atelier schedule daemon` runs conduits on a wall-clock schedule. Each
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
| `GET`    | `/flows/:id`          | Tasks, dependencies and progress          |
| `GET`    | `/flows/:id/tasks/:task/log` | One task's rounds and actions, secrets masked |
| `WS`     | `/ws/run-conduit`     | Run flows, HITL + interactive AI turns    |


Every run also has a page at `/runs/<flow_id>`: a map of its tasks with the
ones running now framed, and the log of whichever task you click. It works for
runs started from the CLI, the dashboard or the scheduler, and refreshes while
the run is going. Shell output appears line by line as it is printed.

Binds to `127.0.0.1:8000` by default; pass `--host 0.0.0.0` to expose
on the LAN — which requires `ATELIER_API_TOKEN`, see [Security](#security).
`--cors-origin` is repeatable.

Conduits and flows resolve exactly as they do on the CLI — `./.atelier`
first, then `~/.atelier` — so the conduits `atelier init` created in the
directory you started the server from are the ones the UI shows.
Schedules are the exception: they live in `~/.atelier/schedules/`, since
one daemon serves every project. Like `atelier run --input`, a `run`
envelope on `/ws/run-conduit` whose `inputs` carry a key the conduit
neither declares nor references comes back as `flow_failed` naming the
key and its closest match, instead of starting the flow.

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
