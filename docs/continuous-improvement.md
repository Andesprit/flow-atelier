# Continuous improvement

Install the platform package, then run the recipe against a GitHub repository
with an `origin` remote, a `main` branch, and Git and GitHub CLI authentication
on the launcher. Install Claude Code for edits and Codex for proposals.
The target's current checkout can have local edits. The experiment uses
temporary checkouts of `origin/main`.

The coordinator runs Git and the GitHub CLI with the launcher's publishing
environment. Agent sessions and measurement commands drop inherited GitHub
token, SSH agent and askpass variables. Use a trusted target and workload.

```sh
atelier install builtin:platform --no-project
atelier run continuous-improvement --input project=/path/to/repo --input goal=latency
```

Set a recurring trigger with `atelier schedule add`. Pass the project, goal
and a descriptive `schedule` input in the job. The recipe records that label
in its decision. The default label for an interactive run is `manual`.

## Fixed workload

Commit `docs/improvement/latency.sh` and `docs/improvement/test.sh` to
`main`. Both run under Bash with the version under test as their working
directory. The coordinator reads the scripts from the original baseline
checkout for both versions. Keep the fixed workload, fixtures and test
drivers under `docs/improvement/`. Keep the behavior checks independent of
the proposed change.

The latency driver must run a fixed operation and print one measured number.
For instance, to measure process startup in milliseconds for a CLI that
implements `python -m my_app --version`, use this driver.

```sh
python - <<'PY'
import subprocess
import sys
import time

started = time.perf_counter_ns()
subprocess.run([sys.executable, "-m", "my_app", "--version"],
               stdout=subprocess.DEVNULL, check=True)
print((time.perf_counter_ns() - started) / 1_000_000)
PY
```

The behavior driver runs the project's test suite and returns its exit status,
for example `python -m pytest -q`. Prepare any dependencies needed by these
commands on the launcher. The recipe refuses changes to either driver, the
decision log or the top-level `test/` and `tests/` directories.

To use other units or a higher threshold, commit `docs/improvement/settings.json`.

```json
{"latency": {"unit": "ms", "min_gain_percent": 5}}
```

## Cost

Run with `--input goal=cost`. Commit `docs/improvement/cost.sh` to run a
fixed workload and print its metered cost per run. Alternatively, print
usage from the workload as JSON, for example
`{"input_tokens":12000,"output_tokens":850}`. For token usage, set both
prices per million in `settings.json`. Use the rates for the workload's
model and currency. The numbers below are illustrative.

```json
{"cost": {"unit": "USD", "input_per_million": 2, "output_per_million": 8}}
```

Cost uses the same paired samples, noise check and threshold as latency.

## Clean code

Run with `--input goal=clean-code`. Select the review rubric in the
launcher's `~/.atelier/settings.json`. Both judges receive the selected
instructions. For example, select a rule file from the target checkout.

```json
{"stepInstructions": {"project_rules": {"/absolute/path/to/repo": ["docs/review.md"]}}}
```

For Python, install Radon, Vulture, Ruff and Pyright on the launcher's PATH.
The recipe records maximum cyclomatic complexity, longest function in lines,
repeated six-line windows, high-confidence dead-code findings, lint findings,
and type errors plus warnings. For another language or an existing analysis
suite, commit `docs/improvement/clean-code.sh` and emit all six measurements.

```json
{"complexity": 7, "function_length": 52, "duplication": 2, "dead_code": 0, "lint_warnings": 0, "type_warnings": 1}
```

If behavior tests pass and every count holds steady or improves, Codex and
Claude Code each compare the changed files in a fresh read-only session.
The recipe shuffles the A/B labels and reverses their order for the second
judge. Both must independently prefer the edit. The log records the votes and
label mapping so a reviewer can check the decision.

## UX

Run with `--input goal=ux`. Commit `docs/improvement/ux.sh` to launch
the version under test, run a fixed browser task, and clean up on exit.
Use the same browser, viewport, fixtures and starting state for every run.
Write the Lighthouse JSON report to the script's first argument, for example
with `lighthouse "$url" --output=json --output-path="$1" --quiet`. On
standard output, print the observations from your automated task and
accessibility checks.

```json
{"steps": 4, "task_completed": true, "keyboard_passed": true, "accessibility_violations": 0}
```

Every run must finish the task with keyboard access. The maximum violation
count must hold steady or improve. Median step count, Lighthouse performance,
and Lighthouse accessibility must each hold steady or improve, and at least
one must clear the configured threshold and observed baseline noise.

## Decisions and pull requests

The proposer sees the measured baseline, `docs/improvement-log.md`, open
improvement PRs, and improvement branches before suggesting one hypothesis.
The hypothesis has a stable lowercase key. Reuse the key for the same
experiment. A durable reservation gives one run ownership of that key for
the project and goal.

For latency, cost and UX, five paired samples alternate which version runs
first. The median paired percentage gain must reach the configured minimum and exceed the full
baseline range expressed as a percentage of the baseline median. Behavior
checks must pass. The PR to `main` includes the change and the decision when
the experiment passes. A rejection produces a PR containing only the
decision log, measurements and reason. The recipe leaves merge to a human.

If PR creation fails after the branch is pushed, resume the run to retry
publication of that branch. Once the log PR lands, the entry on `main` rules
out the same hypothesis even on a launcher with a fresh claim database.
