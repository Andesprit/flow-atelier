# Validating conduits

Use `atelier check` to validate a conduit before running it. Use `--recursive` to check its child conduits.

## A field Atelier does not know is an error

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
typo_demo [project] — FAIL: conduit.yaml:12 typo_demo.consume: unknown key 'depend_on'; remove it or correct its spelling. Did you mean depends_on?
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

## Reading JSON command errors

When a command with `--json` fails before it has a result, stdout is empty.
Stderr contains one JSON object. Branch on `error.code` and display
`error.message` to the user. For example, `atelier logs missing --follow --json`
exits 2 and writes this object to stderr.

```json
{"error":{"code":"invalid_arguments","message":"--follow and --json are mutually exclusive","parameter":"--follow"}}
```

Warnings from inspection commands go to stderr with a result or into
`error.diagnostics` with a command error. Streaming commands keep stderr live.

An unknown flow exits 1 with `unknown_flow` and the requested `flow_id`.
An ambiguous prefix exits 1 with `ambiguous_flow_id` and a `candidates` array
of matching IDs. An invalid choice can include an `allowed` array.
For `rm --json`, supply `--yes` to confirm deletion.

A nonzero exit can also accompany a result on stdout. `check --json` returns
its report even when a conduit fails validation. Read each row's advisory
`warnings` when present; a serial agent handoff or a loop marker an agent can
print may warrant a change even when `ok` is true. An allocated `run --json`
returns `{"flow_id":"…","status":"failed"}` on failure, so the caller can
diagnose that exact run. `wait --json` returns the
observed terminal status for failed, stopped and crashed flows. A wait that
times out exits 124 with a `wait_timeout` error on stderr.

## Checking conduits from a script or an agent

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

## Checking a workflow that calls other workflows

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
    task: echo "summary of $INPUT_FINDING"
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

### The call's arguments, not only the child's name

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
    task: echo "$INPUT_TONE summary of $INPUT_FINDING"
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
