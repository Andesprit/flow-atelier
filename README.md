![flow-atelier](https://raw.githubusercontent.com/Andesprit/flow-atelier/main/atelier.jpg)

# Flow Atelier

**Flow Atelier lets AI coding agents work together, and hand work to each
other, the same way every time, on your machine.**

## Why it is useful

- **Write it once, repeat it.** A job is one short YAML file, called a
  conduit. A step is a shell command, an AI coding agent (Claude Code, Codex,
  Gemini CLI, opencode and other agents that speak ACP), or a question for
  you. Steps run in parallel, loop until tests pass, and every run is saved
  to disk.
- **Agents that work together and delegate safely.** One agent plans, another
  builds, a third reviews. One agent can answer another's questions. An agent
  in one app can hand a task to another with `atelier ask`, at a permission
  level you choose.
- **You stay in control.** Approval gates, a live dashboard, schedules, and
  you can stop a run or resume a failed one. It runs on your machine with the
  agent logins you already have. Flow Atelier stores and proxies no API keys.

## How to use it

```bash
# 1. Install
curl -fsSL https://raw.githubusercontent.com/Andesprit/flow-atelier/main/install.sh | bash

# 2. Set up a project: writes starter conduits for the agents it finds
cd your-project
atelier init

# 3. Run
atelier run pair    # one agent drafts a change, another reviews it
atelier serve       # the live dashboard on http://127.0.0.1:8000

# Or hand one task to another agent
atelier ask "Review the last commit" --path . --harness codex --permissions read-only
```

If Atelier cannot enforce the permission level you ask for, the run fails
instead of ignoring the level. The exit code tells the caller what happened:
`0` done, `1` failed, `143` stopped.

No agent installed yet? `atelier run hello --input name=world` runs a shell
step only. Windows and PyPI installs are in the [guide](https://github.com/Andesprit/flow-atelier/blob/main/docs/guide.md#install).

## A conduit

```yaml
name: ship
description: Codex plans, Claude builds until the tests pass, then you approve
inputs:
  goal: What to build
tasks:
  - plan:
      description: Codex writes the plan
      tool: harness:codex
      task: "Plan how to reach this goal: {{inputs.goal}}"
  - build_until_green:
      description: Build and test, up to 5 times, until the tests pass
      tool: tool:conduit
      depends_on: [plan]
      repeat: 5
      until: output.match(TESTS PASSED)
      on_exhaust: fail
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
            description: Run the tests
            tool: tool:bash
            depends_on: [build]
            task: "make test && echo 'TESTS PASSED'"
  - approve:
      description: You decide
      tool: tool:hitl
      task: "Tests pass. Ship it?"
      depends_on: [build_until_green]
      inputs:
        answer: "Type yes to ship"
```

Save it as `.atelier/conduits/ship/conduit.yaml`, then run
`atelier run ship --input goal="add dark mode"`.

![Dashboard](https://raw.githubusercontent.com/Andesprit/flow-atelier/main/docs/img/dashboard.jpg)

## Good to know

- Agents run with full permissions by default: they can edit files and run
  commands. It is a local tool, not a hosted service. See
  [Security](https://github.com/Andesprit/flow-atelier/blob/main/docs/guide.md#security).
- Check your provider's terms before you run agents unattended:
  [Agent logins and provider terms](https://github.com/Andesprit/flow-atelier/blob/main/docs/guide.md#agent-logins-and-provider-terms).
- Everything else is in [the guide](https://github.com/Andesprit/flow-atelier/blob/main/docs/guide.md): every command, the conduit
  reference, schedules and the HTTP API. Start with
  [Your first workflow](https://github.com/Andesprit/flow-atelier/blob/main/docs/first-workflow.md) or
  [Multi-agent workflows](https://github.com/Andesprit/flow-atelier/blob/main/docs/multi-agent-workflow.md).

## Contributing and license

To contribute, see [CONTRIBUTING.md](https://github.com/Andesprit/flow-atelier/blob/main/CONTRIBUTING.md) and
[DEVELOPMENT.md](https://github.com/Andesprit/flow-atelier/blob/main/DEVELOPMENT.md). To report a security problem in private,
see [SECURITY.md](https://github.com/Andesprit/flow-atelier/blob/main/SECURITY.md). Licensed under [Apache 2.0](https://github.com/Andesprit/flow-atelier/blob/main/LICENSE).
