# Security

To report a security problem privately, see [SECURITY.md](https://github.com/Andesprit/flow-atelier/blob/main/SECURITY.md).

## Server access

The API runs shell commands on the machine hosting it. Treat access to the API
as equivalent to a shell on that machine.

**On loopback (the default).** `atelier serve` binds `127.0.0.1:8000` and needs
no token. Three guards keep a web page you happen to visit from driving it:

- **Origin.** Only pages this server served may call the API, plus origins you
  name with `--cors-origin`, such as the Vite dev server. A page on any other
  site or local port, such as another tool's local UI, gets `403`, whether or
  not its request would need a CORS preflight. A client that sends no `Origin`,
  such as the CLI or `curl`, is not a page and is let through. With
  `ATELIER_API_TOKEN` set, the token does this job instead.
- **Host.** Only `localhost`, `127.0.0.1`, and `::1` are accepted as the `Host`
header. This is what stops DNS rebinding, where an attacker's page resolves
its own hostname to `127.0.0.1` so the browser treats the request as
same-origin and sends no `Origin` for CORS to reject. Requests carrying any
other `Host` get `400 Invalid host header`.
- **WebSocket origin.** CORS does not apply to WebSockets, and a page on any
site can open `ws://127.0.0.1:8000/...` with a loopback `Host`. So the
WebSocket routes check the page's `Origin` themselves: they accept a local
page, an origin passed with `--cors-origin`, or this server's own UI, and
refuse any other with close code `1008` before the connection opens. A page
on another local port is refused too. A client that sends no `Origin`, such as
a script, is not a browser page and is let through.

These guards are for web pages. Any program running as you on the machine can
still call the loopback API without a token. Set `ATELIER_API_TOKEN` when other
users share the machine, or when a program you do not trust runs there.

**Anywhere else.** Before binding to a non-loopback address, set
`ATELIER_API_TOKEN`. Every REST request then needs
`Authorization: Bearer <token>` and WebSocket connections need `?token=<token>`.
Open the dashboard and enter the server's token when the browser prompts you
to sign in. The Vite development server accepts `VITE_API_TOKEN` for local
development. Production builds ignore it.

Choose **Forget token** to clear the browser's saved token, conduit inputs,
run paths, Kanban task history, and Designer drafts. Saved inputs and drafts
expire after seven days. The dashboard removes expired data when it reads it.

The mobile launcher at `/launch` offers **Enable completion notifications**
when the browser and server support Web Push.

`atelier serve` **refuses to start** on a non-loopback host when
`ATELIER_API_TOKEN` is unset. This used to be a warning that scrolled past in
the same second the port opened, so an existing `--host 0.0.0.0` setup with no
token will now stop rather than serve:

```console
$ atelier serve --host 0.0.0.0
error: refusing to serve on non-loopback host '0.0.0.0' without
ATELIER_API_TOKEN. Anyone who can reach this address could run shell
commands via the API. Set ATELIER_API_TOKEN, or bind 127.0.0.1 (the default).
```

Binding a specific host also adds that host to the accepted `Host` values; a
wildcard bind (`--host 0.0.0.0`) cannot know which names reach it, so it accepts
any `Host` and relies on the token — which is why the token is mandatory there
rather than merely advised.

See [Settings and environment variables](settings.md) for which sources can set runtime options.

See [Receive signed application errors](../error-webhooks.md) to set up the separate error receiver and HTTPS relay.

## Agent and shell processes

Each agent and each shell step runs in its own
process group on Linux and macOS. When a step is stopped or times out, Atelier
ends the whole group, so the tools, servers and shells it started do not keep
running; after an agent turn that failed, it also ends what the agent left
behind. On Windows a shell step's process tree is ended, and for an agent only
the agent itself. Steps inherit your environment, without `ATELIER_API_TOKEN`.

## Secrets in terminal output and logs

`atelier run` prints the argument that
identifies each tool call — the bash command, the file path, the search
pattern — so the run is readable. Credential-shaped values (`Bearer <token>`,
`sk-`/`ghp_`/`xox`-prefixed keys, `--password`/`TOKEN=` flags) are masked as
`***` on the way to the screen. This is a heuristic that reduces casual
leakage, not a guarantee: it will miss a secret that does not look like one.
The recorded logs in the global run history and legacy project stores keep the **unredacted** text, so
treat that directory as sensitive and check what you are pasting before sharing
a terminal transcript.

## Shell inputs

A `{{...}}` in a `tool:bash` command becomes part
of the command before bash reads it, quoted or not. An input from a schedule,
the API or a human answer, or an agent's output, can then run commands. Read
values from `"$INPUT_<NAME>"` and `"$OUTPUT_<TASK>"` instead, or from the
file at `"$OUTPUT_<TASK>_FILE"` when a value can pass 100,000 bytes, since the
plain variable is then unset (see
[Values in shell steps](../reference/conduit-yaml.md#values-in-shell-steps)); `atelier check` warns about
the direct form.

## Conduits and packages

See the warning under
[Installing conduit packages](../guides/packages.md): running a conduit
runs whatever shell commands it contains.

## Agent permissions

Agents run with full permissions by default. An agent task can edit any file
and run any command it wants. Atelier switches the agent to the most
permissive mode it knows, such as Claude Code's `bypassPermissions`, and
approves every permission request the agent sends. A prompt, or text the agent
reads in the project, can lead it to do damage. Limit what it may do with
`permissions`:

| Value | What the agent may do without asking |
| --- | --- |
| `full` | Everything. This is the default. |
| `edits` | Read and change files. Atelier picks an accept-edits mode when the agent has one, such as Claude Code's `acceptEdits`. Otherwise it picks a read-only mode that asks before each edit, such as Codex's `read-only`. It allows requests to read, search, edit, move or delete files, and rejects the rest, such as running a command or fetching a URL. |
| `read-only` | Only read, as far as Atelier is asked (see below). Atelier picks a read-only mode such as Codex's `read-only`, else a mode that asks before acting, such as Claude Code's `default` (leaving a bypass mode first), and rejects every request. |

Set it on one task with `permissions:`, or for every agent task with
`ATELIER_AGENT_PERMISSIONS` in the environment or in `.env`. The task field
wins. `atelier plan` shows the level of each agent task. `atelier harness check`
shows the mode runs will use at the `ATELIER_AGENT_PERMISSIONS` level.

```yaml
- review:
    description: Review the change
    task: Review the last commit and list what must be fixed.
    tool: harness:codex
    permissions: read-only
```

What happens after a rejection is up to the agent. Codex ends its turn, so
the task fails and its error names the rejected request, for example
`permissions read-only rejected: Edit files`.

While neither is set, each run prints one line that says agents have full
permissions, and keeps it with the first agent task's steps, so the run page
shows it too. Setting either one, even to `full`, removes that line. A conduit
whose `interaction.permissions` sends requests to a person or a supervisor does
not print it.

The levels are not a sandbox. Atelier only chooses the agent's mode and answers
the requests the agent sends. An agent can still do what its own mode allows
without asking. For example, Codex in its `read-only` mode still runs commands
that only read, inside its own sandbox.

An agent that offers neither a mode named for the level nor a mode known to ask
before acting (`default`, `normal`, `ask`, `code`) could act without asking, so
`edits` and `read-only` cannot be held there. Such a task fails before its
prompt is sent, with `permissions read-only cannot be enforced: ...`. This
covers an agent with no session modes at all, and one whose modes Atelier does
not know, such as opencode's `build`. `atelier harness check` says so in its
modes line. To run anyway, with only the requests the agent asks about checked,
set `ATELIER_ALLOW_UNENFORCED_PERMISSIONS=1` in the environment (it is not read
from `.env`); the run then prints a warning instead.

### Supervisors and local agent settings

A supervisor (see [interaction policies](../interaction.md)) runs in the agent's
read-only mode when it offers one, such as Codex's `read-only`, and every
permission request it sends is rejected.

The agent's own settings still apply at every level. Claude Code loads your
user settings and the project's `.claude/settings.json` and
`.claude/settings.local.json`. Tools that their allow rules pre-approve run
without asking, and their hooks run shell commands by themselves. Neither
reaches Atelier, so `edits` and `read-only` cannot stop them; other agents'
own config files can do the same. A checkout you do not trust can ship such a
file, so `read-only` does not make it safe to review: check its `.claude/`
folder first, or run the agent in a container or a virtual machine.

A conduit whose `interaction.permissions` sends requests to a person or a
supervisor still applies the level first. With `edits` or `read-only` Atelier
then takes a read-only mode when the agent has one, such as Codex's
`read-only`, so the person is still asked about each edit; it never takes an
accept-edits mode there. A request the level does not allow is rejected
without asking anyone. Only the rest are asked. See
[interaction policies](../interaction.md).
