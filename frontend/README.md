# flow-atelier

A web UI for building, scheduling, and running conduit workflows — DAG-based pipelines that orchestrate bash commands, AI coding agents (Claude Code, Codex, Copilot, Cursor), nested conduits, and human-in-the-loop approval gates.

## Features

- **Dashboard** — browse conduits, view run history, and manage scheduled jobs
- **Designer** — visual DAG editor for authoring conduit workflows with drag-and-drop node placement and dependency edges
- **Kanban** — task board for tracking conduit runs across columns (todo → in progress → done) with live log streaming and HITL prompts

## Tech Stack

- **React 18** + **TypeScript**
- **Vite** — dev server and bundler
- **Tailwind CSS v4** — utility-first styling
- **Zustand** — state management
- **XYFlow** — canvas/DAG rendering in the designer
- **dnd-kit** — drag-and-drop in the kanban board
- **Radix UI** — accessible primitives (dialogs, selects, tooltips, etc.)
- **Vitest** — unit tests (jsdom environment)

## Getting Started

### Install on a phone

The phone needs an HTTPS origin with a trusted certificate. To serve the dashboard
through Caddy on the same machine:

1. Choose a hostname you control (shown here as `atelier.example.com`). Point its
   DNS record at Caddy and route TCP ports 80 and 443 to Caddy. Keep the Atelier
   backend bound to loopback.
   Always set `ATELIER_API_TOKEN` before exposing the port, because a raw TCP
   forward such as `ssh -R` or `socat` adds no proxy headers and can expose an
   unauthenticated server even when it listens only on loopback.
2. Install Atelier with the optional push extra on the serving machine if you
   want notifications, for example `uv tool install 'flow-atelier[push]'`. The
   standalone binary supports the launcher, and push delivery requires the
   Python installation with this extra. From your project directory, supply a long random token from your password
   manager and start the server. In Bash:

   ```bash
   read -rsp 'Atelier API token: ' ATELIER_API_TOKEN
   export ATELIER_API_TOKEN
   atelier serve --host 127.0.0.1 --port 8000 --cors-origin https://atelier.example.com
   ```

