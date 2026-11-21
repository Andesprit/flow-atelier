# When to give a second agent a task

A task is one checkable result owned by one agent with the skills it needs.
Keep planning and implementation together when both serve one change. Split
when you need fresh context, parallel work, an independent test or review,
or a different permission level or agent.

## Check the evidence for a decision

Suppose an on-call team wants to move a nightly job. One reader gathers
evidence. A fresh reviewer opens the cited sources and writes the decision
memo. Both need only read access. This Bash exercise uses Git, Python 3 and
two logged-in agents with read-only support.

```bash
atelier harness check claude-code || exit 1
atelier harness check codex || exit 1
work_dir="$(mktemp -d)/agent handoff" || exit 1
mkdir -p "$work_dir" && cd "$work_dir" || exit 1
git init -q || exit 1
printf '02:00 billing starts\n02:02 backup starts\n02:18 billing ends\n' > incident.txt
printf 'Backups must finish by 03:00.\n' > policy.txt
git add incident.txt policy.txt || exit 1
git -c user.name=Demo -c user.email=demo@example.com commit -qm "Record incident and policy" || exit 1
```

```bash
atelier compose triage --permissions read-only \
  -s "claude-code=Use the incident and policy files. Label each claim with file and line. Identify missing evidence." \
  -s "codex=Open each cited line. Write a decision memo listing supported claims, unsupported claims and the next check." || exit 1
atelier check triage --json || exit 1
atelier plan triage || exit 1
atelier run triage --input brief="Would moving the backup avoid a billing overrun while meeting policy?" --json > triage.json || exit 1
flow_id=$(python3 -c 'import json; print(json.load(open("triage.json"))["flow_id"])') || exit 1
atelier outputs "$flow_id" --task step_2 || exit 1
```

For example, the incident shows an overlap at lines 1–3. It gives no backup
duration or evidence of causation. A checker can recommend measuring both
before changing the schedule. The generated YAML hands the first response and
the original brief to the checker.

## Review one commit in parallel

Use a panel when different reviewers should examine the same change without
seeing each other’s findings. Pin the commit in the brief. Give each reviewer
a finding format and let synthesis resolve disagreements against the diff.

```bash
revision=$(git rev-parse HEAD) || exit 1
atelier compose panel --parallel --permissions read-only \
  -s "claude-code=Check correctness at the specified commit. Give each finding as file:line, impact, verification, or say no findings." \
  -s "codex=Check for missing test coverage at the specified commit. Give each finding as file:line, impact, verification, or say no findings." \
  --synthesize "claude-code=Check disagreements against the commit and rank actionable findings with their evidence." || exit 1
atelier plan panel || exit 1
if ! atelier run panel --input brief="Review the patch from git show $revision." --json > panel.json; then
  [ -s panel.json ] || exit 1
  echo "Read the saved status before continuing." >&2
fi
```

If a reviewer fails, synthesis waits for its result. For a failed allocated
run, read its ID from the completion record and diagnose it. Once the failed
reviewer is available, resume. Completed reviewers keep their results.

```bash
panel_id=$(python3 -c 'import json; print(json.load(open("panel.json"))["flow_id"])') || exit 1
if [ "$(python3 -c 'import json; print(json.load(open("panel.json"))["status"])')" = failed ]; then
  atelier diagnose "$panel_id" || exit 1
  atelier run --resume "$panel_id" || exit 1
fi
atelier outputs "$panel_id" --task synthesis || exit 1
```

## Build a change with an independent test

In a project with a trusted test command, the fix-loop starter puts planning
in the builder’s first attempt. A shell task checks the actual change once
per attempt. Failure sends the diagnostic back to the builder. After four
failed attempts the flow fails. A passing exit status unlocks two parallel
diff reviews and a combined verdict. You decide whether to merge.

Try it in the incident exercise above. Give the builder a writable task and
keep the acceptance command under your control.

```bash
atelier create incident-summary --template fix-loop || exit 1
atelier run incident-summary --input goal="Write the billing duration from incident.txt to duration.txt as one line: 18 minutes." \
  --input test_command='test "$(cat duration.txt)" = "18 minutes"' --json
```

[Run the fix-loop starter](loop-agents-graph.md) ·
[Swap agents for a run](reusing-a-workflow-with-other-agents.md) ·
[Compare implementations in separate checkouts](parallel-agents-in-separate-checkouts.md)
