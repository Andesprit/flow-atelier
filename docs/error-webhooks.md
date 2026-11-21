# Receive signed application errors

Start `atelier serve-hooks` on the machine that owns the target repository. It listens on
`127.0.0.1:8001`. Configure each source in the user's `~/.atelier/settings.json` and
restrict that file to the user. Restart the receiver after editing it.

```json
{
  "webhookSources": {
    "orders": {"project": "/absolute/path/to/orders", "conduit": "error-to-pr"}
  },
  "webhookSecrets": {"orders": "<per-source random secret>"},
  "runCaps": {"errors": 2}
}
```

Install the selected conduit before enabling its source. It receives one input,
`report_b64`, containing the exact signed JSON bytes in standard Base64. Decode it
as data inside the trusted intake step. All report text can originate from an
application user.

## Signing a request

Send UTF-8 JSON to `POST /hooks/errors/orders`. Set `X-Atelier-Timestamp` to the
current Unix time in whole seconds. Set `X-Atelier-Signature` to `sha256=`
followed by the lowercase hex HMAC-SHA256 of the ASCII timestamp, a literal
period and the exact request bytes, keyed with the UTF-8 source secret.

```python
timestamp = str(int(time.time()))
signature = "sha256=" + hmac.digest(
    secret.encode(), timestamp.encode() + b"." + body, "sha256"
).hex()
```

Reports may include an application-defined `fingerprint`. Otherwise send an
`exception_class` and an ordered, nonempty list of stack `frames` with stable
module and function names. Line numbers and message text may change between
occurrences. For example:

```json
{"exception_class":"KeyError","frames":[{"module":"orders.checkout","function":"save","line":24}]}
```

A valid request returns HTTP 202 with `run_id` and `status`. The same source,
project and fingerprint return the original ID until a person reopens the error. After history cleanup,
`status` is `history_expired`. The receiver accepts up to 64 KiB and five minutes of
clock skew in either direction. Up to two error runs execute per machine by default.
When all 100 waiting positions are occupied, new errors get HTTP 429 with
`Retry-After`. Previously claimed errors can still be looked up by resending them.
Restarting drains waiting work. A run interrupted after its journal was published
keeps its ID. The dispatcher logs interrupted run IDs at startup. Inspect the local
record with `atelier status <run-id>`. To allow a new run for the same fingerprint,
use `atelier errors reopen <run-id>`. The next signed report starts the new run.
Reopening preserves existing run history and requires a terminal or expired claim.

## HTTPS relay

Use a dedicated TLS virtual host in Apache with `mod_proxy`, `mod_proxy_http`
and `mod_alias`. Configure its certificate and key through the site's usual TLS
settings and include the supplied [webhook relay configuration](webhook-relay.conf).
It forwards the error path to the receiver's loopback port and returns 404 on
other paths, including `/flows`. If you set `serve-hooks --port`, update the
relay target to match.

## Error-to-pr

Install the platform bundle with `atelier install builtin:platform --no-project`.
For a previously installed starter, review local recipe edits before replacing it. Configure
the webhook source to run `error-to-pr`. Add an entry keyed by the resolved repository path
in the same user settings file. This example runs a Python project's unittest suite.

```json
{
  "draftCiDisabledRepositories": ["example/orders"],
  "errorPrProjects": {
    "/absolute/path/to/orders": {
      "repository": "example/orders",
      "test_file": "test_atelier_{id}.py",
      "regression": ["/usr/bin/python3", "{test}"],
      "suite": ["/usr/bin/python3", "-m", "unittest", "discover"]
    }
  }
}
```

Previously prepared snapshots without the skip marker require a fresh run before automatic
publication. Reopen a failed error claim and submit the signed report again.

The regression command receives the new test's relative path as a separate argument.
Use a runner that exits 1 for a failing regression and 0 for success. Both commands must
work offline from a fresh checkout of committed files. Set up the local GitHub CLI
login for the configured repository and the [WhatsApp sender](guide.md#send-a-whatsapp-run-summary).
The repository must be private and its `origin` must match the configured GitHub destination.

Before adding a repository to `draftCiDisabledRepositories`, configure every CI system,
GitHub App and hosting integration to exclude `atelier/*` branches and PRs from those
branches, including preview builds and `pull_request_target` workflows. Worker-authored
tests and build scripts can access credentials if an integration executes them. The
publisher includes `[skip ci]` on each draft commit for GitHub Actions push and
pull_request workflows. Other triggers and providers require their own exclusions.
The user settings entry acknowledges those exclusions for that specific repository.

The shared worker uses the Codex CLI on macOS or Linux with named filesystem permission
profiles and the app-server protocol. Its launcher creates a temporary configuration,
uses the current file-backed Codex login, and checks the effective workspace-write roots,
disabled tool networking, disabled web search, and empty MCP server list before sending
the report. Unsupported configurations stop the run. The worker sees a scratch checkout
of committed `main`, its own Git metadata and the user-selected instructions. Application
credentials, publisher credentials and the original checkout stay outside the workspace.

Atelier first checks the parent suite. It runs the new test against a fresh copy of the
parent plus that test, then reruns the identical test and full suite against fresh copies
of the proposed commit. Failed reproduction or verification leaves a diagnostic in the
run log. The publisher accepts the verified commit, checks that remote `main` still
matches its parent, and creates a draft PR from `atelier/errors/<run-id>`. The notify step
sends the PR URL. Replays return the original run ID. Its publish output holds the PR URL
until run history expires. A person reviews and merges the draft.