3. Install Caddy and save this as `Caddyfile`, substituting your hostname.
   Run `caddy run --config Caddyfile` with permission to bind the HTTPS ports.
   Caddy obtains the certificate and sends the upstream Host expected by Atelier.
   [Caddy reverse proxy setup](https://caddyserver.com/docs/quick-starts/reverse-proxy)

   ```caddyfile
   atelier.example.com {
       reverse_proxy 127.0.0.1:8000 {
           header_up Host 127.0.0.1:8000
       }
   }
   ```

4. Verify that `curl -i https://atelier.example.com/flows` returns **401**.
   Open the same hostname on the phone, enter the token and choose **Install app**
   in Chromium or **Add to Home Screen** in Safari. The HTTPS app shares an
   HttpOnly session cookie between its windows. **Forget token** clears it.
   A server restart or the end of the browser session requires signing in again.

The launcher lists projects from this machine’s global run history. To use a
different folder, enter its absolute path and confirm that its own recipes may
run on this machine. Choose a conduit, fill in its inputs and tap **Launch**.
Closing the phone app leaves the run going. Stopping Atelier, including quitting
the serving desktop app, cancels its mobile runs. Use the interactive dashboard
for live human-input steps. Offline, the app displays a connection message.

Tap **Enable completion notifications** to receive completions from every
project on the machine, including CLI, scheduled and desktop runs. Keep the server
running for delivery. The default VAPID contact is the HTTPS origin used to
subscribe. To set a contact explicitly, add `"vapidSubject": "mailto:ops@example.org"`
to your user `~/.atelier/settings.json` and restart Atelier. Delivery errors appear
under the toggle. Tapping a notice opens the run after authentication.
**Disable completion notifications** removes the device subscription.

### Prerequisites

- **Node.js** ≥ 18
- **npm** (or your preferred package manager)

### Install

```bash
npm install
```

### Environment Variables

A `.env` file in the project root (gitignored) is optional — the app runs with the defaults below, which mirror `.env.example`. Set a variable in `.env` to override it:

| Variable             | Description                              | Default                  |
|----------------------|------------------------------------------|--------------------------|
| `VITE_USE_MOCK_API`  | `"true"` for simulated backend data      | `false` (real backend)   |
| `VITE_BACKEND_URL`   | Base URL for the backend API             | serving origin (same as the UI) |
| `VITE_API_TOKEN`     | Bearer token sent to the backend         | empty (no auth header)   |

When the UI is served by `atelier serve` (the bundled SPA), `VITE_BACKEND_URL`
defaults to the page's own origin, so the frontend automatically targets
whatever host:port the server runs on — no URL to keep in sync. Set it only when
the UI runs on a separate origin (e.g. the Vite dev server on `:5173`).

`VITE_API_TOKEN` has no default on purpose: a shipped token would be a
published shared credential. Leave it empty for local use (`atelier serve` runs
unauthenticated by default) and set it only when the backend is started with a
matching `ATELIER_API_TOKEN`.

To develop the UI against a backend on another port:

```
VITE_BACKEND_URL=http://localhost:8000
```

The backend only answers pages it served itself, so start it with the dev
server's origin:

```bash
atelier serve --cors-origin http://localhost:5173
```

To develop against fixture data with no backend running at all:

```
VITE_USE_MOCK_API=true
```

### Run the dev server

```bash
npm run dev
```

Opens at [http://localhost:5173](http://localhost:5173).

### Build for production

```bash
npm run build
```

Output goes to `dist/`.

### Sync build to the backend

The production build has to land in `flow_atelier/dist/` so the FastAPI app can
serve it (see `flow_atelier/services/api/app.py`). The `Frontend` CI workflow
does this automatically on every pull request and commits the result, so you
normally don't need to. To do it by hand from the repo root:

```bash
cd frontend && npm run build && cd ..
rm -rf flow_atelier/dist
cp -r frontend/dist flow_atelier/dist
```

### Preview the production build

```bash
npm run preview
```

## Project Structure

```
src/
├── components/ui/     # Shared UI primitives (button, dialog, select, etc.)
├── context/           # React context providers (theme)
├── features/
│   ├── dashboard/     # Dashboard page — conduits, history, schedules
│   ├── designer/      # Visual DAG editor — nodes, edges, palette, inspector
│   └── kanban/        # Task board — columns, cards, live task runner
├── hooks/             # Shared React hooks
├── layout/            # App shell — top bar, footer, theme toggle
├── lib/               # Small utilities (cn)
├── pages/             # Route-level page components
├── runner/            # Task execution engine — mock timing, HITL gating, log streaming
├── services/
│   ├── api/           # Backend API calls
│   ├── mock/          # Fixture data for mock mode
│   └── storage/       # LocalStorage persistence
├── styles/            # Global CSS
├── types/             # Shared TypeScript types
└── utils/             # Pure helpers (format, yaml)
```

## Scripts

| Command              | Description                        |
|----------------------|------------------------------------|
| `npm run dev`        | Start dev server (port 5173)       |
| `npm run build`      | Type-check and build for production|
| `npm run preview`    | Serve the production build locally |
| `npm run test`       | Run unit tests once                |
| `npm run test:watch` | Run unit tests in watch mode       |
| `npm run test:coverage` | Run tests with coverage report  |

## How It Works

The app has two operating modes controlled by `VITE_USE_MOCK_API`:

- **Mock mode** — simulates the backend with fixture data and deterministic timing. Conduits run through their tasks with realistic delays and canned log output. No backend required.
- **Real mode** — connects to a flow-atelier backend via REST API. Tasks are submitted and tracked server-side with real execution logs streamed back.

The **runner engine** (`src/runner/`) drives task execution in both modes: it walks the conduit's DAG of tasks, handles human-in-the-loop gates (pausing for user input), streams log lines into the kanban cards, and moves tasks through columns as they progress.
