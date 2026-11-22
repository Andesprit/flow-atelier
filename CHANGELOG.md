# Changelog

## 0.15.0

### Upgrading

- New run history is stored in `~/.atelier/projects/<project>/<flow_id>.jsonl`.
  Existing runs stay in the original project's `.atelier/flows/` directory
  and remain readable from that project. Use `atelier list flows` there to
  find them. New and migrated runs appear in cross-project history in the
  dashboard and CLI. Run `atelier migrate runs --dry-run` from each original
  project to preview migration, then `atelier migrate runs` to move verified
  run trees to the global store. Migration keeps run IDs and worktrees and
  archives the original records under `.atelier/migrated-runs/`.
- Automatic cleanup is off by default. Set `cleanupPeriodDays` in
  `~/.atelier/settings.json` or `ATELIER_CLEANUP_PERIOD_DAYS` in the shell
  environment. A positive value keeps eligible run trees for that many days.
  Use `0` to expire all eligible run history at the next sweep. To disable
  cleanup, leave the environment variable unset and omit the JSON setting or
  set it to `null`.
- Run `atelier init` again to add read-only permissions to `pair` and
  `explain` files that exactly match the stock 0.14.1 starters, with either
  LF or CRLF line endings. Atelier preserves the agents and layout and
  reports the upgrade. If an edited starter has writable agent tasks,
  Atelier keeps it unchanged and asks you to add or set
  `permissions: read-only` on every agent task.
- The desktop app is outside the scope of 0.15.0. The release includes the
  Python package and standalone CLI builds.

### Privacy and credentials

- On Unix, new run history files use owner-only permissions (`0600`), and
  run history directories use `0700`.
- **Forget token** clears saved conduit inputs and Designer drafts in the
  browser as well as the saved token. Saved inputs and drafts expire after
  seven days and are removed when the dashboard reads them.
- The Vite development server accepts `VITE_API_TOKEN`. Production builds
  ignore it. Sign in at runtime using the server's API token in the browser.
- The mobile launcher hides **Enable completion notifications** when Web
  Push is unavailable.
- Dashboard chat copies an existing Codex `auth.json` from `CODEX_HOME`
  (default `~/.codex`) into a private temporary adapter profile. After the
  adapter exits, Atelier writes back a refreshed copy only if the original
  login is still current. Claude chat can use an environment token or an
  existing login file, and the Dictaria integration reads its bearer token
  from the OS keychain. See [agent credentials](docs/guide.md#optional-ai-harnesses)
  for the exact copy and write-back behavior.
