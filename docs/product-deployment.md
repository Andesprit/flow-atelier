# Deploy an approved product to Cloud Run

Run `idea-to-product` without a deployment target to build, test and review
locally. To deploy, select a target configured for the exact working directory
in the launcher's user settings, `~/.atelier/settings.json`.

```json
{
  "deploymentTargets": {
    "/absolute/path/to/app": {
      "staging": {
        "provider": "cloud-run",
        "project": "example-staging",
        "region": "us-central1",
        "service": "reading-list",
        "deploy_account": "reading-deploy@example-staging.iam.gserviceaccount.com",
        "sources": ["app.py", "requirements.txt", "templates"],
        "report_url": "https://relay.example.org/hooks/errors/reading-staging",
        "report_secret": "reading-staging-errors:3"
      }
    }
  }
}
```

List the regular files and directories needed for the service build. The
recipe stages these inputs and its application reporter. Select only files
intended for release. The stager rejects links and private paths such as
`.git`, `.env` and `.atelier`. Existing application files must match the
named layout by the time the build and its tests pass.

```sh
atelier install builtin:platform --no-project
atelier run idea-to-product --input idea="Build a reading list" --input deployment_target=staging
```

On macOS the built-in Codex spec writer and reviewer use the Codex CLI's native
read-only profile. Install Codex with named permission profile and app-server
support. Each review uses a fresh session with a file-backed login. The reviewer
can run local inspection commands, while its checkout is read-only and command
networking is disabled. The builder and shell tests run in the outer OS sandbox
with disposable agent configuration directories.

On macOS, Claude product steps use a scratch home and cannot read your Keychain
login. Run `claude setup-token`, then set `CLAUDE_CODE_OAUTH_TOKEN` to the token
it prints in the environment of the process that launches Atelier. A Claude
product step stops before launch if the token is missing.

For a file-backed agent login, the outer sandbox gives the selected agent a
private copy. If the agent refreshes the login, Atelier saves it after exit
provided the original login still matches the copy taken at startup.

## Runtime events

Configure the 2.3 receiver for the same source, signing key and target project,
and select `error-to-pr` as the receiving conduit. Store the application's
copy of the key as the named Secret Manager version. The Cloud Run runtime
service account needs access to that version. [Configure the receiver](error-webhooks.md)

On spec approval the recipe adds `atelier_events.py` to the target. A Python
application can call it directly. Other runtimes can use the same wire format.
The builder and reviewer receive the reporting contract and must exercise
the application's warning and error paths. Use stable event keys and brief
messages that are safe to record in service logs and the error report.

```python
from atelier_events import emit

emit("WARNING", "cache-cold", "Reading list cache is warming")
emit("ERROR", "save-failed", "Saving the reading list failed")
```

Info, warning and error events produce one JSON line on standard output with
severity, deployment target, fingerprint and message. Errors also send the
timestamped HMAC-signed JSON to the configured relay. Repeated errors with
the same target and event key share a receiver claim. The application reads
`ATELIER_PRODUCT_TARGET`, `ATELIER_ERROR_URL` and `ATELIER_ERROR_KEY` at
runtime. The deployment references the secret by name and version.

## Approval and credentials

The builder, shell test and reviewer run under macOS Seatbelt or Linux
Bubblewrap. The wrapper masks local Google Cloud, AWS, Azure and Kubernetes
configuration and drops cloud credential environment variables. Builders can write
source files and disposable scratch space. Git metadata and persistent agent
configuration are protected. Install `bwrap` on Linux. Hosts without a supported
sandbox refuse these steps. Product deployment requires macOS or Linux for secure
source snapshots.

After tests and independent review pass, the second human gate shows the
project, region, service, source digest, deployment identity and runtime
reporting configuration. Type `DEPLOY` to approve that plan. Declining
finishes the local build. If the destination or release bytes change after
approval, start a new run and review the new digest.

Set up the Google Cloud CLI on the launcher. Give the launcher permission
to impersonate a **dedicated deployment service account for each target**.
Grant that account only the rights to build the approved source and deploy
to the named service. Use a dedicated staging project when source-build
permissions apply to the project. IAM on the impersonated identity determines
the token's effective scope. [Cloud Run source deployments](https://cloud.google.com/run/docs/deploying-source-code)
and [Cloud Run IAM](https://cloud.google.com/run/docs/securing/managing-access)

After approval, the trusted deploy step requests a ten-minute impersonated
access token and passes it to the Cloud Run CLI child through
`CLOUDSDK_AUTH_ACCESS_TOKEN`. The child has a fresh Cloud SDK home. If the
launcher loses the deployment result, inspect the service revision before
starting another deployment. [Token lifetime](https://cloud.google.com/sdk/gcloud/reference/auth/print-access-token)
and [Google Cloud CLI access tokens](https://cloud.google.com/sdk/docs/authenticate)
