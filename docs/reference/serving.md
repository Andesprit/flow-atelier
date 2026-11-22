# HTTP API (`atelier serve`)

`atelier serve` boots a single process that hosts both the HTTP /
WebSocket API and the scheduler daemon. It is the entry point the
Flow Atelier visual frontend connects to. Startup prints the project store and
the active schedule directory. With `--no-scheduler` the API remains available
and startup reports that the scheduler is disabled.

| Method   | Path                  | Notes                                     |
| -------- | --------------------- | ----------------------------------------- |
| `GET`    | `/conduits`           | List conduits                             |
| `GET`    | `/conduits/:name`     | Read one                                  |
| `POST`   | `/conduits`           | Create (201 on success, 409 on collision) |
| `PATCH`  | `/conduits/:name`     | Partial update                            |
| `DELETE` | `/conduits/:name`     | Delete                                    |
| `POST`   | `/conduits/open-path` | Reveal flow run path in OS file explorer  |
| `POST`   | `/tasks/run`          | Run an ad-hoc one-task conduit            |
| `GET`    | `/schedules`          | List active schedules                     |
| `POST`   | `/schedules`          | Create                                    |
| `DELETE` | `/schedules/:id`      | Soft-delete                               |
| `GET`    | `/flows`              | List prior flows                          |
| `GET`    | `/flows/:id/logs`     | Per-flow log entries                      |
| `GET`    | `/flows/:id`          | Tasks, dependencies and progress          |
| `GET`    | `/flows/:id/tasks/:task/log` | One task's rounds and actions, secrets masked |
| `WS`     | `/ws/run-conduit`     | Run flows, HITL + interactive AI turns    |
| `WS`     | `/ws/flows/:id`       | The run page's feed: its map and one task's log, pushed as they change |

Binds to `127.0.0.1:8000` by default; pass `--host 0.0.0.0` to expose
on the LAN — which requires `ATELIER_API_TOKEN`, see [Security](../configuration/security.md).
`--cors-origin <origin>` lets another page call the API, such as the Vite dev
server at `http://localhost:5173`; it is repeatable.

Conduits and flows resolve exactly as they do on the CLI — `./.atelier`
first, then `~/.atelier` — so the conduits `atelier init` created in the
directory you started the server from are the ones the UI shows.
Schedules are the exception: they live in `~/.atelier/schedules/`, since
one daemon serves every project. Like `atelier run --input`, a `run`
envelope on `/ws/run-conduit` whose `inputs` carry a key the conduit
neither declares nor references comes back as `flow_failed` naming the
key and its closest match, instead of starting the flow.

## Interactive turns over the WebSocket

Three envelopes carry the conversation. They are additive — a client
that ignores them still runs flows exactly as before.

| From   | `type`                | Fields                                    |
| ------ | --------------------- | ----------------------------------------- |
| server | `agent_message`       | `flow_id`, `task`, `text`                 |
| server | `agent_input_request` | `flow_id`, `task`, `request_id`, `prompt` |
| client | `agent_input_answer`  | `flow_id`, `request_id`, `answer`         |

`agent_message` is one chunk of agent prose, sent as the agent writes
it, so the client can show the question before the request to answer it
arrives. `agent_input_request` is the turn being handed back to you.

The `request_id` from a request must be echoed verbatim in the answer.
That is what allows several interactive tasks in one flow to be waiting
at the same time: each reply goes to the prompt that asked for it, not
to whichever prompt happens to be pending. An answer carrying an
unknown or already-used `request_id` comes back as an `error` envelope.

The socket is only the local transport between the UI and the
`atelier serve` process on your machine. The harness itself still runs
as that agent's own CLI under your existing login for it, so an
interactive conversation over the WebSocket needs no LLM API key —
exactly as on the terminal.

## Runs the server owns

`POST /mobile/runs` starts a run that belongs to the server instead of a
socket, so it keeps going when the page that started it closes. It still
stops when `atelier serve` stops. A launch with `"interactive": true` tells
the server the client can answer people's input: each `tool:hitl` step and
each question from an interactive agent then waits for an answer, and
`GET /flows` lists the run as `needs_input` while it waits. Without the flag,
a conduit with a `tool:hitl` step is refused as before.

| Method | Path | Notes |
| ------ | ---- | ----- |
| `GET`  | `/mobile/inputs?flow_id=<id>` | What runs are waiting for, oldest first; `flow_id` is optional |
| `POST` | `/mobile/runs/:flow_id/inputs/:request_id` | Answer one request: `{"answers": {...}}` for a form, `{"answer": "..."}` for a question |

Each request has a `kind` of `form` (a `tool:hitl` step, with its `fields`)
or `question` (an agent's question, in `prompt`). `flow_id` is the run as
`GET /flows` lists it; `sub_flow_id` names a nested conduit's run when that
is where the question came from. An answer to a request that is no longer
waiting returns 404, and a form answer without every field returns 422.
Waiting requests live in the server's memory, so they end with their run.

See the [HTTP API reference](http-api.md) for the endpoint definitions and [Dashboard and run pages](../guides/dashboard.md) for the live run view.
