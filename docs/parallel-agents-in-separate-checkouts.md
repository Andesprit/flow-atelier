# Two agents changing the same code at once

Parallel review is easy: two agents read, and their answers are just text.
Parallel *implementation* is not, because both agents edit files, and by
default every task of a run shares one working directory. Point two coding
agents at the same repository and they overwrite each other.

`--worktree <task>` fixes that. Each selected task gets its own detached Git
worktree, cut from your checkout's current commit before anything starts. Two
agents can then rewrite the same file, both results survive, and you read them
side by side and decide.

This exercise runs two agents on one brief in separate checkouts, compares the
candidates, recovers a failed step without redoing the finished ones, and
proves your own checkout never moved. You should have done [Orchestrating
several agents in one workflow](multi-agent-workflow.md) first.

## Before you start

Three agents you are already logged into, and Git. The guide uses
`claude-code` and `codex` as the two writers and `gemini` as the reviewer that
reads both; any three names from `atelier list harnesses` work.

```bash
atelier --version || exit 1
git --version || exit 1
atelier harness check claude-code || exit 1
atelier harness check codex || exit 1
atelier harness check gemini || exit 1
```

`harness check` opens an ACP session and closes it without sending a prompt, so
it costs no tokens. Every block below ends its lines in `|| exit 1`: paste them
into a script and a failed prerequisite stops there rather than halfway through
a run.

## 1. A disposable repository

`--worktree` needs a repository with at least one commit, because a worktree is
cut from a commit. The space in the folder name is deliberate; real project
paths have them.

```bash
work_dir="$(mktemp -d)/parallel agents" || exit 1
mkdir -p "$work_dir" || exit 1
cd "$work_dir" || exit 1
git init -q . || exit 1
git config user.email you@example.com || exit 1
git config user.name "You" || exit 1
printf '.atelier/\n' > .gitignore || exit 1
printf 'The nightly billing job runs at 02:00 and often overruns.\n' > NOTES.md || exit 1
git add -A || exit 1
git commit -qm "start" || exit 1
base="$(git rev-parse HEAD)" || exit 1
echo "working in: $work_dir at $base"
```

`.atelier/` holds the run records and the checkouts. Keeping it out of the
index is what you would do in a real project anyway.

## 2. A recipe you are not going to edit

```bash
atelier compose candidates --parallel \
  -s "claude-code=Rewrite NOTES.md so an on-call engineer knows what to do. Edit the file." \
  -s "codex=Rewrite NOTES.md so an on-call engineer knows what to do. Edit the file." \
  --synthesize "gemini=Two engineers rewrote NOTES.md. Say which version you would keep and why. Do not edit anything." \
  || exit 1
cp ".atelier/conduits/candidates/conduit.yaml" "$work_dir/recipe.snapshot" || exit 1
```

Two agent tasks, `step_1` and `step_2`, with no dependency on each other, and a
`synthesis` that waits for both. Only the two writers need a checkout of their
own: the reviewer reads and reports, so it runs where you are.

It is an ordinary conduit — read it, edit it, check it into your repository.
Isolation is not written into the file: it is something you ask for per run.

## 3. See what would be cut, before cutting it

```bash
atelier plan candidates --worktree step_1 --worktree step_2 || exit 1
atelier plan candidates --json --worktree step_1 --worktree step_2 || exit 1
```

Each selected task is marked `⌂ --worktree (own checkout)`, and one line above
the waves names the repository and the exact commit a run would cut from. The
JSON carries the same answer under `isolation` (`tasks`, `source`, `base`) and
an `isolated` flag per task.

`plan` creates nothing: no worktree, no flow, no agent. If the source would
refuse the run — uncommitted work, no commit yet, not a repository — the
preview says so as `problem` instead of pretending it would succeed.

## 4. Run both agents at once

```bash
brief="Make NOTES.md useful at 02:00. Constraint: one screen, no jargon." || exit 1
atelier run candidates --worktree step_1 --worktree step_2 \
  --input brief="$brief" > parallel.log 2>&1 || { cat parallel.log; exit 1; }
cat parallel.log || exit 1
flow_id="$(sed -n 's/^flow_id: //p' parallel.log)" || exit 1
[ -n "$flow_id" ] || exit 1
echo "run: $flow_id"
```

The checkouts are created before the first agent starts, so a selection that
cannot work costs no prompt. Both agents then run at the same time, each in its
own copy of the repository at `$base`.

## 5. Read both candidates

```bash
ws=".atelier/workspaces/$flow_id" || exit 1
atelier status "$flow_id" || exit 1
ls "$ws" || exit 1
cat "$ws/step_1/NOTES.md" || exit 1
cat "$ws/step_2/NOTES.md" || exit 1
git -C "$ws/step_1" diff --stat "$base" || exit 1
git -C "$ws/step_2" diff --stat "$base" || exit 1
```

`status` grows a `checkout` column with the absolute path of each task's
directory, and names the source and the commit they were cut from. A blank cell
is a task that ran in the shared working directory. `status --json` carries the
same thing under `workspaces` as `source`, `base` and `paths`.

Each directory is a normal checkout: `git -C <path> diff`, `git -C <path> log`,
your editor, your test runner. Nothing was merged and nothing will be — reading
the two diffs and deciding is your job, and it is the point.

The `synthesis` step was told where to look without the recipe mentioning a
path. Because `step_1` and `step_2` had checkouts of their own, its prompt
gained one authoritative block naming them:

