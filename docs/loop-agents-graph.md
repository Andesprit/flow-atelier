# One file: a fix loop with agent reviews

This walkthrough starts in an empty directory and builds a workflow with a
builder that plans and fixes, a shell test, two parallel reviews, and a final
verdict. The loop and its body live in one `conduit.yaml`. You can preview the
whole graph and swap the fix agent for one run without editing the recipe.

You need Atelier, Bash, and at least one working agent. The starter uses
`claude-code` by default, as the `code-review` starter does. The example also
uses `codex` for the fix step. Check both before spending a run:

```bash
atelier harness check claude-code
atelier harness check codex
```

If Codex is unavailable, use another installed harness in the `--agent` value
below. If Claude Code is unavailable, change the `harness:claude-code` entries
in the generated file to your installed harness, then run `atelier check`.

## Create the workflow and a tiny test command

```bash
mkdir loop-demo
cd loop-demo
git init
atelier create ship --template fix-loop
```

`create` writes only `.atelier/conduits/ship/conduit.yaml`. Open it to see the
graph: `fix_until_green → {review_correctness,
review_maintainability} → verdict`. The two reviews share the same dependency
and inspect the working tree diff and the passing test output.
`max_concurrency: 2` lets them run together. Inside `fix_until_green`,
`fix` runs before `test`. The loop runs at most four times. A passing shell
check prints the marker for `until: output.match(TESTS PASSED)`.
`on_exhaust: fail` blocks reviews after four failed attempts. The builder
receives the goal on every attempt and the previous failure through
`{{loop.previous}}` on retries.

The `test_command` input defaults to `python3 -m pytest -q` for a Python
project; pass `--input test_command='python -m pytest -q'` if your machine
only has `python`. This small shell test makes the loop visible without requiring a
Python test suite. It fails once, then passes:

```bash
cat > check.sh <<'SH'
n=$(cat .attempts 2>/dev/null || echo 0)
n=$((n + 1))
echo "$n" > .attempts
if [ "$n" -lt 2 ]; then echo "one failing test"; exit 1; fi
echo "test suite green"
SH
```

For your own project, omit the `test_command` input to use the default, or
pass your real check command. The test task prints the command's output,
including its error output, then `TESTS FAILED` when that command exits
nonzero and `TESTS PASSED` when it succeeds. The task exits with the command's
failure code after printing `TESTS FAILED`. Atelier retries a failed attempt
without evaluating its output predicate, so even a builder that says
`TESTS PASSED` cannot send a failing change to review.

## Preview and run

```bash
atelier check ship --recursive
atelier plan ship --agent fix_until_green.fix=codex
atelier run ship --input goal='make the demo pass' --input test_command='bash check.sh' --agent fix_until_green.fix=codex
atelier status latest
```

`check` validates the inline body. `plan` shows `fix [harness:codex]` and
`test [tool:bash]` inside `fix_until_green`, with the two reviews in the same
wave. Neither command starts an agent. In the run, Codex receives both fix
prompts; the second includes `TESTS FAILED` from the first attempt. Status
reports `fix_until_green` at iteration `2/4` and lists both passes: the fix
agent, the pass result, and whether the condition was met. During the run,
the child banners read `fix_until_green 1/4 > fix` and then `2/4 > fix`.
The `--agent` choice applies only to this run.

For a specific model and effort, the same address accepts
`--agent fix_until_green.fix=codex:<model>:<effort>` when that harness offers
those choices. You can also override a top-level task, for example
`--agent review_correctness=codex`.

## Compare agents after a swap

Run the same inputs again with a different fix agent, then compare the saved
runs. `--again` copies the first run's inputs; the dotted `--agent` choice
applies only to the new run:

```bash
rm .attempts
atelier run ship --again latest --agent fix_until_green.fix=claude-code
atelier compare ship
```

`compare ship` selects its two newest top-level runs by start time. It shows
each run's status and duration, each task's recorded agent, and the loop's
passes and condition result. Changed rows are marked. If other runs happened
in between, use `atelier compare <first-flow-id> <second-flow-id>` or add
`--json` for structured output. Comparison reads saved runs; it does not call
an agent or alter the recipe. Reset the project's test data and other relevant
state before a rerun if you want to judge the agent choice fairly; `--again`
reuses inputs, but it does not reset the working directory.

## Make it fail, diagnose it, repair it

Create a test command that never passes:

```bash
cat > always-fail.sh <<'SH'
echo "one failing test"
exit 1
SH
atelier run ship --input goal='make the demo pass' --input test_command='bash always-fail.sh' --agent fix_until_green.fix=codex
atelier status latest
atelier diagnose latest
```

The `run` command is expected to exit 1. Status records that
`fix_until_green` exhausted four iterations without matching its loop
predicate and that both reviews and the verdict were cancelled. Status and
diagnose show passes `1/4` through `4/4`, each with `fix [harness:codex]`,
the failed `test [tool:bash]` step, its exit code, and `condition not met`.
Repair the check or code so the command exits zero. Here, choose the working
test command for a fresh run:

```bash
rm .attempts
atelier run ship --input goal='make the demo pass' --input test_command='bash check.sh' --agent fix_until_green.fix=codex
atelier status latest
```

The `rm` resets only this demo's test counter. A real project should fix its
failing test or implementation. You can raise `repeat` if four attempts are
too few; leave `on_exhaust: fail` in place when later tasks require green
tests. If an agent itself fails inside the loop, that try counts as failed
and the loop goes on. If the last try fails that way too, `atelier diagnose
latest` names the nested task, agent, and iteration and gives the parent run's
`atelier run --resume <flow-id>` command.
