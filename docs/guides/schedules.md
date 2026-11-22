# Running on a schedule

`atelier schedule daemon` runs conduits on a wall-clock schedule. Each
schedule is one YAML file in `~/.atelier/schedules/`. The CLI, API and daemon
share this directory. Set `ATELIER_GLOBAL_ATELIER_DIR` in the environment to
change the global root. Both `schedule add` and scheduler-enabled `serve`
print the schedule directory. A new schedule's relative `run_path` is anchored
to the working directory where you add it.

**Upgrading project schedules.** Stop old project daemons. In each directory
where one used to run, use `atelier list schedules` to discover legacy jobs,
then `atelier schedule migrate` to move them without starting a daemon.
Scheduling-enabled `serve`, `schedule daemon`, and writable schedule commands
also migrate that project's jobs. The source is the configured project store
(`ATELIER_ATELIER_DIR`, default `./.atelier`). Migration preserves IDs,
fire markers, history and run counts, and anchors relative run paths to the
current directory. A conflict or unreadable state leaves the affected job in
place. Migration reports its path and the reason, then continues with the
remaining jobs. `atelier schedule migrate` prints the moved and kept counts.

The first `atelier schedule daemon` or `atelier serve` to start holds the
scheduler lease for the shared store. While it runs, another `schedule daemon`
reports the owner's PID and exits with an error. Another `serve` reports the
PID and serves its dashboard and API without dispatching jobs. Use
`atelier serve --no-scheduler` to open a dashboard explicitly in that mode.

## Starting the scheduler at login

Schedules fire only while a scheduler runs. To keep one running after you
close every terminal, install the scheduler service once:

```bash
atelier schedule service install
```

The service starts now and at every login:

- On macOS it is a launchd agent in `~/Library/LaunchAgents`. launchd
  restarts it after a crash.
- On Linux it is a systemd user unit in `~/.config/systemd/user`. systemd
  restarts it after a crash. The unit stops when you log out unless
  lingering is on (`loginctl enable-linger`).
- On Windows it is a Task Scheduler task named **Flow Atelier Scheduler**
  that starts when you log on. It runs in a hidden console, so no window
  opens for the scheduler or the agents it starts.

The service runs the schedule directory that `atelier list schedules` shows
when you install it. Run `install` again after you move the Atelier
installation or change `ATELIER_GLOBAL_ATELIER_DIR`.

On macOS and Linux, login services do not read your shell profile. The
service saves the `PATH` of the shell where you run `install`, so the agents
you can run there are found at login. Run `install` again after you add an
agent in a new directory or change `PATH`. API keys exported in your profile
are not saved. Use each agent's own login instead. On Windows the task uses
your Windows user environment variables, including `PATH`.

The service waits while `atelier serve` or another `schedule daemon` holds the
scheduler lease, and takes over when that process stops. On macOS and Windows
its output goes to `~/.atelier/scheduler.log`. On Linux use
`journalctl --user -u atelier-scheduler.service`.

```bash
atelier schedule service status      # installed, running, process ID, logs
atelier schedule service uninstall   # stop it and remove it from login
```

`uninstall` warns when a scheduler still holds the lease afterwards, for
example a running `atelier serve`.

To register a schedule, write a YAML file like the one below and run
`atelier schedule add <file>`:

```yaml
conduit_name: report
inputs:
  date: today
run_path: /abs/path
schedule:
  mode: recurring
  name: weekday mornings
  days: [1, 2, 3, 4, 5]
  times: ["06:00", "12:00"]
```

`days` are `1=Mon` .. `7=Sun`; `times` are `"HH:mm"` 24-hour strings.
One-shots use `mode: once` with a `run_at` ISO datetime instead of
`days` / `times`. Fixed intervals use `mode: interval` with
`every_minutes` (e.g. `every_minutes: 30` for every half hour, `120`
for every two hours) — these repeat forever. `atelier schedule add`
also accepts the same shape in JSON if you prefer that format. Like
`atelier run --input`, it rejects an `inputs` key the conduit neither
declares nor references, so a mistyped key fails at install time instead
of silently running with the default on every fire.

- New or removed schedules are picked up on the next reload tick
(default 30s).
- One-shot schedules remember they fired, so a daemon restart never
re-runs them.
- Each schedule runs at most one instance at a time; missed fires
are coalesced.
- `atelier schedule run-now <id-or-name>` fires a schedule
immediately, bypassing the daemon.

## Running unattended

To run a conduit on a schedule in the cloud, with Claude Code and Codex
logged in and no one at the keyboard, see
[Running unattended on Cloud Run](../running-unattended.md). It covers
storing each agent's login as a secret, an image with the adapters
preinstalled, and a job that checks both logins with a test prompt before
it starts.
