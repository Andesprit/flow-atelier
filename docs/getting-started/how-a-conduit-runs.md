# How a conduit runs

You write a conduit YAML file and put it in
`.atelier/conduits/<name>/conduit.yaml`. When you run
`atelier run <name>`, flow-atelier:

1. Reads the YAML.
2. Looks at each task's `depends_on` list to figure out which tasks can
    start now and which have to wait.
3. Starts every ready task at the same time, up to a configurable
    limit (`max_concurrency`).
4. For each task, picks an executor based on the `tool:` field:

    | Tool                  | What it runs                                                         |
    | --------------------- | -------------------------------------------------------------------- |
    | `tool:bash`           | a shell command                                                      |
    | `tool:hitl`           | prompts a human on the terminal for one or more named answers        |
    | `tool:conduit`        | another conduit, as a nested run                                     |
    | `harness:claude-code` | Claude Code (via the [ACP](https://agentclientprotocol.com) adapter) |
    | `harness:codex`       | OpenAI Codex (via the ACP adapter)                                   |
    | `harness:opencode`    | [opencode](https://opencode.ai)                                      |
    | `harness:copilot`     | GitHub Copilot CLI                                                   |
    | `harness:cursor`      | Cursor CLI                                                           |

5. Saves each run to `~/.atelier/projects/<project>/<flow_id>.jsonl` — what
    ran, what each task printed, whether it succeeded, when it
    finished.

AI harnesses use their own logins. Dashboard chat can copy an existing login
into a private temporary adapter profile. See [AI harnesses](../configuration/agent-logins.md#optional-ai-harnesses)
for the credential handling details.
