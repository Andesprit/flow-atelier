# Outbound runner protocol v1

Pair from the machine that will execute the work. The runner has no listening port.
The local user chooses the folder, its public label, and the recipes allowed in it.

```sh
atelier runner pair https://control.example
atelier runner expose /work/manuals Manuals
atelier runner approve FOLDER_ID build
atelier runner start
```

Approval prints the digest, conduit source directories, bundled files and their executable bits,
and each agent task's harness and permissions. Confirm the digest at the prompt or pass
`--yes` in scripts. Both `approve` and `start` warn when the privacy filter hides a label or name.

Use `atelier runner folders` to list local paths, folder IDs, labels and approved digests.
`atelier runner revoke FOLDER [CONDUIT]` withdraws one approval or all approvals in the folder.
`atelier runner unexpose FOLDER` removes the registration and its approvals. The project stays
on disk. To forget the identity, stop the runner and run `atelier runner unpair`. This also clears
its registrations, approvals, job mappings and buffered records. Revoke its bearer on the control
plane to invalidate other copies.

`atelier runner start` prints job IDs, outcomes, local refusal reasons and connection errors.
Use `atelier runner status` in another terminal for heartbeat time, online state, the last error
and the five most recent refusals.

The pairing command prompts for a single-use invitation with echo disabled. A successful exchange
creates a runner identity in the user store's private `runner/` directory. One identity is bound to
one HTTPS origin. It connects directly and ignores `HTTPS_PROXY` and other proxy variables.
The bearer is stored in plaintext in `runner/runner.sqlite3`. Helpers and agents running as the
same OS user can read it and impersonate the runner, including claiming jobs and reporting results.
Choose public labels and conduit names. The runner advertises those names and locally generated
folder IDs.

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
Use a bundled helper for shell redirection. The runner refuses `tool:hitl` steps, including in
child conduits, because it runs unattended. Package complex shell programs and their helper
dependencies in the bundle and resolve them relative to the script. Locally approved helpers and agents are trusted code with access to
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

The runner omits names containing the bearer, a string value from user settings, a known token
pattern or the local hostname. It also omits names containing environment values at least eight
characters long. For variable names containing TOKEN, KEY, SECRET, PASS, AUTH or CRED
(case-insensitive), it filters every nonempty value. Add private values as a JSON array of strings in
`~/.atelier/runner/redact.json` (relative to the configured global store). A filtered public folder
label falls back to the folder ID. The same rules apply before buffering and again on transmission,
including retransmission. A runner has no command or protocol option for raw uploads.

Sequence numbers span all jobs of one identity, start at 1 and persist in the local SQLite store.
On each poll the runner sends up to 100 rows after the server's contiguous cursor. The server
checks the owner and folder of each job, rejects gaps, and commits the batch atomically. Retries of
previously committed sequence numbers do not create additional records. A tenant view reads
these rows in sequence order. It displays `online` and the last heartbeat from the runner's most
recent authenticated poll. See the [test reference peer](../../tests/runner/plane.py).

The runner applies the user setting `cleanupPeriodDays` at startup on its first successful poll
and daily thereafter. It removes expired verified conduit copies while idle and expires buffered
records after the server acknowledges their sequence. Unacknowledged records stay available for
replay. Sequence numbers continue across cleanup, and job mappings continue to prevent reruns.
