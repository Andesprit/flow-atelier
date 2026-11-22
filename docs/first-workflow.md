# Your first workflow

## Ask one agent

From a project with Codex installed and logged in, give the agent a skill it
has, a goal and an acceptance check. For example, if it has a code-review
skill, ask for one finding you can verify yourself.

```bash
atelier ask "Use your code-review skill on the last commit. Give one regression with file, line and a way to reproduce it, or say no finding." --path . --harness codex --permissions read-only
```

Enter submits a reply. Alt+Enter, or Escape then Enter, inserts a newline.
Atelier records the conversation as a flow. If the adapter cannot enforce
read-only access under the default policy, the task fails before the prompt.

### Call from a script

The Bash examples that read JSON use Python 3. This call returns after one
agent turn, with a 60-second timeout. Keep stdout and stderr separate.

```bash
atelier ask "Review the last commit. Ask which caller to focus on if needed." \
  --path . --harness codex --permissions read-only --timeout 60 --json > turn.json
flow_id=$(python3 -c 'import json; print(json.load(open("turn.json"))["flow_id"])')
atelier ask "$flow_id" --json
```

A completed turn has an `answer`. For `needs_input`, read the `question`
and answer that specific request. Answering requires an adapter that can
reopen the original session and still report an enforceable read-only mode.

```bash
request_id=$(python3 -c 'import json; print(json.load(open("turn.json"))["request_id"])')
atelier ask "$flow_id" --answer "Focus on the CLI caller." --request-id "$request_id" --json
```

To end a pending question instead, run this from the same project.

```bash
atelier ask "$flow_id" --cancel --json
```

### Save the assignment

For repeated or scheduled work, compose a named, non-interactive task.
`brief` varies on each run. `check` reports advisory warnings, including
serial handoffs that could serve one outcome and loop checks based on agent
prose. Read the warnings even when validation succeeds.

```bash
atelier compose commit-review --permissions read-only \
  --step "codex=Use your code-review skill. Give one finding with file, line and reproduction, or say no finding."
atelier check commit-review
atelier run commit-review --input brief="Review the last commit." --json > run.json
flow_id=$(python3 -c 'import json; print(json.load(open("run.json"))["flow_id"])')
atelier outputs "$flow_id" --task step_1
atelier logs "$flow_id" --jsonl
```

`run --json` writes one completion record such as
`{"flow_id":"…","status":"completed"}`. A failed allocated run also returns
its ID and status, with a nonzero exit. A command error before a result exists
leaves stdout empty and writes a JSON object with `error.code` and
`error.message` to stderr. Logs in JSON Lines format have one record per
line. Add `--follow` to stream a running flow.

For example, deliberately omit the required `brief` to see a pre-run error.
Expect exit 2, an empty `result.json` and `missing_inputs` in `error.json`.

```bash
if atelier run commit-review --json > result.json 2> error.json; then
  echo "Expected a missing brief" >&2
  exit 1
fi
cat error.json
```

### Schedule it

To register a weekly review, run this once from the project you want reviewed.
The relative `run_path` is saved against that directory.

```bash
cat > weekly-review.yaml <<'YAML'
conduit_name: commit-review
inputs: {brief: "Review the last commit."}
run_path: .
schedule:
  name: weekly-commit-review
  mode: recurring
  days: [1]
  times: ["09:00"]
YAML
atelier schedule add weekly-review.yaml
atelier list schedules --json
```

Schedules live in the shared `~/.atelier/schedules/` store. Use one scheduler for that store and
`atelier serve --no-scheduler` for a dashboard that only watches.
[Schedule file and registration](guides/schedules.md)

## Try failure and recovery with Bash

This separate exercise needs only Bash and Python 3. In a fresh directory,
create a recipe that prepares once, checks a script, then executes it.

```bash
tutorial_dir="$(mktemp -d)/atelier tutorial"
mkdir -p "$tutorial_dir"
cd "$tutorial_dir"
mkdir -p .atelier/conduits/script-check
cat > .atelier/conduits/script-check/conduit.yaml <<'YAML'
name: script-check
description: Check a script before running it
tasks:
  - prepare:
      description: Count preparations
      tool: tool:bash
      task: echo prepared >> preparation.log
  - syntax:
      description: Check Bash syntax
      tool: tool:bash
      task: bash -n demo.sh
      depends_on: [prepare]
  - execute:
      description: Run the checked script
      tool: tool:bash
      task: bash demo.sh
      depends_on: [syntax]
YAML
echo 'echo "workflow works"' > demo.sh
atelier check script-check --json
atelier plan script-check
atelier run script-check
```

Break the script. Capture the failed run’s own ID, so another run starting
in the same directory cannot change which failure you diagnose.

```bash
echo 'if true; then' > demo.sh
if atelier run script-check --json > broken.json; then
  echo "Expected the syntax check to fail" >&2
  exit 1
fi
failed=$(python3 -c 'import json; print(json.load(open("broken.json"))["flow_id"])')
atelier diagnose "$failed"
atelier logs "$failed" --task syntax --show stderr
```

The syntax error cancels `execute`. Repair the file and resume. The two lines
in `preparation.log` show that the completed preparation was kept. Running
again allocates a new ID and adds a third line.

```bash
echo 'echo "workflow works"' > demo.sh
atelier run --resume "$failed"
cat preparation.log
atelier run --again "$failed"
cat preparation.log
```

Resume reads the current recipe and workspace. A task interrupted before
its result was saved can repeat its side effects. Use a fresh run when you
need to repeat a completed task.
