# Compare two implementations in separate checkouts

Give two agents the same base commit and fixture when you want to choose
between implementations. Each writer gets its own Git worktree. The shared
fixture stays in the source checkout so each candidate faces the same test.

This Bash exercise needs Git, Python 3 and three logged-in harnesses.
The reviewer needs a read-only mode. Substitute another supported harness
if needed. Start by checking each connection.

```bash
atelier --version || exit 1
git --version || exit 1
atelier harness check claude-code || exit 1
atelier harness check codex || exit 1
atelier harness check gemini || exit 1
```

## Make a common starting point

A billing job should alert at 15 minutes. The program takes a minute count
and prints `ok` or `late`. Start with a deliberately incomplete implementation.

```bash
work_dir="$(mktemp -d)/parallel agents" || exit 1
mkdir -p "$work_dir" && cd "$work_dir" || exit 1
git init -q || exit 1
printf '.atelier/\n' > .gitignore
printf 'echo ok\n' > billing.sh
cat > accept.sh <<'SH'
set -eu
for minutes in 0 14 15 20; do
  expected=ok
  if [ "$minutes" -ge 15 ]; then expected=late; fi
  actual=$(bash billing.sh "$minutes")
  [ "$actual" = "$expected" ] || { echo "$minutes: expected $expected, got $actual"; exit 1; }
done
SH
git add .gitignore billing.sh accept.sh || exit 1
git -c user.name=Demo -c user.email=demo@example.com commit -qm "Add billing alert fixture" || exit 1
base=$(git rev-parse HEAD) || exit 1
```

## Give each writer a checkout

The two writers have no dependencies. The reviewer receives their replies
and the paths where they worked. Save the recipe in the source checkout.

```bash
mkdir -p .atelier/conduits/candidates || exit 1
cat > .atelier/conduits/candidates/conduit.yaml <<'YAML'
name: candidates
description: Compare billing alerts
inputs:
  brief: Acceptance criteria and shared fixture
tasks:
  - step_1:
      description: Implement with a shell test
      tool: harness:claude-code
      task: "Use a shell test in billing.sh. {{inputs.brief}}"
  - step_2:
      description: Implement with arithmetic
      tool: harness:codex
      task: "Use Bash arithmetic in billing.sh. {{inputs.brief}}"
  - compare:
      description: Run the source fixture against each candidate
      tool: tool:bash
      depends_on: [step_1, step_2]
      task: |
        fixture="$PWD/accept.sh"
        for checkout in "{{step_1.workspace}}" "{{step_2.workspace}}"; do
          if (cd "$checkout" && bash "$fixture"); then result=pass; else result=fail; fi
          printf '%s | %s\n' "$checkout" "$result"
        done
  - synthesis:
      description: Compare candidates
      tool: harness:gemini
      permissions: read-only
      depends_on: [step_1, step_2, compare]
      task: |
        {{inputs.brief}}
        Inspect the diff in each checkout. Shared fixture results:
        {{compare.output}}
        Compare candidate, actual checkout path, test result and one tradeoff in a table.
        First checkout: {{step_1.workspace}}
        Second checkout: {{step_2.workspace}}
YAML
cp .atelier/conduits/candidates/conduit.yaml recipe.snapshot || exit 1
```

Start with a clean tracked working tree. The preview names the selected tasks,
source repository and base commit. A worktree contains the tracked files at
that commit. Install any dependencies the candidate needs in its checkout.

```bash
atelier plan candidates --worktree step_1 --worktree step_2 || exit 1
atelier plan candidates --json --worktree step_1 --worktree step_2 || exit 1
```

```bash
brief="Edit billing.sh only. Print late at 15 minutes or more, otherwise ok. Shared fixture: $work_dir/accept.sh"
atelier run candidates --worktree step_1 --worktree step_2 \
  --input brief="$brief" --json > run.json || exit 1
flow_id=$(python3 -c 'import json; print(json.load(open("run.json"))["flow_id"])') || exit 1
```

## Compare the results

The shell comparison runs the same source fixture with each candidate as its
current directory. A failing candidate gets its own result row. Read the
actual checkout paths and results alongside the reviewer's table.

```bash
atelier status "$flow_id" --json || exit 1
atelier outputs "$flow_id" --task compare || exit 1
atelier outputs "$flow_id" --task synthesis || exit 1
```

```bash
[ -z "$(git status --porcelain --untracked-files=no)" ] || exit 1
[ "$(git rev-parse HEAD)" = "$base" ] || exit 1
```

## Recover a failed review

To see how recovery works, request an unavailable model for the reviewer.
Here the reviewer also has a checkout. The two writers finish before the
review fails.

```bash
if atelier run candidates --worktree step_1 --worktree step_2 --worktree synthesis \
  --agent synthesis=gemini:no-such-model-xyz --input brief="$brief" --json > broken.json; then
  echo "Expected the unavailable model to fail" >&2
  exit 1
fi
broken_id=$(python3 -c 'import json; print(json.load(open("broken.json"))["flow_id"])') || exit 1
atelier diagnose "$broken_id" || exit 1
```

A draft file makes checkout reuse visible. Resume with the working reviewer.
The draft survives and the completed writers keep their results.

```bash
printf 'half a thought\n' > ".atelier/workspaces/$broken_id/synthesis/DRAFT.md" || exit 1
atelier run --resume "$broken_id" --agent synthesis=gemini || exit 1
cat ".atelier/workspaces/$broken_id/synthesis/DRAFT.md" || exit 1
```

A fresh run inherits the selection and creates new checkouts from the source
repository's current HEAD. The old candidates remain available for comparison.

```bash
atelier run --again "$broken_id" --agent synthesis=gemini --json > again.json || exit 1
new_id=$(python3 -c 'import json; print(json.load(open("again.json"))["flow_id"])') || exit 1
git worktree list || exit 1
cmp .atelier/conduits/candidates/conduit.yaml recipe.snapshot || exit 1
```

Worktrees isolate file edits but share Git history and the user's operating
system permissions. Choose and integrate the winning change yourself. Git
retains these worktrees until you remove them, even if you delete a flow.
