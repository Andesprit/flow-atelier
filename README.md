![flow-atelier](https://raw.githubusercontent.com/Andesprit/flow-atelier/main/atelier.jpg)

# Flow Atelier

Run a shell command, hand a job to a coding agent, or save a repeatable workflow
in YAML. Each run keeps its inputs, task results and logs on your machine.

## First run

In your project directory on macOS (Apple Silicon) or Linux (x86_64), install
Atelier and add it to the current shell. Windows and Python installs are in the
[install guide](https://github.com/Andesprit/flow-atelier/blob/main/docs/guide.md#install).

```bash
curl -fsSL https://raw.githubusercontent.com/Andesprit/flow-atelier/main/install.sh | bash
export PATH="$HOME/.atelier/bin:$PATH"
```

```bash
atelier init
atelier run hello --input name=world
```

Look for `hello world` and the final `flow_id:` line. This step uses Bash and
needs no AI account. A **conduit** is the saved recipe. A **flow** is one run.

## Try an agent

For a single assignment, start with one agent. In a Git project with Codex
and its code-review skill installed, ask for a result you can check.

```bash
atelier ask "Use your code-review skill on the last commit. Give one regression with file, line and a way to reproduce it, or say no finding." --path . --harness codex --permissions read-only
```

If `init` found an installed agent CLI, it also created `pair` for proposals
with independent review. In one terminal, open the dashboard for this project.

```bash
atelier serve --no-scheduler
```

In a second terminal in the same directory, check the agents can start, then
ask for a proposal. Follow the run-page link to see the draft, review and revision.

```bash
atelier check pair --probe && atelier run pair
```

## Choose the smallest workflow

Give one agent its skills, a goal and an acceptance check. A task owns one
checkable result. Split for clean context, parallel work, an independent test
or review, or a different permission level or agent.

| What you need | Where to start |
| --- | --- |
| One answer now, or the same check every week | [Ask one read-only agent or save a one-step conduit](https://github.com/Andesprit/flow-atelier/blob/main/docs/first-workflow.md) |
| A code change with a reproducible test | [Plan and build in one task, run a shell test, then review the diff](https://github.com/Andesprit/flow-atelier/blob/main/docs/loop-agents-graph.md) |
| A decision backed by evidence | [Have a second reader check each claim against its source](https://github.com/Andesprit/flow-atelier/blob/main/docs/multi-agent-workflow.md) |
| Independent reviews of one commit | [Run a read-only panel and combine its findings](https://github.com/Andesprit/flow-atelier/blob/main/docs/multi-agent-workflow.md#review-one-commit-in-parallel) |
| Two competing implementations | [Run the same fixture in each writer’s checkout](https://github.com/Andesprit/flow-atelier/blob/main/docs/parallel-agents-in-separate-checkouts.md) |

![Dashboard](https://raw.githubusercontent.com/Andesprit/flow-atelier/main/docs/img/dashboard.jpg)

Agents run with full permissions by default. The read-only examples request a
restricted mode explicitly. See [security and agent permissions](https://github.com/Andesprit/flow-atelier/blob/main/docs/guide.md#security)
and the [command reference](https://github.com/Andesprit/flow-atelier/blob/main/docs/guide.md#commands).

## Contributing and license

To contribute, see [CONTRIBUTING.md](https://github.com/Andesprit/flow-atelier/blob/main/CONTRIBUTING.md) and
[DEVELOPMENT.md](https://github.com/Andesprit/flow-atelier/blob/main/DEVELOPMENT.md). To report a security problem in private,
see [SECURITY.md](https://github.com/Andesprit/flow-atelier/blob/main/SECURITY.md). Licensed under [Apache 2.0](https://github.com/Andesprit/flow-atelier/blob/main/LICENSE).
