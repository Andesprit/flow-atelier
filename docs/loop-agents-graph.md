# One file: a fix loop with agent reviews

This walkthrough starts in an empty directory and builds a workflow with a
planning agent, a fix/test loop, two reviews that can run together, and a final
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
graph: `plan → fix_until_green → {review_correctness,
review_maintainability} → verdict`. The two reviews share the same dependency
and `max_concurrency: 2` lets them run together. Inside `fix_until_green`,
`fix` runs before `test`. `repeat: 4` caps attempts; `until:
output.match(TESTS PASSED)` stops early; `on_exhaust: fail` keeps reviews from
running if the test never passes. The fix prompt receives the previous attempt
through `{{loop.previous}}`.

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
nonzero and `TESTS PASSED` when it succeeds. A failing check is
feedback for the next loop attempt, so it does not abort the run by itself.

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
agent, the test's last line, and whether the condition was met. During the run,
the child banners read `fix_until_green 1/4 > fix` and then `2/4 > fix`;
an unmet pass has an amber `condition not met` marker. The recipe still names
Claude Code for `fix`: the `--agent` choice applies only to this run.

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
predicate and that the reviews were cancelled. Both status and diagnose show
passes `1/4` through `4/4`, each with `fix [harness:codex]`, `TESTS FAILED`,
and `condition not met`. The live result panel warns on each unmet pass, and
the final loop row in status reads `4/4`.
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
tests. If an agent itself fails inside the loop, `atelier diagnose latest`
names the nested task, agent, and iteration and gives the parent run's
`atelier run --resume <flow-id>` command.
