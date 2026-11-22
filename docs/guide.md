# Flow Atelier guide

[Read the Flow Atelier documentation](https://flow-atelier.andesprit.com/docs/). The links below take you to each topic.

## How a conduit runs

[Read this section](getting-started/how-a-conduit-runs.md)

## Install

[Read this section](getting-started/install.md)

### One-command install (no Python needed)

[Read this section](getting-started/install.md#one-command-install-no-python-needed)

### Install with uv (for Python users)

[Read this section](getting-started/install.md#install-with-uv-for-python-users)

### Optional: AI harnesses

[Read this section](configuration/agent-logins.md#optional-ai-harnesses)

#### Picking a model

[Read this section](configuration/agent-logins.md#picking-a-model)

#### Picking a reasoning effort

[Read this section](configuration/agent-logins.md#picking-a-reasoning-effort)

#### Checking a harness before you use it

[Read this section](configuration/agent-logins.md#checking-a-harness-before-you-use-it)

## Quickstart

[Read this section](getting-started/quickstart.md)

### Your first AI workflow: review what you staged

[Read this section](getting-started/quickstart.md#your-first-ai-workflow-review-what-you-staged)

### Your first multi-agent workflow: chain or panel

[Read this section](getting-started/quickstart.md#your-first-multi-agent-workflow-chain-or-panel)

### Running one workflow with different agents

[Read this section](getting-started/quickstart.md#running-one-workflow-with-different-agents)

### Checking the whole agent team before a run

[Read this section](getting-started/quickstart.md#checking-the-whole-agent-team-before-a-run)

### Two coding agents changing the same code at once

[Read this section](getting-started/quickstart.md#two-coding-agents-changing-the-same-code-at-once)

### Recovering a failed workflow

[Read this section](recovering-a-failed-workflow.md)

### Reading a conduit before you run it

[Read this section](guides/authoring.md#reading-a-conduit-before-you-run-it)

### Passing a file as an input

[Read this section](guides/authoring.md#passing-a-file-as-an-input)

### Writing a conduit with your editor's help

[Read this section](guides/authoring.md#writing-a-conduit-with-your-editors-help)

### A field Atelier does not know is an error

[Read this section](guides/validation.md#a-field-atelier-does-not-know-is-an-error)

### Reading JSON command errors

[Read this section](guides/validation.md#reading-json-command-errors)

### Checking conduits from a script or an agent

[Read this section](guides/validation.md#checking-conduits-from-a-script-or-an-agent)

### Checking a workflow that calls other workflows

[Read this section](guides/validation.md#checking-a-workflow-that-calls-other-workflows)

#### The call's arguments, not only the child's name

[Read this section](guides/validation.md#the-calls-arguments-not-only-the-childs-name)

### Waiting for a run from another terminal or agent

[Read this section](guides/authoring.md#waiting-for-a-run-from-another-terminal-or-agent)

## Examples

[Read this section](guides/authoring.md#examples)

### A simple conduit (`hello`)

[Read this section](guides/authoring.md#a-simple-conduit-hello)

### A bigger conduit (`deploy_pipeline`)

[Read this section](guides/authoring.md#a-bigger-conduit-deploy_pipeline)

## Conduit reference

[Read this section](reference/conduit-yaml.md)

### Templating

[Read this section](reference/conduit-yaml.md#templating)

#### Values in shell steps

[Read this section](reference/conduit-yaml.md#values-in-shell-steps)

### Conditional dependencies

[Read this section](reference/conduit-yaml.md#conditional-dependencies)

### Loops (`repeat` + `until` / `while`)

[Read this section](reference/conduit-yaml.md#loops-repeat--until--while)

### Retries and per-task timeout

[Read this section](reference/conduit-yaml.md#retries-and-per-task-timeout)

### When an agent runs out of usage

[Read this section](reference/conduit-yaml.md#when-an-agent-runs-out-of-usage)

### Continuing an agent's conversation (`session`)

[Read this section](reference/conduit-yaml.md#continuing-an-agents-conversation-session)

### Asking a human (`tool:hitl`)

[Read this section](reference/conduit-yaml.md#asking-a-human-toolhitl)

#### Approving an exact patch

[Read this section](reference/conduit-yaml.md#approving-an-exact-patch)

### Long AI conversations (`interactive: true`)

[Read this section](reference/conduit-yaml.md#long-ai-conversations-interactive-true)

#### Conduit-wide human and supervisor policies

[Read this section](reference/conduit-yaml.md#conduit-wide-human-and-supervisor-policies)

#### Interactive turns over the WebSocket

[Read this section](reference/serving.md#interactive-turns-over-the-websocket)

## Where conduits live

[Read this section](configuration/locations.md)

### Dashboard chat

[Read this section](guides/dashboard.md#dashboard-chat)

## Commands

[Command overview](reference/command-overview.md) · [CLI reference](https://flow-atelier.andesprit.com/docs/reference/cli/)

## Installing conduit packages

[Read this section](guides/packages.md)

## Running on a schedule

[Read this section](guides/schedules.md)

## HTTP API (`atelier serve`)

[Server and WebSocket behavior](reference/serving.md) · [HTTP API reference](https://flow-atelier.andesprit.com/docs/reference/http-api/)

## Running unattended

[Read this section](guides/schedules.md#running-unattended)

## Agent logins and provider terms

[Read this section](configuration/agent-logins.md)

## Security

[Read this section](configuration/security.md)

## Folder layout

[Read this section](configuration/locations.md#folder-layout)

## Install the platform recipe bundle

[Read this section](guides/packages.md#install-the-platform-recipe-bundle)

### Idea to product

[Read this section](guides/packages.md#idea-to-product)

## Send a WhatsApp run summary

[Read this section](guides/whatsapp.md)
