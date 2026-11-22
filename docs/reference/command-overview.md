# Commands

```
# authoring
atelier init
atelier create <name> [--description <text>] [--template hello|code-review|fix-loop]
                                                       # scaffold a starter conduit
atelier compose <name> --step <harness>=<prompt> [--step ...] [--parallel]
                       [--synthesize <harness>=<prompt>] [--description <text>] [--permissions <level>]
                                                       # write one task, a handoff chain,
                                                       # or parallel workers plus one synthesis step
                                                       # see docs/multi-agent-workflow.md
atelier check [<conduit>] [--json] [--recursive]        # validate conduit(s) without running
                                                       # --recursive also checks the conduits they call
atelier plan <conduit> [--json]                        # print the DAG as ordered waves, run nothing
atelier show <conduit> [--json]                        # print its definition and inputs, run nothing
atelier schema                                         # print the conduit.yaml JSON Schema for your editor

# <flow_id> below accepts a unique prefix, or 'latest' for the most recently started flow
# running
atelier run <conduit> [--input key=value ...] [--input-file key=path ...]
                      [--show-steps/--hide-steps] [--json]
                                                       # --input-file loads a UTF-8 text file into one input
atelier ask <query> --path <directory> [--harness <name>] [--permissions <level>]   # interactive agent session (default: claude-code)
atelier ask <query> --path <directory> --json [--timeout <seconds>]   # one bounded turn
atelier ask <flow_id> --json [--answer <text> --request-id <id> | --cancel]
atelier run --resume <flow_id>                         # resume a failed/crashed flow
atelier run --again <flow_id>                          # fresh run reusing a past flow's inputs
atelier stop <flow_id>                                 # gracefully halt a running flow

# inspecting
atelier status <flow_id>
atelier diagnose <flow_id> [--json]                    # post-mortem: what failed, what is kept, what to do next
atelier wait <flow_id> [--timeout 60]                  # block until it finishes; exit 0 only if it did
atelier logs <flow_id> [--task <name>] [--json]         # saved text or JSON array
atelier logs <flow_id> --jsonl [--follow]               # one record per line
atelier outputs <flow_id> [--task <name>] [--json]    # read back a finished flow's results
atelier timing <flow_id> [--json]                      # wall time, per-task duration and agent startup
atelier list conduits
atelier list flows [--conduit <name>]
atelier list schedules [--json]
atelier list harnesses [--ready] [--json]
atelier harness install <name>...                      # run npx agents without npx's per-launch cost
atelier rm <flow_id> [--force] [--yes]                 # delete one flow run
atelier prune [--conduit <name>] [--older-than <days>] [--keep <n>]   # bulk-delete old flows

# sharing conduits (see "Installing conduit packages" below)
atelier install <source> [--ref <git-ref>] [--project] [--force]
atelier update <package>                               # re-fetch and re-install from source
atelier uninstall <package>                            # delete a package's conduits

# scheduling
atelier schedule add <file.{json,yaml}>
atelier schedule migrate
atelier schedule rm <id-or-name>
atelier schedule run-now <id-or-name>
atelier schedule history <id-or-name>
atelier schedule daemon [--reload-interval 30] [--log-level INFO] [--wait-for-lease]
atelier schedule service install                       # start the scheduler at login
atelier schedule service status
atelier schedule service uninstall

# HTTP + WebSocket server
atelier serve [--host 127.0.0.1] [--port 8000] \
              [--reload-interval 30] [--cors-origin URL]* \
              [--log-level INFO] [--idle-exit MINUTES] [--no-scheduler]

# maintenance
atelier self-update                                    # prebuilt binary only; uv installs use `uv tool upgrade`
```

See the [CLI reference](cli.md) for the full list of commands and flags.
