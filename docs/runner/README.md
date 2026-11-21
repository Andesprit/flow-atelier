# Outbound runner protocol v1

Pair from the machine that will execute the work. The runner has no listening port.
The local user chooses the folder, its public label, and the recipes allowed in it.

```sh
atelier runner pair https://control.example
atelier runner expose /work/manuals Manuals
atelier runner approve FOLDER_ID build
atelier runner start
```

Use `atelier runner status` in another terminal to see the last acknowledged heartbeat and the
runner's local online/offline estimate.

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
Validate each HTTP body against its named definition, for example
`messages.schema.json#/$defs/Poll`. The sample file groups bodies by type.
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
For a shell task, v1 accepts a literal `true`, `false`, `pwd`, `echo` or `printf` command, or a
bundled executable entry point such as `bash "{{conduit_dir}}/scripts/build.sh"`. The supported
interpreters are bash, sh, python, python3 and node. A helper may take simple literal arguments.
Put complex shell programs in the bundle. Package their helper dependencies there too and resolve
them relative to the script. Locally approved helpers and agents are trusted code with access to
the working tree and installed tools.
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

## Outbound records

The public stream contains job lifecycle events, structured task completion logs, and a reserved
chat delivery status. Each record includes its job ID, registered folder ID, sequence, kind and
status. A task may include its name. Location comes from the job's local folder mapping. The
projection discards environment fields, paths, hostnames, session contents, shell output, chat
text, error details and other unmapped fields. The runner never opens a transcript for upload.

The runner omits names containing an environment value, a string value from user settings, a
known token pattern or the local hostname. To add private values, put a JSON array of strings in
`~/.atelier/runner/redact.json` (relative to the configured global store). A filtered public folder
label falls back to the folder ID. The same rules apply before buffering and again on transmission,
including retransmission. A runner has no command or protocol option for raw uploads.

Sequence numbers span all jobs of one identity, start at 1 and persist in the local SQLite store.
On each poll the runner sends up to 100 rows after the server's contiguous cursor. The server
checks the owner and folder of each job, rejects gaps, and commits the batch atomically. Retries of
previously committed sequence numbers do not create additional records. A tenant view reads
these rows in sequence order. It displays `online` and the last heartbeat from the runner's most
recent authenticated poll. See the [test reference peer](../../tests/runner/plane.py).