```text
--- BEGIN WORKSPACE PROVENANCE (authoritative) ---
step_1 worked in /…/workspaces/<flow_id>/step_1
step_2 worked in /…/workspaces/<flow_id>/step_2
Each is its own Git checkout of /…/parallel agents at commit <base>.
Nothing has been merged: open a directory to read what that worker actually changed.
--- END WORKSPACE PROVENANCE (authoritative) ---
```

Your prompt text is never rewritten to make room for it, and a run without
`--worktree` gains nothing. For explicit control, `{{<task>.workspace}}` in a
prompt resolves to that task's directory — its own checkout when it has one,
otherwise the run's shared directory.

## 6. Your own checkout did not move

```bash
cat NOTES.md || exit 1
git rev-parse HEAD || exit 1
[ -z "$(git status --porcelain --untracked-files=no)" ] || exit 1
[ "$(git rev-parse HEAD)" = "$base" ] || exit 1
echo "source branch, index and files are exactly as you left them"
```

No branch was created, checked out, moved or committed. The worktrees are
detached at `$base`.

## 7. A failure, and a resume that keeps the finished work

A pinned model the agent does not offer is refused when the session opens —
after the two writers have already finished. This block makes that happen on
purpose, so the recovery is real rather than described:

```bash
atelier run candidates \
  --worktree step_1 --worktree step_2 --worktree synthesis \
  --agent synthesis="gemini:no-such-model-xyz" \
  --input brief="$brief" > broken.log 2>&1
cat broken.log || exit 1
broken_id="$(sed -n 's/^flow_id: //p' broken.log)" || exit 1
[ -n "$broken_id" ] || exit 1
atelier status "$broken_id" || exit 1
```

`step_1` and `step_2` are `completed`, each with its own checkout; `synthesis`
is `failed`, and its checkout exists with whatever it managed before failing.
Nothing was cleaned up.

```bash
printf 'half a thought\n' > ".atelier/workspaces/$broken_id/synthesis/DRAFT.md" || exit 1
atelier run --resume "$broken_id" --agent synthesis=gemini || exit 1
cat ".atelier/workspaces/$broken_id/synthesis/DRAFT.md" || exit 1
atelier status "$broken_id" || exit 1
```

The `DRAFT.md` stands in for work in progress an interrupted agent left behind.
The resume continues in that same directory and it is still there afterwards:
the checkouts belong to the run, so recovery goes back to them rather than
cutting replacements. The two finished writers are not prompted again, and
their directories are untouched.

`--resume` and `--worktree` are mutually exclusive, for the same reason: a new
selection would mean abandoning the edits the run already has.

## 8. Do it again, with fresh checkouts

```bash
atelier run --again "$broken_id" --agent synthesis=gemini \
  > again.log 2>&1 || { cat again.log; exit 1; }
new_id="$(sed -n 's/^flow_id: //p' again.log)" || exit 1
[ -n "$new_id" ] || exit 1
[ -d ".atelier/workspaces/$new_id/step_1" ] || exit 1
[ -d ".atelier/workspaces/$broken_id/step_1" ] || exit 1
git worktree list || exit 1
cmp ".atelier/conduits/candidates/conduit.yaml" "$work_dir/recipe.snapshot" || exit 1
echo "the recipe is byte-identical after three runs"
```

`--again` inherits which tasks are isolated, not the directories: a new flow
gets new checkouts, cut from the source repository's HEAD **as it is now** —
not from the old run's base. The old run keeps its own directories, status and
outputs.

`git worktree list` shows every checkout Git knows about, including all of
these. They are yours now. When you are done reading them:

```text
git worktree remove ".atelier/workspaces/<flow_id>/step_1"
git worktree remove --force ".atelier/workspaces/<flow_id>/step_2"   # if it has edits
rm -rf "$(dirname "$work_dir")"                                      # the whole exercise
```

`atelier rm <flow_id>` deletes the run record only. It does not touch a
checkout, and it never will: a pruned log should not take an agent's work with
it.

## What isolation is, and what it is not

* **It is a separate working copy.** Each selected task gets a detached Git
  worktree of the same pinned commit, so two agents editing the same relative
  path do not collide, and each result is readable on its own.
* **It is not a sandbox.** A worktree shares the repository's object store and
  puts no limit on what a process can open, run, bind or spend. An agent that
  goes looking outside its directory will find things. Isolation here is about
  agents not overwriting each other, not about containment.
* **It is not a copy of your environment.** Only tracked files at that commit
  are checked out. `node_modules`, `.venv`, `.env`, build output and anything
  else untracked or ignored is absent, and installing dependencies in each
  checkout is yours to do — often the largest real cost of this workflow.
* **It is not a merge.** Nothing is merged, rebased, reset, stashed, pushed or
  deleted for you. You read the candidates and take what you want.
* **It selects top-level tasks of the recipe you are running**, by exact name,
  and only ones with a working directory of their own: agent tasks and
  `tool:bash`. A `tool:hitl` or `tool:conduit` step is refused with its name. A
  same-named task inside a nested conduit is unaffected — that child is its own
  run and makes its own choice.
* **It refuses a dirty source.** Uncommitted changes to tracked files are named
  and the run stops, because they would not be in the checkouts and starting
  agents against a base that silently omits your work in progress is worse than
  saying so. Untracked files do not block anything.
* **Each task starts at its checkout's root**, even when you ran `atelier` from
  a subdirectory. Running inside a linked worktree of your own works too: that
  checkout's HEAD is the base.
* **The cost is a checkout per task.** For a large repository that is real disk
  and real time before the first prompt; `status` shows the paths so you can
  measure it with `du -sh`. The Git object store is shared, so the cost is the
  working files, not the history.
