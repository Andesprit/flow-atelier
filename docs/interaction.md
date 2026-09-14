# Conduit interaction policies

Add an `interaction` block to choose who answers harness questions and who
approves tool operations. The designer exposes the same settings in its conduit
panel. The policy applies to all harness tasks in this conduit; it enables their
interactive reply loop even when a task omits `interactive: true`.

```yaml
name: supervised_build
description: Implement a feature using existing project conventions
interaction:
  questions: hybrid
  permissions: approve_all
  supervisor:
    tool: harness:codex
    instructions: |
      Make implementation decisions using the task and existing conventions.
      Ask the human about scope changes and unspecified product preferences.
    max_replies: 8
    timeout: 120
    max_context_chars: 200000
tasks:
  - implement:
      description: Add pagination
      task: Add pagination following the project's existing conventions.
      tool: harness:claude-code
```

| Setting | Choices | Default within `interaction` |
| --- | --- | --- |
| `questions` | `human`, `supervisor`, `hybrid` | `human` |
| `permissions` | `approve_all`, `human`, `supervisor`, `hybrid` | `approve_all` |

- `human`: route every request to terminal input or the WebSocket client.
- `supervisor`: the configured harness always decides. Failure, invalid output,
  or an exhausted budget fails the task; it never falls back to a human.
- `hybrid`: the supervisor answers or explicitly escalates. Supervisor failures
  and budget limits also escalate to the human input channel.
- `approve_all`: automatically select an available allow option for tool
  permissions, preferring `allow_always`. This does not answer questions.

A supervisor is required when either setting uses `supervisor` or `hybrid`.
`supervisor.tool` names any registered `harness:<name>`; readiness checks include
that harness. Invalid policies and unknown fields in the policy are rejected.
There are no task-level policy overrides. Nested conduits use their own policy;
`tool:hitl` gates continue to require human answers.

## Compatibility

Omitting `interaction` (or setting it to `null`) preserves existing behavior:
tasks opt into human conversations with `interactive: true`, other harness tasks
run one turn, and tool permission requests are automatically allowed. An explicit
`interaction: {questions: human}` enables human conversations for every harness
task in the conduit. PATCH accepts `interaction: null` to remove a policy;
omitting the field preserves the saved policy.

## Context and decisions

Each decision runs a separate supervisor session through the existing harness
executor, using its installed CLI and login. It receives the conduit description,
task definition, resolved inputs, every prompt sent to the worker, the complete
ACP session updates exposed by the worker, and previous human/supervisor
answers and escalations. Permission decisions also receive the full current
tool request and exact offered option ids. Private harness information that is
not exposed through ACP cannot be included.

The supervisor returns a validated JSON answer or escalation. For permissions it
must select an offered option id or `cancel`; choosing a rejection option sends
that rejection back to the worker. Invalid human permission input fails the task
without granting the operation. The supervisor is instructed not to use tools;
its own ACP permission requests are denied, and it cannot recursively supervise
itself or request human input.

Human and supervisor answers are displayed with their source and persisted as
`interaction` steps. Final task logs include a `session` array with the full
observed conversation, including supervisor usage when reported. The normal task
`usage` field continues to describe the worker; supervisor usage is separate.
Task outputs remain worker output, with only the final worker turn passed to
downstream tasks.

## Limits and permissions

`max_replies` bounds supervisor calls per task execution attempt across questions
and permissions together. `timeout` bounds each supervisor call; the task timeout
also bounds the complete interaction, including human waits. Repeated task
iterations and retries start new sessions and budgets. Worker conversations
retain the existing overall interactive-turn limit.

`max_context_chars` bounds the serialized supervisor prompt. Exceeding it fails
in supervisor mode or escalates in hybrid mode. History is never silently
truncated; automatic context compaction is not implemented. A disconnected or
unavailable human input channel follows the existing input failure/timeout
behavior. Resume restarts an unfinished task, not its suspended ACP session.

For permission modes other than `approve_all`, Atelier does not enable a bypass
mode. If the harness starts in a recognized bypass mode, Atelier switches to an
advertised default/normal/ask/code mode or fails if none exists. Permission
routing governs requests actually exposed by the harness over ACP; it is not an
OS sandbox and cannot intercept operations that the harness executes without
requesting permission.
