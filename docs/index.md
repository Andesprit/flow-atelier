# Flow Atelier

Flow Atelier runs repeatable workflows on your machine. A **conduit** is a
YAML recipe with steps for shell commands, coding agents, other conduits,
or human approvals. A **flow** is one run of that recipe. Atelier keeps its
inputs, results, and logs so you can inspect and resume it.

## Install

On macOS (Apple Silicon) or Linux (x86_64), run this command.

```bash
curl -fsSL https://raw.githubusercontent.com/Andesprit/flow-atelier/main/install.sh | bash
```

See [Install](getting-started/install.md) for Windows, uv, and adding
Atelier to your current shell.

## Start here

- [Quickstart](getting-started/quickstart.md) runs a Bash workflow and shows how to try an agent.
- [How a conduit runs](getting-started/how-a-conduit-runs.md) explains task order and run history.
- [Your first workflow](first-workflow.md) walks through asking one agent, saving a recipe, and recovering a run.
- [Conduit YAML](reference/conduit-yaml.md) covers inputs, dependencies, loops, and human approvals.
- The [CLI](reference/cli.md) and [HTTP API](reference/http-api.md) references list commands and endpoints.
- [Security](configuration/security.md) covers agent permissions, server access, and run logs.

![Flow Atelier dashboard showing a conduit and its run history](img/dashboard.jpg)
