# Where conduits live

Conduits can live in two places:

- **Project**: `./.atelier/conduits/` — scaffolded by `atelier init`.
- **Global**: `~/.atelier/conduits/` — shared across all projects.

When you run a conduit, flow-atelier checks the project folder first,
then the global folder. A project-level conduit silently overrides a
global one with the same name.

Each new run gets an event file under `~/.atelier/projects/<project>/`.
On Unix, Atelier creates run history files with mode `0600` and directories
with mode `0700`.
Open the dashboard from any project to see global history. Choose a target
directory in the project filter, or use `atelier list flows --project .`.
The API accepts the same filter at `GET /flows?project=<directory>`.
Older records remain readable from the original project’s `.atelier/flows/` directory.
From that project, run `atelier migrate runs --dry-run` to verify its run trees,
then `atelier migrate runs` to publish the verified journals. Migration archives
each verified source under `.atelier/migrated-runs/` and preserves its worktrees.
Migrated runs retain their last heartbeat and finish time. If you enable
automatic cleanup, the next sweep can expire an already-old finished tree.
Its source archive stays in the project.

## Folder layout

The `.atelier` directory lives in the working directory where
`atelier` is invoked.

```
.atelier/
├── conduits/
│   └── <conduit_name>/conduit.yaml
└── flows/                                  # legacy project-local runs
    └── <flow_id>/                          # <YYYYMMDD>_<uuid8>_<conduit>
        ├── input.yaml                      # the inputs this run was given
        ├── logs.jsonl                      # append-only log, one JSON object per line
        ├── progress.json                   # live per-task status
        ├── outputs.yaml                    # per-task outputs (written as tasks finish)
        └── flows/
            └── <child_flow_id>/...         # nested tool:conduit runs

~/.atelier/
├── projects/<encoded-project>/
│   ├── <flow_id>.jsonl                     # one versioned event per line
│   └── <flow_id>/
│       ├── steps.jsonl                     # live tool steps
│       └── outputs/<task>/<sequence>       # output text over 100,000 UTF-8 bytes
├── schedules/
│   └── <slug>-<hash>.yaml                  # one YAML file per shared schedule
└── scheduler_state.json                    # fired-once markers and run history
```

Set `ATELIER_GLOBAL_ATELIER_DIR` in the process environment to relocate the
entire global store. A run header records the full resolved project path.
The folder name replaces each non-alphanumeric character with a hyphen, so
two projects can share a folder while retaining their separate identities.
Encoded names longer than 160 UTF-8 bytes use a SHA-256 digest of the full
project path. Existing unshortened folders remain readable.
