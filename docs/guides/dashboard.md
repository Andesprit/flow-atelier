# Dashboard and run pages

Start the dashboard with `atelier serve` and open the address it prints. Use
`--no-scheduler` when you only want the dashboard and API. In the Designer,
open a conduit to edit its tasks and dependencies.

![Flow Atelier Designer](../img/designer.jpg)

## Run pages

Every run also has a page at `/runs/<flow_id>`: a map of its tasks with the
ones running now framed, and the log of whichever task you click. It works for
runs started from the CLI, the dashboard or the scheduler. The server follows
the run's files and pushes each new line over `/ws/flows/<flow_id>` within a
quarter second, shell output included.

`atelier run` and `atelier ask` print that page's address when a server at the
configured address can load the flow, for example
`· run page http://127.0.0.1:8000/runs/20260925_0eb21391_hello`, so an agent
driving the CLI can hand the link to you. The page loads only while
`atelier serve` runs from the same project directory as the run. Set
`ATELIER_SERVE_URL` when you serve on another host or port. If the server is absent,
unresponsive, or serves another project, the CLI gives an instruction instead:
run `atelier serve --port 0 --no-scheduler` from this project, then open `/runs/<flow_id>` at
the address it prints. Port 0 chooses a free port.

A server started only to show a run page does not need to live forever.
`atelier serve --no-scheduler --idle-exit 30` skips scheduling and stops itself after 30 minutes with no open page,
no request, no running flow in that directory and no scheduled run in progress.
Without the flag it runs until you stop it.

## Dashboard chat

On Linux, set `ATELIER_API_TOKEN` when starting the server to enable chat controls.
For Claude on macOS, run `claude setup-token` and set `CLAUDE_CODE_OAUTH_TOKEN`
in the server environment. The sandboxed adapter uses that token to sign in.

Chat previews show the resolved conduit path before a start and the project
destination before saving a recipe. Stop, resume, start, schedule enablement and
recipe saves each need a dashboard click. Chat runs the proposed recipe in an
isolated checkout first. It uses `atelier save-recipe <name>` because that command
takes the complete tested definition as JSON on stdin, validates it, and writes
it to the selected project when you confirm.

See [Where conduits live](../configuration/locations.md) for project selection and run history, and [Security](../configuration/security.md) for server access.
