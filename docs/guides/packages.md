# Installing conduit packages

A conduit is just a folder, so conduits are shareable. `atelier install`
installs them from a git repo or a local path:

```bash
atelier install owner/repo                  # GitHub shorthand
atelier install https://github.com/owner/repo.git
atelier install ./some/local/package
atelier install owner/repo --ref v1.2.0     # pin a branch, tag, or commit
```

You are asked whether to install globally (`~/.atelier`) or into the
current project (`./.atelier`); `--project` / `--no-project` answers that
up front. An existing conduit of the same name is **skipped**, not
overwritten, unless you pass `--force`.

> **Conduits are code.** A conduit can run any shell command on your
> machine the moment you `atelier run` it. Read a package before you
> install it, and pin `--ref` for anything you don't control.

A package is any repo with its conduits under `.atelier/conduits/` and an
`atelier-package.yaml` at the root:

```yaml
name: my-conduits        # letters, digits, _ and - only
version: 1
conduits:
  - deploy
  - nightly_report
```

Each listed name must be a directory under `.atelier/conduits/`. The whole
directory is copied, so helper scripts and templates next to
`conduit.yaml` travel with it. Without a manifest, flow-atelier discovers
conduits by scanning that directory and warns that it did so. Schedules
are never installed — they hold machine-specific state.

`atelier update <package>` re-fetches from the recorded source and
re-installs. `atelier uninstall <package>` deletes only the conduits that
install actually wrote, so a conduit that was skipped on collision is
left alone.

## Install the platform recipe bundle

`atelier install builtin:platform --no-project` installs version 3 in the global
conduit store. It includes `error-to-pr`,
`idea-to-product`, `continuous-improvement` and `meetings-to-tasks`.
Configure the Dictaria account and test a
manual poll before enabling its five-minute schedule. [Meeting intake setup](../meetings-to-tasks.md)
For application errors, configure a signed webhook source. [Error-to-PR setup](../error-webhooks.md#error-to-pr)

A project's own conduit takes precedence. Reinstalling skips existing files.
Use `atelier update platform --force` only when you intend to replace them.

### Shared agent instructions

To share instructions between harnesses, add a `stepInstructions` entry to
your user settings. Use the target project's resolved absolute path as the
key and list the rule files relative to that directory.

```json
{
  "stepInstructions": {
    "project_rules": {"/absolute/path/to/repo": ["CLAUDE.md", "docs/review.md"]},
    "skills": ["code-review"]
  }
}
```

Harness tasks receive the selected rule text for that project and the text
of each selected `~/.claude/skills/<name>/SKILL.md`. A skill directory can
be a symlink. Select these files in user settings or the process environment. An unreadable
selection stops the conduit before its harness starts and names the file.

### Idea to product

For staging or production, see [approved Cloud Run deployments](../product-deployment.md).

Use an existing directory as the target path. Start the CLI in that directory,
or select it as the dashboard run path. An empty directory works for a new
product. Supply a phrase or paste a paragraph, spec or PRD as the `idea`.

```sh
cd /path/to/target
atelier run idea-to-product --input 'idea=Build a reading list'
atelier run idea-to-product --input-file idea=/path/to/prd.md --input 'test_command=make test'
```

Codex drafts a spec with observable acceptance criteria and a test command.
An explicit `test_command` overrides the proposal. Review the spec and the
effective command, then type `APPROVE` to allow edits and test execution.
Declining ends the run. In a new project, the builder creates the test entry
point named in the approved spec.

Each of the three allowed attempts runs Claude Code as builder. A passing
shell test starts the independent Codex review. Failed tests and requested
changes go back to the next builder attempt. A passing test and an exact
`PASS` from review release the final `atelier notify` step. Configure the
[sender](whatsapp.md) before running the recipe. Agent overrides for `attempt.build`
and `attempt.review` must still name different harnesses.

For fixed-workload experiments, see [Continuous improvement](../continuous-improvement.md).
