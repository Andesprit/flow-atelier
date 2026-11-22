# Roadmap

Flow Atelier runs repeatable workflows with shell commands and coding agents.
The CLI and web dashboard share run history across projects. The dashboard
also lets you design conduits, follow active runs and manage schedules.
See the [quickstart](getting-started/quickstart.md) to get started.

## 0.15.0

New runs use the shared history under `~/.atelier/projects/`. Existing project
history stays available from its original project and can be migrated into the
shared store. Automatic cleanup is off by default. To enable it, set
`cleanupPeriodDays` in `~/.atelier/settings.json` to the number of days to
keep eligible history. Use `0` to expire all eligible history at the next
sweep. See the [upgrade notes](upgrading.md#0150) and
[cleanup settings](configuration/settings.md#automatic-cleanup) for details.

## What's next

Track proposals and suggest improvements in
[GitHub Issues](https://github.com/Andesprit/flow-atelier/issues).
