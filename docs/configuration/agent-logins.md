# Agent logins and provider terms

Flow Atelier starts each agent's own CLI with its existing login. See
[AI harnesses](#optional-ai-harnesses) for how sandboxed dashboard chat copies
and can refresh login files.

Each provider has its own terms for automated or unattended use, and for use
through third-party tools. Some consumer subscriptions restrict this. Check
your provider's current terms before you run agents unattended or on a
schedule. API keys or business plans are the route providers document for
automation.

- Anthropic (Claude Code):
  [Consumer Terms](https://www.anthropic.com/legal/consumer-terms) and
  [Commercial Terms](https://www.anthropic.com/legal/commercial-terms).
- OpenAI (Codex): [Terms of Use](https://openai.com/policies/terms-of-use).
- Google (Gemini CLI):
  [Gemini CLI terms and privacy](https://github.com/google-gemini/gemini-cli/blob/main/docs/resources/tos-privacy.md).

Other agents have their own terms. Check them the same way.

## Optional: AI harnesses

You only need the AI tools you actually plan to use. If you never use AI
in your conduits, you can skip this entire section.

Install the agent and sign in with its own CLI, then point Atelier at its
command by name or argv.

For sandboxed dashboard chat, Atelier copies an existing Codex `auth.json`
from `$CODEX_HOME` (default `~/.codex`) to `.codex/auth.json` in the chat
session's private temporary directory before starting the adapter. Claude chat
passes `CLAUDE_CODE_OAUTH_TOKEN` to the adapter when set. On Linux, if that
token is unset, Atelier copies an existing `.credentials.json` from
`$CLAUDE_CONFIG_DIR` (default `~/.claude`) to `.claude/.credentials.json` in
the same temporary directory.

After the adapter exits, Atelier writes a changed copy back only if the
temporary login is a regular file with one hard link and no symlink, its
contents parse as a JSON object, and the original login still resolves to
the same path and contains the bytes Atelier first read. Atelier removes the
temporary directory when the chat session closes. The Dictaria integration
reads the configured account's token from the OS keychain and uses it as a
bearer token for Dictaria requests.

What flow-atelier does do is run the command you selected, exactly as that
agent documents it. For agents distributed through `npx` or `uvx`, the
documented command fetches the package on first use — that is the agent's
own distribution mechanism doing its normal thing, the same as running the
command yourself in a shell. Agents distributed as a binary are never
downloaded; you install those, and flow-atelier runs what it finds on
PATH.

An AI task names its agent and nothing else:

```yaml
- review:
    description: review the diff
    task: "review the working tree and list any bugs"
    tool: harness:gemini
    depends_on: []
```

The names come from the [ACP registry](https://agentclientprotocol.com/get-started/registry),
a snapshot of which ships with flow-atelier. To see what you can type and
what already works on your machine:

```bash
atelier list harnesses           # every agent, and whether it runs here
atelier list harnesses --ready   # just the ones you can use right now
atelier harness sync           # refresh the list from the ACP registry
```

Roughly 40 agents are listed, including `harness:claude-code`,
`harness:codex`, `harness:gemini`, `harness:copilot`, `harness:cursor`,
`harness:opencode`, `harness:qwen-code`, `harness:goose` and
`harness:amp-acp`. A name is only the launch command that agent
documents; the `via` column says how it starts:

- `npx` / `uvx` — the agent's own package manager fetches it on first
run, at the version the registry pins. Needs Node.js or uv on PATH.
- `installed` — an `npx` agent you set up with `atelier harness install`.
- `binary` — you install the agent's CLI, and flow-atelier runs it from
PATH. `atelier list harnesses` names the missing binary when it isn't there.

Either way, logging in is yours to do, with that agent's own CLI.

**Gemini CLI no longer works with a personal Google login.** Since 18 June
2026, Google does not serve Gemini CLI to consumer accounts: Login with Google
no longer works for personal, Google AI Pro and Google AI Ultra accounts. To
use `harness:gemini`, set Gemini CLI up with a paid Gemini API key, Vertex AI,
or an existing Gemini Code Assist Standard or Enterprise license. Google stopped
selling Standard and Enterprise on 9 October 2026 and is phasing them out
([schedule](https://docs.cloud.google.com/gemini/docs/codeassist/sunset)), so
prefer an API key or Vertex AI for a new setup. See
[Google's notice](https://developers.google.com/gemini-code-assist/docs/deprecations/code-assist-individuals)
and [Gemini CLI's authentication guide](https://github.com/google-gemini/gemini-cli/blob/main/docs/get-started/authentication.mdx).

`npx` spends about a second on every launch resolving a package it has
already cached, and a conduit launches the agent once per task. Install
the pinned version once and every launch runs it directly:

```bash
atelier harness install claude-code codex
```

After `atelier harness sync` pins a newer version, that agent goes back to
`npx` until you run `atelier harness install` again; sync says which ones.

### Picking a model

A third segment names the model, spelled the way the agent lists it:

```yaml
    tool: harness:codex:gpt-5.1-codex
    tool: harness:claude-code:claude-sonnet-4-5
```

flow-atelier selects it on the ACP session before the first prompt. A
model the agent does not offer fails the task at once and prints the
models it does offer. `atelier harness check <name>` lists them too, as
`models:`. Without the suffix the agent runs on its own default.

### Picking a reasoning effort

A fourth segment names how hard that model should think, again spelled
the way the agent lists it:

```yaml
    tool: harness:codex:gpt-5.6-sol:high
    tool: harness:claude-code:opus[1m]:xhigh
```

Effort only comes after a model, because which efforts exist is a
property of the model: the agent is asked for the model first, and the
efforts it offers *for that model* are what the value is checked
against. An effort that model does not offer fails the task before the
prompt is sent, and prints the ones it does offer.

Leave the segment off and the model keeps whatever effort it defaults
to — flow-atelier sends nothing, so your own agent configuration stands.

To see the choices for a given model, no prompt and no tokens:

```bash
atelier harness check codex             # efforts for its default model
atelier harness check codex:gpt-5.6-sol # efforts for that model
```

### Checking a harness before you use it

```bash
atelier harness check gemini
atelier harness check codex:gpt-5.6-sol
atelier harness check --cmd "/opt/my-agent --acp"
```

This starts the agent, completes the ACP handshake, opens a session and
stops. No prompt is sent, so it costs no tokens. A `:<model>` or
`:<model>:<effort>` suffix is resolved the way a run resolves it, so
what the check reports is what a task naming that same tool would get.
It reports one of:

- **ok** — with the agent's name and version, the ACP version, the
  session modes it offers, and the models and reasoning efforts it
  offers for the model in force.
- **not found on PATH** — install the agent yourself, then re-check.
- **started but did not speak ACP** — usually the wrong entry point;
  many CLIs need an `--acp` flag.
- **could not open a session** — usually not logged in. The check lists
  the auth methods the agent advertises, and you log in with that
  agent's own CLI.
- **not usable — model/effort not offered** — the session opened fine;
  the suffix named something the agent does not have. The message lists
  what it does.

Failures exit non-zero and include the tail of the agent's own stderr,
which is where a failing agent explains itself.

For an agent the registry doesn't list — something private, a fork, a
local build — give flow-atelier its command and it becomes a first-class
harness:

```bash
ATELIER_HARNESSES='{"mine":["/opt/my-agent","--acp"]}'   # tool: harness:mine
```

To pin one of `claude-code`, `codex`, `opencode`, `copilot` or `cursor`
to a specific argv, the matching `ATELIER_*_LAUNCH_CMD` variable still
overrides the registry (see `.env.example`).
