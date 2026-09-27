# Recovering a failed workflow

A run across several agents fails on its third task. Twenty minutes of agent
work is already saved, the terminal that printed the error is gone, and the
question is not "what broke" but "what do I still have, and what will it cost
to finish".

`atelier diagnose <flow>` answers that from what the run saved: the task that
actually failed, the work that is kept and who produced it, and the exact
commands to read or continue it. This is a 4-minute exercise. You will break a
run on purpose, read it back, repair it, and finish it without paying for the
agent work twice.

You should have done [Orchestrating several agents in one
workflow](multi-agent-workflow.md) first.

## Before you start

One agent you are logged into. The guide uses `claude-code`; any name from
`atelier list harnesses` works if you change it in both places below.

```bash
atelier --version || exit 1
```

The blocks below are Bash — macOS, Linux, WSL or Git Bash. They quote every
path and gate every line, so you can paste them into a script without adding
error handling of your own.

## 1. A workflow with something to lose

```bash
work_dir="$(mktemp -d)/incident work" || exit 1
mkdir -p "$work_dir/.atelier/conduits/triage" || exit 1
cd "$work_dir" || exit 1
echo "working in: $work_dir"
cat > .atelier/conduits/triage/conduit.yaml <<'YAML' || exit 1
name: triage
description: an agent reads the incident, a check runs, an agent concludes
tasks:
  - name: analyse
    description: the expensive part
    tool: harness:claude-code
    task: "In one sentence, name the most likely cause of a retry storm."
  - name: verify
    description: a local check that will fail
    tool: tool:bash
    depends_on: [analyse]
    task: |
      if [ -f approved.txt ]; then
        echo "check passed"
      else
        echo "approved.txt is missing" >&2
        exit 4
      fi
  - name: conclude
    description: needs both
    tool: harness:claude-code
    depends_on: [analyse, verify]
    task: "Combine {{analyse.output}} and {{verify.output}} into one line."
YAML
```

`analyse` costs a real agent turn. `verify` fails until a file exists.
`conclude` needs both, so it never starts.

## 2. Break it

```bash
atelier run triage
echo "exit: $?"
```

The agent answers, the check fails, and the run stops. Note the `flow_id:` it
printed — everything below can also find it with `latest`.

## 3. Ask what you have

```bash
atelier diagnose latest || exit 1
```

Read it top to bottom. It tells you four different things, and keeps them
apart:

- **What failed.** `verify`, exit 4, with the tail of its own output. The
  cancelled `conclude` is listed apart, under *cut off when the run failed* — a
  cancellation is not a second failure, and reporting it as one sends you
  hunting for a bug that isn't there. Nor is it reported as work that never
  happened: a task can be cancelled while it is running, so the report says
  whether its own saved records show it executing, and says *unknown* when they
  cannot settle it rather than promising nothing was touched.
- **What is kept.** `analyse`, the agent that actually produced it, and the
  size of its saved result. A resume replays that result; it does not ask the
  agent again.
- **Where the agent attribution comes from.** `from its log entry` means that
  is what ran. `from this run's saved choice` means the run recorded the
  selection but never got a log for it. `unknown` means nothing saved says —
  the report will not guess from the current recipe, because you may have
  edited it since.
- **What you can do.** Each line is a command with this run's full id already
  in it, ready to paste. Diagnose runs none of them.

Add `--json` for the same report as one object — the shape a coding agent or a
script should read, and what makes `diagnose` useful to something that is not
a person:

```bash
atelier diagnose latest --json | head -30 || exit 1
```

## 4. Repair, then finish

The report named the missing file. Create it, then run the resume line it
printed:

```bash
touch approved.txt || exit 1
atelier run --resume latest || exit 1
```

Watch what starts: `verify` and `conclude`, not `analyse`. The agent turn you
already paid for is replayed from the saved result.

```bash
atelier outputs latest || exit 1
atelier diagnose latest || exit 1
```

The second report now says *completed*, with nothing to recover.

## What diagnose will not tell you

- **Which tasks a resume will run.** It reports what completed and what did
  not, separately. Resume re-reads the conduit file as it stands *now*, so if
  you edited or renamed tasks since the run, the shape that runs is the
  current one. The report says so, and names any task the file gained or lost.
- **That a run is safe to resume, when it might still be going.** A saved
  `running` status is only called *crashed* when the runner process is
  provably gone on this machine. On another host, with no pid recorded, or
  with a pid that cannot be probed, the state stays uncertain and no resume is
  recommended — resuming a run that is in fact alive runs the work twice.
- **What a stopped run would do next.** Resume does not take a stopped or
  completed flow. The report points at `atelier run --again <id>` instead,
  which starts a fresh run of the same recipe and inputs.
- **Anything it had to guess.** A missing outputs file, a task with no log
  entry, a recipe that no longer parses: each is reported as unavailable, with
  the evidence that *does* exist. A task whose only record is the live steps
  it wrote before being killed says exactly that.

Retrying unfinished work costs what it costs: `atelier run --resume` starts
real agents again for the tasks that did not complete, and they can spend
tokens and change your files. The report says that on the line that suggests
it.
