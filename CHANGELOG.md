# Changelog

## 0.15.0

### Upgrading

<!-- --8<-- [start:upgrade-0-15-0] -->
- New run history is stored in `~/.atelier/projects/<project>/<flow_id>.jsonl`.
  Existing runs stay in the original project's `.atelier/flows/` directory
  and remain readable from that project. Use `atelier list flows` there to
  find them. New and migrated runs appear in cross-project history in the
  dashboard and CLI. Run `atelier migrate runs --dry-run` from each original
  project to preview migration, then `atelier migrate runs` to move verified
  run trees to the global store. Migration keeps run IDs and worktrees and
  archives the original records under `.atelier/migrated-runs/`.
- Automatic cleanup is off by default. Set `cleanupPeriodDays` in
  `~/.atelier/settings.json`. `ATELIER_CLEANUP_PERIOD_DAYS` in the process
  environment overrides that setting. With a positive value, each run in an
  eligible tree must be at least that many days old before the tree expires.
  Use `0` to expire all eligible run history at the next sweep. To disable
  cleanup, leave the environment variable unset and omit the JSON setting or
  set it to `null`.
- Run `atelier init` again to add read-only permissions to `pair` and
  `explain` files that exactly match the stock 0.14.1 starters, with either
  LF or CRLF line endings. Atelier preserves the agents and layout and prints
  `upgraded .atelier/conduits/<name>/conduit.yaml to read-only`. If an edited
  starter has writable agent tasks, Atelier keeps the file and prints
  `warning .atelier/conduits/<name>/conduit.yaml has writable agent tasks.`
  Add or set `permissions: read-only` on every agent task in that file.
- The macOS desktop app is removed. Use the CLI, or the dashboard from
  `atelier serve`, which your browser can install as an app. To keep
  schedules firing without an open terminal, run
  `atelier schedule service install` once. It starts the scheduler at login
  on macOS, Linux and Windows. See [Running on a schedule](https://flow-atelier.andesprit.com/docs/guides/schedules/#starting-the-scheduler-at-login).
<!-- --8<-- [end:upgrade-0-15-0] -->

<!-- --8<-- [start:privacy-0-15-0] -->
### Privacy and credentials

- On Unix, new run history files use owner-only permissions (`0600`), and
  run history directories use `0700`.
- History from 0.14.1 in `<project>/.atelier/flows/` keeps its existing
  permissions. To protect it, run `atelier migrate runs` or
  `chmod go-rwx .atelier/flows` from that project.
- **Forget token** clears the browser's saved token, conduit inputs, run paths,
  Kanban task history, and Designer drafts. Saved inputs and drafts expire
  after seven days and are removed when the dashboard reads them.
- The Vite development server accepts `VITE_API_TOKEN`. Production builds
  ignore it. Sign in at runtime using the server's API token in the browser.
- The mobile launcher hides **Enable completion notifications** when Web
  Push is unavailable.
- Dashboard chat copies an existing Codex `auth.json` from `CODEX_HOME`
  (default `~/.codex`) into a private temporary adapter profile. After the
  adapter exits, Atelier writes back a refreshed copy only if the original
  login is still current. Claude chat can use an environment token or an
  existing login file, and the Dictaria integration reads its bearer token
  from the OS keychain. See [agent credentials](https://flow-atelier.andesprit.com/docs/configuration/agent-logins/#optional-ai-harnesses)
  for the exact copy and write-back behavior.
<!-- --8<-- [end:privacy-0-15-0] -->
