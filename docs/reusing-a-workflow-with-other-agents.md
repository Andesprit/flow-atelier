# Running one workflow with different agents

A conduit names an agent per task. That name is a default, not a verdict:
`--agent <task>=<harness>` runs one task on another agent for one invocation,
and the file on disk is never touched. Useful when you are rate-limited on one
provider, when you want a second opinion from a different model, or when an
agent you were logged into this morning is logged out now.

This exercise takes one composed recipe and runs it three ways — your choice,
a recovery after a bad choice, and a fresh run that swaps both agents. Nothing
here edits the recipe.

You should have done [Orchestrating several agents in one
workflow](multi-agent-workflow.md) first; this picks up where it stops.

## Before you start

Two agents you are already logged into. The guide uses `claude-code` and
`codex`; any two names from `atelier list harnesses` work.

```bash
atelier --version || exit 1
atelier harness check claude-code || exit 1
atelier harness check codex || exit 1
```

`harness check` opens an ACP session and closes it without sending a prompt,
so it costs no tokens. Every block below ends its lines in `|| exit 1`: paste
them into a script and a failed prerequisite stops there rather than halfway
through a run.

## 1. A disposable workspace

```bash
work_dir="$(mktemp -d)/agent choices" || exit 1
mkdir -p "$work_dir" || exit 1
cd "$work_dir" || exit 1
echo "working in: $work_dir"
```

The space in the folder name is deliberate; real project paths have them.

## 2. A recipe you are not going to edit

```bash
atelier compose triage \
  -s "claude-code=Propose one concrete fix. Say what you would change and why." \
  -s "codex=Review the proposal above. Name what breaks, or say it is sound." \
  || exit 1
cp ".atelier/conduits/triage/conduit.yaml" "$work_dir/recipe.snapshot" || exit 1
```

Two agent tasks, `step_1` on `claude-code` and `step_2` on `codex`. The copy is
only so this guide can prove, at the end, that nothing rewrote the file.

## 3. See who would get what, before spending anything

```bash
atelier plan triage || exit 1
atelier plan triage --agent step_2=claude-code || exit 1
```

The first `plan` shows the recipe: `step_1 [harness:claude-code]`, then
`step_2 [harness:codex]`. The second shows what `run` would do with the same
flag — `step_2 [harness:claude-code]`, marked `⇄ --agent (recipe:
harness:codex)` so the preview can never be mistaken for the file.

`plan` starts no agent, creates no flow and writes nothing. `atelier plan
triage --json --agent step_2=claude-code` answers the same question for a
script: each task carries its effective `tool` and, when replaced, the
`recipe_tool` it came from.

## 4. Run it with your choice

```bash
brief="The nightly billing job is late. Constraint: budget=0." || exit 1
atelier run triage --agent step_2=claude-code --input brief="$brief" \
  > both-on-claude.log 2>&1 || { cat both-on-claude.log; exit 1; }
cat both-on-claude.log || exit 1
flow_id="$(sed -n 's/^flow_id: //p' both-on-claude.log)" || exit 1
[ -n "$flow_id" ] || exit 1
echo "first run: $flow_id"
```

Both steps ran on Claude Code. The handoff is unchanged: `step_2` still waits
for `step_1` and still receives its result, quoted under a `RESULT FROM step_1`
marker — and that marker names the agent that actually produced the result, so
a replacement never leaves the next agent reading the wrong attribution.

The run's last line is its flow id. Keeping it is what lets every command below
address this exact run instead of resolving `latest` again.

## 5. Read the run back

```bash
atelier status "$flow_id" || exit 1
atelier status "$flow_id" --json || exit 1
atelier outputs "$flow_id" --task step_2 || exit 1
```

`status` grows an `agent` column listing the choices this run was launched
with, and says in one line that the conduit file is unchanged. `status --json`
carries the same thing as `task_agents`, so a script can read which agent
produced which output. A run started without `--agent` has no such column and
no such key.

## 6. When the choice was wrong

A pinned model the agent does not offer is refused when the session opens —
after the step before it has already finished. This block makes that happen on
purpose, so the recovery is real rather than described:

```bash
atelier run triage --agent step_2="codex:no-such-model-xyz" \
  --input brief="$brief" > bad-model.log 2>&1
cat bad-model.log || exit 1
broken_id="$(sed -n 's/^flow_id: //p' bad-model.log)" || exit 1
[ -n "$broken_id" ] || exit 1
atelier status "$broken_id" || exit 1
```

`step_1` is `completed` and `step_2` is `failed`. The saved agent choice is
still there, which is what makes the next command short:

```bash
atelier run --resume "$broken_id" --agent step_2=codex || exit 1
atelier status "$broken_id" || exit 1
atelier outputs "$broken_id" --task step_2 || exit 1
```

`--resume` with no `--agent` would have reused the saved choice — the bad model
included — and failed the same way. Naming the task replaces that one
assignment and leaves the rest of the run alone: `step_1` is not asked again,
and the replacement is handed the brief and the result `step_1` already
produced.

Only a step that is `pending` or `failed` can be re-pointed. Asking to change
the agent of a step that already completed is refused before anything on disk
moves, because its output is already in the prompts downstream of it — use
`--again` for that, below. And switching tools does not undo a file an agent
already edited: recovery is about the work still to do.

## 7. Do it again, the other way round

```bash
atelier run --again "$broken_id" \
  --agent step_1=codex --agent step_2=claude-code \
  > swapped.log 2>&1 || { cat swapped.log; exit 1; }
cat swapped.log || exit 1
swapped_id="$(sed -n 's/^flow_id: //p' swapped.log)" || exit 1
echo "swapped: $swapped_id"
atelier status "$swapped_id" --json || exit 1
cmp ".atelier/conduits/triage/conduit.yaml" "$work_dir/recipe.snapshot" || exit 1
echo "the recipe is byte-identical after three runs"
```

`--again` starts a fresh flow with its own id, reusing the source run's saved
inputs — the brief is not retyped — and its saved agent choices, which the two
`--agent` flags then override. The source run is untouched: it keeps its own
status, outputs and assignment record.

The final `cmp` is the point of the whole exercise. Three runs, four different
agent assignments, one unchanged file.

## What a selection can and cannot do

* It names **top-level tasks of the recipe you are running**. A task of the
  same name inside a nested `tool:conduit` is not affected — that child is read
  from the store on its own.
* It re-points **agent tasks only**. A `tool:bash`, `tool:hitl` or
  `tool:conduit` step is refused, with its name, rather than converted; change
  those by editing the conduit.
* The harness is any name `atelier list harnesses` shows, with or without the
  `harness:` prefix, and may pin a model or a model and an effort:
  `--agent step_2=codex`, `--agent step_2=codex:gpt-5.1`,
  `--agent step_2=codex:gpt-5.1:high`. `atelier harness check codex` lists the
  models that agent offers.
* The readiness gate probes what you chose, not what the recipe says. Replacing
  an agent you cannot run is enough to start the run; naming a replacement you
  cannot run stops it before the first prompt.
* A recipe written before this existed still records a static agent name in the
  markers `atelier compose` used to generate. Those are the recipe's defaults.
  What actually ran is in `atelier status --json` under `task_agents`, and in
  `atelier plan --agent`, for every run.
