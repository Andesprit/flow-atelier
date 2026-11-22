# Authoring conduits

## Reading a conduit before you run it

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

## Passing a file as an input

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

File contents are ordinary input values. A `tool:bash` step should read
them from `"$INPUT_<NAME>"` or the file at `"$INPUT_<NAME>_FILE"`, not
from `{{inputs.<name>}}` in the command (see
[Values in shell steps](../reference/conduit-yaml.md#values-in-shell-steps)). `--input-file` is a
convenience for passing documents, not a sandbox and not secret storage.

## Writing a conduit with your editor's help

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
      task: echo "hello $INPUT_NAME"
      tool: tool:bash
      depends_on: []
```

Run it with `atelier run hello --input name=world`. The shell reads the input
from the `INPUT_NAME` environment variable; see
[Values in shell steps](../reference/conduit-yaml.md#values-in-shell-steps) for why.

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
      task: 'git clone -b "$INPUT_BRANCH" "$INPUT_REPO_URL" /tmp/build'
      tool: tool:bash
      depends_on: []

  - run_tests:
      description: Run tests
      task: "cd /tmp/build && make test"
      tool: tool:bash
      depends_on: [clone_repo]
      repeat: 3                          # try up to 3 times
      until: output.match(PASS)        # ...stopping early on success
      on_exhaust: fail                 # stop the run if no try passed

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
      depends_on:
        - "approve.output.match((?m)^confirm: 'yes'$)"
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
- `run_tests` runs `make test` up to 3 times, stopping as soon as the
output contains `PASS`. After 3 runs without `PASS` the pipeline stops, so
nothing is deployed after red tests.
- `code_review` asks Claude Code to review the code and end with
either `VERDICT: APPROVE` or `VERDICT: REJECT`.
- `approve` only runs if Claude approved (`...match(VERDICT:\s*APPROVE)`).
It asks the human two typed questions on the terminal.
- `deploy` only runs when the human types `yes` at `confirm`, and calls
another conduit (`deploy_to_env`) as a nested run. Any other answer, even an
empty one, skips it. A plain `depends_on: [approve]` would not wait for a
yes: a gate finishes once it has an answer, whatever the answer is.
- `rollback` only runs if Claude rejected. The two branches are
mutually exclusive — the unmet branch is silently skipped, not
failed.

## Waiting for a run from another terminal or agent

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

See [Validating conduits](validation.md).
