# Conduit reference

A conduit has a name, description, and list of tasks. See [Authoring conduits](../guides/authoring.md#examples) for YAML examples and [Validating conduits](../guides/validation.md) for checks you can run before execution.

## Conduit fields

| Field | Required or default | Purpose |
| --- | --- | --- |
| `name` | Required | The conduit name. Use letters, digits, underscores, and hyphens. Match the folder name. |
| `description` | Required | A short description of the conduit. |
| `tasks` | Required | The list of task definitions. |
| `inputs` | Empty map | Named inputs for the run. Use a string for a description or an object with `description` and `default`. An input with no default is required. |
| `timeout` | `3600` seconds | Default timeout per task. |
| `max_concurrency` | `3` | Maximum tasks that can run at once in this conduit. |
| `interaction` | Unset | [Question and permission policies](../interaction.md) for agent tasks. |

[Runtime settings](../configuration/settings.md#project-settings) can change
the defaults for `timeout` and `max_concurrency`.

## Task fields

Use a `name:` field or the single-key shorthand shown in the examples.

| Field | Required or default | Purpose |
| --- | --- | --- |
| `name` | Required | A unique task name using letters, digits, and underscores. |
| `description` | Required | A short description of the task. |
| `task` | Required unless the task has an inline `tasks` list | A shell command, agent prompt, human prompt, or called conduit name, depending on the tool. |
| `tool` | Required | The executor. Common choices are `tool:bash`, `tool:hitl`, `tool:conduit`, and `harness:<name>[:<model>[:<effort>]]`. |
| `depends_on` | Empty list | Task names or [conditions](#conditional-dependencies) that must be satisfied before this task runs. |
| `inputs` | Empty map | Named answers for `tool:hitl` or input bindings for `tool:conduit`. |
| `tasks` | Unset | An inline body for `tool:conduit`. |
| `repeat` | `1` | Maximum loop iterations. |
| `until`, `while` | Unset | One condition that checks loop output. Requires `repeat > 1`. |
| `on_exhaust` | `complete` | Use `fail` if exhausting a loop with an unmet condition should fail the task. |
| `on_failed_try` | `retry` | Use `fail` to stop a conditional loop on the first failed try. |
| `stagnation_limit` | Unset | Number of identical consecutive loop outputs that causes failure. |
| `retries` | `0` | Additional attempts after a task fails. |
| `retry_backoff` | `0` seconds | Delay before a retry. |
| `timeout` | Inherits the conduit timeout | Per-task timeout in seconds. |
| `interactive` | `false` | Enable multiple agent turns. |
| `session` | Unset | Agent session name to carry between turns. |
| `permissions` | Inherits the runtime permission setting | Permission level for this agent task. |
| `different_harness_from` | Unset | Upstream agent task that must use a different harness. |
| `deny_cloud_config` | `false` | Deny access to local cloud configuration for a shell or agent task. |

## Templating

- `{{inputs.<name>}}` — a conduit input or HITL answer.
- `{{<task_name>.output}}` — the printed output of an earlier task.
The earlier task must appear in `depends_on`.
- `{{<task_name>.tool}}` — the tool that task actually ran on, so a prompt
quoting an upstream result can name the agent that produced it. It follows a
`--agent` selection, so the label stays true when a run replaces that agent.
The named task must appear in `depends_on`.
- `{{loop.previous}}` — this task's output from its previous loop
iteration (empty before the first iteration completes). When that try
failed, it starts with `[this try failed with exit code N]`, followed by
the try's output and its error text. Only valid on a looping task
(`repeat > 1`).
- `{{loop.history}}` — every prior iteration of this task, rendered as
numbered blocks. Only valid on a looping task (`repeat > 1`).

A missing `{{inputs.x}}` fails the task immediately; a reference to a
task that was skipped or hasn't completed skips the referencing task.
`atelier run` rejects an `--input` or `--input-file` key the conduit
neither declares nor references, so a mistyped key fails before the run
starts.

A declared input without a default is required. In a terminal, `atelier
run` asks for each one you left out. Without a terminal (a script, an
agent, CI) it exits 2 before the run starts, lists every missing input
with its description and prints only the flags to add, such as
`--input 'destination=<value>'`. Replace each `<value>` and append the
flags to the command you just ran; your other inputs and options stay as
you wrote them. The message never repeats the values you supplied.

### Values in shell steps

In a `tool:bash` step, read values from environment variables, not from
`{{...}}`. Atelier puts a `{{...}}` value into the command text before bash
reads it, so bash runs any `$(...)`, `;` or backtick in the value, even
inside quotes. An input, a human answer or an agent's output can then run
commands on your machine. Bash never runs what is inside a variable. Keep the
variable in double quotes, so spaces and `*` in the value stay as they are:

```yaml
- greet:
    description: greet someone
    tool: tool:bash
    task: echo "hello $INPUT_NAME"
```

Every shell step gets these variables on top of the normal environment:

- `INPUT_<NAME>` for each input, the value of `{{inputs.<name>}}`.
- `OUTPUT_<TASK>` for each finished task in the step's `depends_on` chain,
  the value of `{{<task>.output}}`.
- `LOOP_ITERATION` (1, 2, ...), `LOOP_PREVIOUS` and `LOOP_HISTORY` in a
  looping task (`repeat > 1`), the values of `{{loop.*}}`.

These replace any variable of the same name already in your environment;
every other variable passes through as before. Up to 0.12 Atelier set none of
them, so a step that read, say, `$INPUT_TOKEN` from your environment now gets
the `token` input instead when the conduit has one. A variable holds the value
exactly, so `$OUTPUT_<TASK>` keeps the newline that `echo` printed at the
end: an unquoted `{{<task>.output}}` only seemed to drop it, because bash
read that newline as the end of the command.

A step inside a `tool:conduit` loop body does not loop itself, so it gets no
`LOOP_*` and no `OUTPUT_<TASK>` of the enclosing conduit: forward them under
the calling task's `inputs:` (`feedback: '{{loop.previous}}'`) and read
`$INPUT_FEEDBACK`. A variable a step does not get is empty, and `atelier
check` warns about it.

The name is in upper case, and every character that is not a letter, a digit
or `_` becomes `_`, so input `repo-url` is `$INPUT_REPO_URL`. If two names
give the same variable, or one name's variable is the other's `_FILE` form
(`report` and `report_file`), the name that comes first in sorted order keeps
it, and the other value is only reachable through `{{...}}`. Declare each run
input the step reads under `inputs:`, so `atelier run` accepts it. A
`tool:hitl` answer needs no declaration: `$INPUT_<NAME>` holds it once the
human answers, and declaring it makes the run ask for it before it starts, or
stop when no one is at the terminal.

Each value is also written to a file, and `<NAME>_FILE` holds its path, for
example `cat "$INPUT_NAME_FILE"`. The plain variables of one step hold at most
100,000 bytes in total, one budget shared by all its values, so the programs
the step starts keep their room for arguments. Values the command names come
first, then the others in name order. A value that does not fit, or that has
a NUL byte or a broken character, gets only the file form and leaves
`<NAME>` unset, so read the file when a value can be large, for example
`< "$OUTPUT_<TASK>_FILE"`. When the command names a variable that is left unset
this way, the step's error output starts with a line that says so. The files
are removed when the step ends.

A command up to 32,000 bytes runs as `bash -c`, as before. A bigger one runs
from a temporary script file, so it no longer fails with "argument list too
long"; there `$0` names that file, which is gone when the step ends. Use
`"$BASH"`, not `$SHELL`, to start the running bash again.

`{{...}}` still works in shell steps as before. `atelier check` warns when a
shell step puts `{{inputs.*}}`, `{{<task>.output}}` or `{{loop.*}}` straight
into its command, and names the variable to use instead. `{{conduit_dir}}`
and `{{<task>.workspace}}` are paths you control, so they are fine to use.

## Conditional dependencies

```
<task>.output.match(<regex>)        # dependency met if regex matches
<task>.output.not_match(<regex>)    # dependency met if regex does NOT match
```

The regex is everything between the leftmost `(` and the last `)`.
Python's `re.search` is used.

Quotes around the regex are optional and stripped when present, so
`output.match(PASS)` and `output.match("PASS")` behave identically. To match a
literal quote character, escape it — `output.match(\"PASS\")` looks for `"PASS"`
*with* the quotes.

If a condition is not met, the task is **skipped**, not failed.
Anything that depends on a skipped task is also skipped.

## Loops (`repeat` + `until` / `while`)

A task with `repeat > 1` can break out of its loop early:

```
until: output.match(<regex>)       # break as soon as an output matches
until: output.not_match(<regex>)   # break as soon as no output matches
while: output.match(<regex>)       # loop while an output matches; break otherwise
while: output.not_match(<regex>)   # loop while no output matches; break otherwise
```

Set at most one of `until` / `while`. The first iteration always runs
before the predicate is checked.

`stagnation_limit: <n>` (n >= 2, needs `repeat > 1`) fails the task with
`stagnated: n identical consecutive outputs` when n tries in a row leave the
same text, which is what `{{loop.previous}}` shows for each try. It works with
a plain `repeat` as well as with `until` / `while`, and it fails the task even
with `on_exhaust: complete`, so its dependents do not run.

A try that fails does not end a loop that has `until` or `while`. When a
try still exits nonzero after its `retries`, `until` and `while` are not
checked on it: the try counts toward `repeat`, and the next try starts. If
the last try still fails, the task fails, whatever `on_exhaust` says. The
next try sees what went wrong through
`{{loop.previous}}`: a first line `[this try failed with exit code N]`, then
the try's output, then its error text. For a shell step that is its stdout
and stderr. For a `tool:conduit` body it is the output of the body task that
failed (and its stderr, when that task is a shell step), then a line that
names that task and its error. The exit code is then the body's as a whole,
usually 1, not the code of the task that failed. That text is raw: whatever
the command or the agent printed. A shell step must read it as
`"$LOOP_PREVIOUS"`, or as `"$INPUT_<NAME>"` when a loop body gets it
forwarded, never through `{{loop.previous}}` in the command, where any `$(...)`
or backtick in it would run (see [Values in shell steps](#values-in-shell-steps)).
After a failed try,
`{{loop.previous}}` holds that failure text, not the last good output, so a
loop that carries state from try to try (a counter, say) should read it from
`{{loop.history}}`, which keeps the earlier tries too (the last 10 by
default).

Up to 0.12, a failed try ended the loop at once and failed the run. Now the
loop goes on and the next try sees the failure. A loop whose last try still
fails fails the run as before, so a red test loop still stops its dependents.

Set `on_failed_try: fail` on a loop with `until` or `while` to end it at the
first try that still fails after its `retries`, as every loop did up to 0.12.
The task fails with that try's exit code and error, its dependents do not run,
and `atelier run --resume <flow-id>` goes on in the same try. Use it for a loop
that walks through stages, such as a work queue or a long goal run, where a
failure means something is broken and the next pass would only fail again. The
default, `on_failed_try: retry`, is the rule above.

```yaml
- work_the_queue:
    tool: tool:conduit
    task: next_item
    repeat: 100
    until: output.match(^QUEUE_END$)
    on_exhaust: fail
    on_failed_try: fail   # an item that fails stops the queue
```

Some failures still end the loop. With a plain `repeat` and no `until` or
`while`, the first failure fails the task. A `tool:hitl` step that gets no
answer fails at once, also inside a `tool:conduit` body, so the steps after
the loop never run without that answer. A `tool:conduit` body that cannot
start, for example because it misses a required input, also fails at once.
An agent that runs out of usage waits for the reset, as described below, and
never spends a try. `stagnation_limit` counts failed tries too, so the same
failure several times in a row can fail the task.

For a loop with several tasks, put `tasks:` on its `tool:conduit` step. Each
iteration runs the body as a child flow; the predicate sees **every body task's
output from that iteration** and fires on any match. The body receives only
the values forwarded by the calling task's `inputs:` map. `{{conduit_dir}}`
still points at the directory containing the one file.

```yaml
- retry_while_rate_limited:
    tool: tool:bash
    task: 'curl -s -o body -w "%{http_code}" https://api/x'
    repeat: 10
    while: output.match(^429$)

- run_until_test_passes:
    tool: tool:conduit
    description: fix then test
    repeat: 5
    until: output.match(PASS)
    on_exhaust: fail
    inputs:
      goal: "{{inputs.goal}}"
      feedback: "{{loop.previous}}"
    tasks:
      - fix:
          description: repair from the previous test result
          tool: harness:codex
          task: "Fix {{inputs.goal}}. Previous test: {{inputs.feedback}}"
      - test:
          description: check the repair
          tool: tool:bash
          depends_on: [fix]
          task: make test
```

For a body reused by other workflows, put its tasks in a separate conduit and
use `task: build_and_test` instead of the inline `tasks:` list. Do not set
both fields on one task. `atelier check` validates an inline body's tasks and
dependencies even without `--recursive`; use `--recursive` to follow named
children and to include the body in `--probe`.

Without `on_exhaust: fail`, a loop that reaches its limit without meeting
`until` or `while` still completes and its dependents receive the last output,
as long as that last try exited 0. If the last try still failed, the task fails
whatever `on_exhaust` says, and the saved reason says how the last try failed,
with its exit code and error.
`atelier check` and `atelier plan` warn when such a loop has dependents. A run,
`atelier status`, and `atelier diagnose` report the unmet condition, iteration
count, and which dependent tasks ran. Set `on_exhaust: fail` on that task when
an unmet condition must stop the workflow; raise `repeat` when more attempts
are appropriate. The default remains `complete` for existing recipes.

If an agent fails inside a `tool:conduit` loop with `until` or `while`, that
try fails and the next one starts a fresh body. When the loop still fails, for
example after its last try with `on_exhaust: fail`, the parent run and
`diagnose` name the child task, agent, and loop iteration. Resume the parent
flow with `atelier run --resume <parent-flow-id>`, or replace that child agent
for the remaining work with `atelier run --resume <parent-flow-id> --agent
<loop-task>.<child-task>=<harness>`. Use `atelier logs <child-flow-id> --show
all` for the raw error and Python traceback.

## Retries and per-task timeout

- `retries: <n>` — if a task *fails*, re-run it up to `n` more times
(default `0`). This is different from `repeat`, which runs a task
several times. In a loop with `until` or `while`, a try uses its
retries first, and only then counts as a failed try.
- `timeout: <seconds>` — override the per-task time limit for one task.
When omitted, the conduit-level `timeout` applies.

A shell task that times out or is cancelled stops with the processes it
started: its process group, or its process tree on Windows. A task is
cancelled when another task fails, when you press Ctrl-C, or when you run
`atelier stop`. A process that leaves the group, for example through
`setsid` or a tool that turns itself into a daemon, is not stopped. On
Windows, a process whose parent has already exited is no longer part of the
tree and keeps running too, for example a background job left by a shell
that finished, or the child of `( cmd & )`. The tests check this stop on
Linux and macOS only.

A shell task waits until every process it started has closed the task's
output. To leave a server running after the task ends, send both of its
output streams elsewhere, for example `server > server.log 2>&1 &`.
Otherwise the task waits for it until its timeout, then fails and stops it;
the timeout message then says that a background process held the output.
`nohup` and `disown` do not change this.

## When an agent runs out of usage

A subscription agent that hits its 5-hour or weekly limit refuses the
turn. The run does not fail. The task waits until the limit resets, then
tries the same turn again, and the wait spends none of its `retries`.
`atelier status` shows the task as running with the time it waits for,
and `atelier stop` ends the wait like any running task.

The reset time comes from the agent. Claude Code reports it directly;
Codex and other agents say it in their error message ("try again in 2
hours"). When the agent does not say, the task tries again every 30
minutes. A loop body run as a nested conduit fails on the limit, and the
top-level run waits and then resumes it where it stopped, so the nested
task's own `timeout` never cuts a wait short.

A task waits at most 6 hours in total. Set `ATELIER_USAGE_LIMIT_MAX_WAIT`
to change that in seconds, or to `0` to fail at once. A limit that resets
after the budget fails the task with the reset time, and
`atelier run --resume` picks it up after the reset.

## Continuing an agent's conversation (`session`)

Every agent task normally opens a new conversation, so the agent reads
the project again each time. Give two tasks the same `session` name and
the later one becomes the next turn of the earlier one's conversation:

```yaml
- build:
    description: Implement the change
    task: Implement {{inputs.goal}}
    tool: harness:claude-code
    session: worker
- review:
    description: Review what was built
    task: Review the last commit and list what must be fixed.
    tool: harness:codex
    depends_on: [build]
- repair:
    description: Fix what the review found
    task: "The reviewer found:\n{{review.output}}\nFix these."
    tool: harness:claude-code
    session: worker
    depends_on: [review]
```

`repair` starts with everything `build` already knows, so it only has to
read the findings.

- Only `harness:` tasks can name a session, and a session belongs to the
  agent that opened it: a task on another agent that uses the same name
  starts its own conversation.
- The name is a template. `session: "fix-{{inputs.ticket}}"` starts a new
  conversation when the ticket changes, which is how a recipe keeps one
  conversation from growing forever.
- Sessions belong to the whole run. A looping task continues its own
  session on each iteration, a retry continues the session of the attempt
  that failed or timed out, `--resume` keeps them, and a conduit that a
  parent runs again and again finds the session its last run left.
- Tasks that share a session take turns, even when they could run in
  parallel.
- If the agent cannot reload the session, or no longer has it, the task
  runs in a new one and a warning says why. Each log entry of such a task
  records `session` and `session_continued`.

## Asking a human (`tool:hitl`)

A `tool:hitl` task declares its own `inputs: {name: description}`
map. At runtime flow-atelier prints the prompt, asks for each input
by name on the terminal, and saves the answers so downstream tasks
can use them as `{{inputs.<name>}}`.

A gate finishes once it has its answers, whatever they are, so a step with a
plain `depends_on: [approve]` runs after `no` or an empty answer too. To
make it an approval, depend on the answer. The gate's output lists each
answer as YAML, `confirm: 'yes'`:

```yaml
  - deploy:
      task: make deploy
      tool: tool:bash
      depends_on:
        - "approve.output.match((?m)^confirm: 'yes'$)"
```

Any other answer skips `deploy`. `atelier check` warns about a step that
depends on a gate named for an approval (its name or an input mentions
approve or confirm) without such a condition.

### Approving an exact patch

Suppose `release.patch` adds one new file to a Git worktree. Run this shell
command there in a step named `preview` before prompting for approval.
Git stores the proposed bytes and returns their digest.

```bash
digest=$(git hash-object -w --no-filters release.patch) && printf '%s' "$digest"
```

Make `approve` depend on `preview`. Set its prompt to
`Review git cat-file -p {{preview.output}} before answering.` Run that
command in the worktree to inspect the exact patch. Keep the affirmative
dependency on `deploy` shown above. For this local deployment, use the
following shell command in the same worktree.

```bash
set -eo pipefail
current=$(git hash-object --no-filters release.patch)
if [[ "$current" != "$OUTPUT_PREVIEW" ]]; then
  echo 'Patch changed since approval. Start a new run.' >&2
  exit 1
fi
if git cat-file blob "$current" | git apply --reverse --check - 2>/dev/null; then
  exit 0
fi
git cat-file blob "$current" | git apply -
```

The apply reads the verified blob. If a run stops just after creating the
file, the reverse check recognizes the applied patch on resume. For a
remote deployment, use the approved digest and target as the deployment
service's idempotency key so the service can recognize a repeated request.

## Long AI conversations (`interactive: true`)

When a harness task sets `interactive: true`, flow-atelier appends
this line to every message it sends to the AI:

> When — and only when — you are completely finished, output the exact
> token `[ATELIER_DONE]` to signal completion.

Then it keeps the conversation open: the AI replies, flow-atelier
streams the reply to your terminal, and if the AI didn't write
`[ATELIER_DONE]` yet, flow-atelier asks **you** for the next message
to send back. The loop ends when `[ATELIER_DONE]` shows up.

How that next message reaches flow-atelier depends on where the run
started. On the terminal it reads one line from stdin — typed at the
`› ` cursor, or piped in for scripted runs. Under `atelier serve` the
same conversation travels over `/ws/run-conduit`: the agent's prose is
streamed to the client as it is written, and the client sends the reply
back on the same socket.

Tool permission requests are automatically approved by default. Set
`permissions` to limit them (see [Security](../configuration/security.md)), or add a
conduit-level `interaction` policy to choose human or supervisor decisions.

Non-interactive tasks run one turn and stop.

For a direct interactive agent conversation without writing a conduit,
use `atelier ask` (Claude Code by default; `--harness <name>` picks
another agent from `atelier list harnesses`):

```bash
atelier ask "Help me write a specification" --path /absolute/path/to/project
```

`--path` is required and becomes Claude's working directory. Claude's
questions are read from stdin just like any other interactive harness task.
Atelier records the conversation in the global history for that working directory. The agent runs with full permissions unless
`--permissions edits` or `--permissions read-only` sets another level, or
`ATELIER_AGENT_PERMISSIONS` (in the environment or in `.env`) does; see
[Security](../configuration/security.md).

```bash
atelier ask "Review the last commit" --path . --harness codex --permissions read-only
```

`atelier run` and `atelier ask` exit with 0 when the run completed, 1 when it
failed, and 143 when `atelier stop` or another SIGTERM stopped it.
`atelier stop` exits with 1 when the flow is still running after its 15
second wait.

### Conduit-wide human and supervisor policies

Choose the behavior once for every harness task in a conduit:

```yaml
interaction:
  questions: hybrid       # human | supervisor | hybrid
  permissions: approve_all # approve_all | human | supervisor | hybrid
  supervisor:
    tool: harness:codex
    instructions: |
      Follow existing project conventions.
      Escalate scope changes and unspecified product preferences.
```

`human` always asks you. `supervisor` always delegates and fails if the
supervisor cannot decide. `hybrid` lets it answer or escalate to you.
`approve_all` automatically allows tool permissions. The supervisor receives
the full worker session exposed through ACP, including prior replies and tool
activity. Answers are attributed and recorded.

Adding `interaction` enables conversations for all harness tasks. Omitting it
preserves existing task-level `interactive` behavior and automatic permissions.
The designer exposes these controls in the conduit panel. Explicit `tool:hitl`
gates remain human; nested conduits use their own policy.

See [interaction policies](../interaction.md) for limits, failure handling,
permission boundaries, and the complete configuration.
