# Continuous improvement

Install the platform package, then run the recipe against a GitHub repository
with an `origin` remote, a `main` branch, and Git and GitHub CLI authentication
on the launcher. Install Claude Code for edits and Codex for proposals.
The target's current checkout can have local edits. The experiment uses
temporary checkouts of `origin/main`.

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

## Decisions and pull requests

The proposer sees the measured baseline, `docs/improvement-log.md`, open
improvement PRs, and improvement branches before suggesting one hypothesis.
The hypothesis has a stable lowercase key. Reuse the key for the same
experiment. A durable reservation gives one run ownership of that key for
the project and goal.

Five paired samples alternate which version runs first. The median paired
percentage gain must reach the configured minimum and exceed the full
baseline range expressed as a percentage of the baseline median. Behavior
checks must pass. The PR to `main` includes the change and the decision when
the experiment passes. A rejection produces a PR containing only the
decision log, measurements and reason. The recipe leaves merge to a human.

If PR creation fails after the branch is pushed, resume the run to retry
publication of that branch. Once the log PR lands, the entry on `main` rules
out the same hypothesis even on a launcher with a fresh claim database.

