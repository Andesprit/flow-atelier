![flow-atelier](./atelier.jpg)

# Flow Atelier

**Write down how a job gets done. Let AI agents do it, the same way, every time.**

Flow Atelier runs recipes, called conduits, written in one short YAML file.
Each step is a shell command, an AI coding agent such as Claude Code or Codex,
or a question for you. Steps run in parallel when they can, loop until tests
pass, and save every run to disk, so you can leave a job running overnight and
read exactly what happened in the morning.

It runs on your machine and uses the agent logins you already have. No API
keys are configured, stored, or proxied by Flow Atelier.

## Try it in a minute

```bash
curl -fsSL https://raw.githubusercontent.com/Andesprit/flow-atelier/main/install.sh | bash
cd your-project
atelier init           # finds the agents you have installed and writes starters
atelier serve          # in a second terminal: the live dashboard on :8000
atelier run explain    # your agents read the project and brief you
atelier run pair       # one agent drafts a change, another answers and reviews it
```

With Claude Code and Codex both installed, `explain` has them read the project
in parallel and then merges their answers into one brief. Without an agent,
`atelier run hello --input name=world` shows the basics.

`pair` shows agents working with each other. With both installed, Claude Code proposes a change,
Codex answers Claude's questions and reviews the proposal, and Claude revises
in the same conversation. It uses the conduit-wide
[`interaction`](docs/interaction.md) policy and a shared
[`session`](docs/guide.md#continuing-an-agents-conversation-session).
Nothing is edited; you get the final plan and diff.
Windows and PyPI installs are in the [guide](docs/guide.md#install).

## What people run with it

- **Overnight goal loops.** Give it a goal at night. An agent plans, another
  builds and fixes until the tests pass, reviewers check the result, and you
  read the report in the morning.
- **Conductor and worker.** Codex plans and reviews while Claude Code writes
  the code, each with its own login, handing work back and forth.
- **Review panels.** Two agents review the same change in parallel and a
  third merges their findings before you commit.
- **Media pipelines.** Shell steps run tools such as ffmpeg, an agent makes
  the editing calls, and you approve before the final render.
- **Plain automation.** Scheduled reports, polling with backoff, and deploys
  with a human approval gate. Atelier works with no AI at all.

## What a conduit looks like

```yaml
name: ship
description: Plan with Codex, build with Claude until tests pass, then ask me
inputs:
  goal: What to build
tasks:
  - plan:
      description: Codex writes the plan
      tool: harness:codex
      task: "Plan how to achieve this goal: {{inputs.goal}}"
      depends_on: []
  - build_until_green:
      description: build and test until green
      tool: tool:conduit
      depends_on: [plan]
      repeat: 5                          # loop up to 5 times...
      until: output.match(TESTS PASSED)  # ...until the tests pass
      on_exhaust: fail                   # still red after 5? stop here
      inputs:
        goal: "{{inputs.goal}}"
        plan: "{{plan.output}}"
        feedback: "{{loop.previous}}"
      tasks:
        - build:
            description: Claude Code makes the change
            tool: harness:claude-code
            task: "Goal: {{inputs.goal}}\nPlan: {{inputs.plan}}\nLast test run: {{inputs.feedback}}"
        - test:
            description: run the tests
            tool: tool:bash
            depends_on: [build]
            task: "make test && echo 'TESTS PASSED'"
  - approve:
      description: you decide
      tool: tool:hitl
      task: "Tests pass. Ship it?"
      depends_on: [build_until_green]
      inputs:
        answer: "Type yes to ship"
```

Save it as `.atelier/conduits/ship/conduit.yaml` and run
`atelier run ship --input goal="add dark mode"`. Swap any step's agent for one
run with `--agent build_until_green.build=codex`.

## What it looks like

The designer lays a conduit out by dependency depth, so each column is a set of
steps that can run at the same time:

![Designer](./docs/img/designer.jpg)

Runs stream to the dashboard, including the human-approval gates:

![Dashboard](./docs/img/dashboard.jpg)

## How it compares


|                   | Flow Atelier                                            | n8n / Zapier                   | Prefect / Dagster / Airflow | GitHub Actions                  |
| ----------------- | ------------------------------------------------------- | ------------------------------ | --------------------------- | ------------------------------- |
| Runs where        | your machine, local-first                               | hosted / self-hosted server    | scheduler + workers         | CI runners                      |
| AI steps          | a first-class task type, using your own agent CLI login | LLM API nodes, you supply keys | you write the client code   | you write the client code       |
| Human-in-the-loop | built in, mid-DAG, blocks the run                       | via external forms/webhooks    | not really                  | manual approval on environments |
| Workflow format   | one simple YAML file per conduit                        | JSON built in a GUI            | Python                      | YAML                            |
| Loops & retries   | `repeat` / `until` / `while` / `retries` on any task    | loop and wait nodes in the GUI | Python control flow         | matrix builds; no retry-until   |
| State             | plain files under `.atelier/`                           | a database                     | a database                  | opaque to you                   |


Pick Flow Atelier when the work is a repeatable recipe you want AI agents to
execute on your own machine, with you in the loop at the points that matter.
Pick the others when you need multi-tenant hosting, distributed workers, or
enterprise scheduling — flow-atelier deliberately does none of that.

> **Note on scope.** flow-atelier runs shell commands and AI agents on the
> machine it is installed on. It is a local developer tool, not a hosted
> multi-user service. See [Security](#security) before exposing it on a network.



## Why processes, not agents

Flow Atelier is built on a simple premise: the world doesn't run on people, it runs on processes that people execute.
Ask someone to make anything, for example: the best yogurt in the world, what a person will do is that they'll research, experiment, and produce something average. The same is true of an AI agent. But give that person a clear, step-by-step recipe — and keep refining it over time — and you can reach the best yogurt in the world.
That's what Flow Atelier does. We help you build simple, repeatable instructions for getting something done the same way every time, then let anyone improve on them. It mirrors how real businesses actually work. Coca-Cola and Pepsi don't differ because their managers, lawyers, or engineers are fundamentally different people — they differ because their processes are different.
Flow Atelier gives you the tool to design and refine those processes. The difference: instead of people executing them, AI agents do the work.

## The words we use

A few terms show up everywhere in the docs:

- **Conduit** — a recipe. An ordered set of steps written in a single
YAML file.
- **Task** — one step in that recipe. A task runs a shell command,
asks a person a question, calls an AI tool, or runs another conduit.
- **Flow** — one run of a conduit. Every run is saved to disk, so you
can always go back and see exactly what happened.
- **Harness** — an AI coding tool (Claude Code, Codex, opencode,
Copilot, Cursor) that a task can hand work to.
- **HITL** — "human in the loop": a step that pauses to ask a person a
typed question, then continues with the answer.


## Learn more

- [Your first workflow](docs/first-workflow.md): build one, break it, recover it.
- [Loops, agents, and graphs](docs/loop-agents-graph.md): the fix-until-green pattern.
- [Multi-agent workflows](docs/multi-agent-workflow.md): chains and panels.
- [Agents in separate checkouts](docs/parallel-agents-in-separate-checkouts.md): two agents editing the same code at once.
- [The guide](docs/guide.md): install options, every command, the conduit
  reference, schedules, packages, the HTTP API, and [security](docs/guide.md#security).
- [Claude Code plugins](https://github.com/Andesprit/flow-atelier-skills): skills that teach your agent to write and run conduits.

## Contributing

For test instructions, the project layout, and internal architecture
notes, see [DEVELOPMENT.md](./DEVELOPMENT.md).
