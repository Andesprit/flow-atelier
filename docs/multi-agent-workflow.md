# Orchestrating several agents in one workflow

A 5-minute exercise that builds two multi-agent workflows from the command
line — a handoff chain and a review panel with a synthesis step — runs both,
and reads their saved results back by flow id. You never write YAML by hand,
and what you get is still an ordinary conduit you can open and edit.

`atelier compose` is the only new command here. Everything after it is the
workflow surface Atelier already has: `check`, `plan`, `run`, `outputs`,
`--resume`.

New to Atelier? Run the single-agent
[first workflow](first-workflow.md) first; it needs no AI account at all.

## Before you start

Two agents you are already logged into. The guide uses `claude-code` and
`codex`; any two names from `atelier list harnesses` work, and you can swap
them in every command below.

Each prerequisite is checked explicitly, so a missing or logged-out agent
stops here instead of halfway through a run:

```bash
atelier --version || exit 1
atelier harness check claude-code || exit 1
atelier harness check codex || exit 1
```

`harness check` opens an ACP session and closes it. It sends no prompt, so it
costs no tokens.

The blocks below are Bash — macOS, Linux, WSL or Git Bash. They quote every
path, and they all end in `|| exit 1`, so you can paste them into a script
without adding error handling of your own.

## 1. A disposable workspace

```bash
work_dir="$(mktemp -d)/agent handoff" || exit 1
mkdir -p "$work_dir" || exit 1
cd "$work_dir" || exit 1
echo "working in: $work_dir"
```

The space in the folder name is deliberate; real project paths have them.

## 2. Compose a handoff

One `--step` per agent, in the order you want them to work. Only the first
`=` separates the agent from its instruction, so the prompt keeps its own:

```bash
atelier compose triage \
  -s "claude-code=Propose one concrete fix. Say what you would change and why." \
  -s "codex=Review the proposal above. Name what breaks, or say it is sound." \
  || exit 1
```

That wrote `.atelier/conduits/triage/conduit.yaml` and nothing else. No agent
started, no run was created.

## 3. Read it before you run it

```bash
cat ".atelier/conduits/triage/conduit.yaml" || exit 1
atelier check triage --json || exit 1
atelier plan triage || exit 1
```

Two tasks, `step_1` and `step_2`. Both receive the conduit's required `brief`
input; `step_2` also receives `step_1`'s result, quoted under a
`RESULT FROM step_1 (harness:claude-code)` marker so the second agent can tell
the material apart from its instruction. `step_2` lists `step_1` under
`depends_on`, which is what makes the handoff an order and not a hope.

`check --json` reports `"required_inputs": ["brief"]`. `plan` prints the two
waves and runs nothing.

The file is yours from here. Add a `tool:bash` step, a `tool:hitl` approval,
a `timeout`, a `retries` — `compose` writes the first draft, it does not own
the result.

## 4. Run it

```bash
atelier run triage --input brief="The nightly billing job is late. Constraint: budget=0." || exit 1
```

The brief reaches both agents. The second agent starts only once the first has
finished, because it cannot be handed a result that does not exist yet.

## 5. Read the exact result back

```bash
flow_id=$(atelier wait latest --timeout 5) || exit 1
atelier outputs "$flow_id" --task step_1 || exit 1
atelier outputs "$flow_id" --task step_2 || exit 1
```

`atelier wait` prints only the resolved flow id, so the next command addresses
one exact run instead of resolving `latest` again and possibly landing on a
newer one. Every prompt and every reply stays on disk under that id:
`atelier logs "$flow_id"` shows them tomorrow.

## 6. A panel instead of a chain

`--parallel` cuts the links between the steps: every agent gets the brief and
nothing else, and they run at the same time. `--synthesize` adds one final
`synthesis` task that depends on all of them and receives each result labelled
with the step and the agent that produced it:

```bash
atelier compose panel --parallel \
  -s "claude-code=Review the brief for correctness risks." \
  -s "codex=Review the brief for security risks." \
  --synthesize "claude-code=Merge the two reviews into one ranked list." \
  || exit 1
atelier plan panel || exit 1
```

`plan` shows it: one wave with both reviewers, then a wave with `synthesis`.

```bash
atelier run panel --input brief="The nightly billing job is late. Constraint: budget=0." || exit 1
panel_id=$(atelier wait latest --timeout 5) || exit 1
atelier outputs "$panel_id" --task synthesis || exit 1
```

The reviewers work from the same brief and never see each other's answers;
only the synthesizer sees both.

One honest limit: parallel agents run in the **same directory**. The composed
prompts ask for analysis, and nothing stops an agent from editing files
anyway — that is a prompt, not a sandbox. Give writing agents separate
worktrees, or keep the parallel shape for review and analysis.

## When an agent fails

Nothing special: a composed conduit fails and recovers like any other. The
failing step is recorded, everything downstream of it is cancelled rather than
run on a missing result, and the run exits non-zero.

```text
# after fixing whatever failed — a login, a rate limit, a prompt
atelier run --resume "$flow_id"
```

`--resume` finishes that same flow. Steps that already completed are not asked
again, so a failure in the last agent of a five-agent chain costs you one
agent, not five. The mechanics, and the difference from `--again`, are covered
in the [first workflow](first-workflow.md#7-repair-then-resume).

## Choosing agents, models and effort

A step names any harness `atelier list harnesses` shows, with or without the
`harness:` prefix, and may pin a model or a model and a reasoning effort:

```text
-s "codex=..."                       the agent's own default model
-s "codex:gpt-5.1=..."               that model
-s "codex:gpt-5.1:high=..."          that model at that effort
```

`atelier harness check codex` lists the models the agent offers, and
`atelier harness check codex:<model>` the efforts that model offers. To ask
every agent of one workflow at once instead of naming them yourself, see
[Checking the whole agent team before a
run](checking-the-team-before-a-run.md).

The name a step carries is its default, not a lock: `atelier run <name>
--agent <task>=<harness>` runs one task on another agent without touching the
file. [Running one workflow with different
agents](reusing-a-workflow-with-other-agents.md) walks that path — preview,
run, read back, recover a bad choice, and re-run with the agents swapped.

A prompt may not contain `{{...}}`. Composed prompts reach the agent as you
typed them — only the whitespace around the prompt is trimmed — so a template
reference is refused when you compose rather than quietly reinterpreted at run
time. If you want one, write it into the conduit.yaml afterwards — it is a
normal file.
