# Your first workflow: run it, break it, recover it

A 5-minute exercise that takes one three-step conduit all the way through its
real life cycle: a successful run, a deliberate failure, a diagnosis from saved
history, a repair that keeps the work already done, and a clean re-run from the
top. Everything runs locally with `bash`. No AI account, no API key, no Git
repository, and no network access after installing Atelier.

By the end you will have seen the four things that a single agent conversation
does not give you:

- a failure stops the steps downstream instead of running them on bad input,
- the diagnosis survives the terminal session and is addressable by flow id,
- `--resume` keeps the steps that already succeeded,
- `--again` starts a brand-new run with its own identity.

New to Atelier? Install it and run the `hello` conduit first — see the
[Quickstart](../README.md#quickstart).

## Before you start

- `atelier` on your `PATH` (`atelier --version` should answer).
- `bash`, plus `mkdir`, `cat`, `echo` and `mktemp`. macOS, Linux, or a Bash
  shell on Windows such as WSL or Git Bash. The blocks below are Bash; they are
  not PowerShell.

Everything happens inside one throwaway directory. Nothing is installed
globally, nothing in your existing projects is touched, and nothing is deleted
for you.

## 1. A disposable workspace

```bash
tutorial_dir="$(mktemp -d)/atelier tutorial"
mkdir -p "$tutorial_dir"
cd "$tutorial_dir"
echo "working in: $tutorial_dir"
```

Keep this shell open. Every later command assumes you are still in
`$tutorial_dir`, because the workflow calls a script that lives here.

The space in the folder name is deliberate. Real project paths have them, and
every command below quotes its paths so they keep working.

## 2. The conduit

A conduit is a YAML file describing tasks and what each one depends on. This
one has three:

```bash
mkdir -p .atelier/conduits/script-check
cat > .atelier/conduits/script-check/conduit.yaml <<'YAML'
name: script-check
description: Prepare, check and run a shell script
tasks:
  - prepare:
      description: record one preparation run
      task: "echo prepared >> preparation.log"
      tool: tool:bash
      depends_on: []
  - syntax:
      description: check demo.sh for syntax errors
      task: "bash -n demo.sh"
      tool: tool:bash
      depends_on: [prepare]
  - execute:
      description: run demo.sh
      task: "bash demo.sh"
      tool: tool:bash
      depends_on: [syntax]
YAML
```

`prepare` only appends a line to `preparation.log`. That file is the tutorial's
counter: it makes "did this step run again?" something you can read, instead of
something you have to trust. In a real project these three tasks become your
own setup, your own validation, and your own build or deploy command.

Now the script the workflow works on:

```bash
cat > demo.sh <<'SH'
echo "workflow works"
SH
```

## 3. Look before you run

```bash
atelier check script-check --json
atelier plan script-check
```

`check` answers "is this file valid and are its tools usable?" — you should see
`"ok": true`. `plan` prints the three waves in dependency order and runs
nothing.

Note what `check` does **not** do: it never opens `demo.sh`. It validates the
conduit and its tools, not the contents of every shell command or external
script they call. That is exactly the gap the `syntax` task exists to close.

## 4. The first run

```bash
atelier run script-check
```

The run happens in the foreground and prints a transcript: three tasks, all
green, `workflow works` in the `execute` panel, and a final `flow_id:` line.

That transcript is for you. To get the same result back in a form a script or a
coding agent can use, ask for the flow by id:

```bash
flow_id=$(atelier wait latest --timeout 5)
atelier outputs "$flow_id" --task execute
```

```text
workflow works
```

`atelier wait` prints **only** the resolved flow id on stdout, and exits
non-zero if the run did not complete. So `flow_id=$(atelier wait ...)` gives the
next command an exact run to address, instead of resolving `latest` a second
time and possibly landing on a newer one.

## 5. Break it on purpose

Now make `demo.sh` invalid — an `if` with no `fi`:

```bash
cat > demo.sh <<'SH'
if true; then
  echo "workflow works"
SH
```

The next run is **supposed to fail**. The block below expects that, and says so
loudly if the failure does not happen:

```bash
if atelier run script-check; then
  echo "UNEXPECTED: the broken script passed the syntax check"
else
  echo "the run failed on purpose; that is this step's expected result"
fi
```

In the transcript: `prepare` is green, `syntax` is red with Bash's own
complaint, and `execute` is `cancelled (upstream failed)`. `execute` never ran
the broken script — the dependency graph stopped it. That is the difference
between a workflow and a chain of `&&`: the failure is recorded as a state, not
just a non-zero exit that scrolls away.

## 6. Read the failure back

```bash
atelier status latest --json
atelier logs latest --task syntax --show stderr
cat preparation.log
```

`status --json` gives you the per-task verdict:

```text
"prepare": { "status": "completed" ... }
"syntax":  { "status": "failed", "reason": "exit=2 stderr=demo.sh: line 3: ..." }
"execute": { "status": "cancelled" ... }
```

`logs --task syntax --show stderr` prints Bash's diagnostic itself, saved on
disk: `demo.sh: line 3: syntax error: unexpected end of file`. Reopen this
terminal tomorrow and it is still there.

`preparation.log` now has **two** lines. Each `atelier run` is a new flow, so
`prepare` ran once for each.

## 7. Repair, then resume

Fix the script and continue the same run:

```bash
cat > demo.sh <<'SH'
echo "workflow works"
SH
atelier run --resume latest
cat preparation.log
```

Three things to notice:

- The transcript starts at `syntax`, not at `prepare`. Only two tasks run.
- `preparation.log` still has **two** lines. The completed work was kept.
- The final `flow_id:` is the **same id** as the failed run. This is that run,
  finishing — not a new one.

## 8. A fresh run with `--again`

```bash
recovered_flow_id=$(atelier wait latest --timeout 5)
atelier run --again "$recovered_flow_id"
cat preparation.log
```

This time everything runs, including `prepare`, so `preparation.log` reaches
**three** lines — and the final `flow_id:` is a new id, different from the one
you just recovered.

```bash
fresh_flow_id=$(atelier wait latest --timeout 5)
atelier outputs "$fresh_flow_id" --json
atelier list flows --conduit script-check
```

```text
{
  "prepare": "",
  "syntax": "",
  "execute": "workflow works\n"
}
```

Three flows are listed. The `prepare` counter went 1 → 2 → 2 → 3: one per new
flow, and unchanged by the resume. That single number is the whole distinction
between the two recovery commands.

## What `--resume` and `--again` actually do

**`--resume <flow_id>`** continues that same flow. It reuses the inputs saved
with the run, skips the tasks whose completion was already written to disk, and
re-reads the conduit and any files on disk **as they are now** — which is why
editing `demo.sh` was enough. It is not a snapshot or a checkout, and it is not
exactly-once: a task that finished but was killed before its result was saved
runs again. Use it when the earlier work is still good and expensive to repeat.

**`--again <flow_id>`** allocates a new flow and runs the conduit from the top,
reusing the source run's saved inputs (override individual ones with `--input`).
Use it when earlier results are stale and must be recomputed.

Both run in your **current directory**. A run started from the CLI does not pin
itself to the folder it began in, so resume and re-run from the same place, or
the scripts the tasks call will not be where they expect.

One honest limit of this exercise: it only edits `demo.sh`, an external file the
unfinished step reads. Changing the *definition* of a task that already
completed will not make `--resume` re-run it — that task is done as far as the
flow is concerned. Use `--again` for that.

## `latest` versus real flow ids

This walkthrough uses `latest` because its directory has exactly one run at a
time. `latest` means "the most recently started run in this project", so the
moment two runs overlap — a scheduler, a second terminal, an agent working in
parallel — it stops being the run you meant.

For anything beyond a tutorial, capture the id and use it:

```bash
flow_id=$(atelier wait latest --timeout 60) && atelier outputs "$flow_id" --json
```

`atelier status <id> --json` and `atelier list flows --json` both report
`flow_id`, so a coding agent can diagnose and recover a specific run without
parsing a transcript.

## Done

Remove the workspace whenever you like: `rm -rf "$tutorial_dir"`. Nothing
outside it was changed.

Next: swap the three tasks for your own commands, or add an AI step — see
[Your first AI workflow](../README.md#your-first-ai-workflow-review-what-you-staged).
