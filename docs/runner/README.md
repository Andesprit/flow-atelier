# Outbound runner protocol v1

Pair from the machine that will execute the work. The runner has no listening port.
The local user chooses the folder, its public label, and the recipes allowed in it.

```sh
atelier runner pair https://control.example
atelier runner expose /work/manuals Manuals
atelier runner approve FOLDER_ID build
atelier runner start
```

The pairing command prompts for a single-use invitation with echo disabled. A successful exchange
creates a runner identity in the user store's private `runner/` directory. One identity is bound to
one HTTPS origin. The service can revoke its bearer token. Choose public labels and conduit names.
The runner advertises only those names and locally generated folder IDs.

## Wire contract

All requests are HTTPS POST, content type `application/json`, with `Authorization: Bearer <token>`.
The version is in the path. A success is HTTP 200 and a JSON object. Use 401/403 for rejected or
revoked credentials, 409 for ownership or sequence conflicts, and 429/5xx for transient errors.
Clients must verify TLS certificates, reject redirects and reject unexpected fields. No service
URL, filesystem path, command, environment, recipe, or credential override comes from a job.

| Path under /runner/v1 | Request | Response | Behavior |
| --- | --- | --- | --- |
| pair | Pair | Identity | Atomically consume an invitation and bind a new identity to its tenant. |
| poll | Poll | Dispatch | Replace this runner's public folders, record heartbeat and optionally claim work. Return the highest contiguous event sequence stored for this runner, initially zero. |
| result | Result | Ack | Record owner-only job state. Repeat of the same final result is idempotent. |
| records | Records | Ack | Commit a contiguous batch for this runner. Deduplicate on runner ID and sequence. |

See the [JSON Schema](v1/messages.schema.json) and [example messages](v1/samples.json).
A server authenticates every request. Derive the tenant and runner from the bearer, never a payload.
Restrict tenant-facing reads to that tenant. Bind a job ID to one runner in a transaction before
returning it. Deliver the same immutable job to that runner until it reports a terminal result.
Retain ownership across connection loss. A stale heartbeat after start means an unknown outcome
requiring an explicit recovery decision. The reference peer uses 10-second beats and a 60-second
online window. Compute liveness from the server's receipt time.

## Approval scope

Review executable code locally. Bundle recipe-owned helpers alongside `conduit.yaml` and reference
them with `{{conduit_dir}}`. The digest covers each bundled file, executable bits, and all statically
named child recipe bundles (project-first, then global). Dynamic child names, linked bundle entries,
and replaced folder roots cannot be approved for a run. Host tools and the project's working files
remain local execution dependencies. Conduits execute with their ordinary local process permissions.
V1 launches with locally defined recipe defaults. Remote input and tool overrides are outside this
version. Reapprove locally after a bundle edit.

## Dispatch and recovery

The runner commits the immutable job ID, folder, conduit and allocated local run ID to SQLite
before acknowledging start. It retries a lost start acknowledgement with the same mapping.
After acknowledgement it commits a start marker, keeps the verified bundles in
`runner/runs/<job-id>/conduits/`, and starts the normal engine with the registered working directory.
The local engine journal contains the full execution detail. A separate polling loop continues to
send heartbeats during execution. The worker commits the outcome locally before reporting it.
Repeating a claimed job reuses the mapping and returns the recorded outcome. If the process died
after the start marker but before recording the outcome, the runner reports `unknown` and the
operator can investigate the mapped local run. Automatic retries never execute that job again.
