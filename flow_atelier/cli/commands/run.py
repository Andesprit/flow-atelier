"""`atelier run` command, and the terminal driver every flow-running command shares."""
from __future__ import annotations

import asyncio
import contextlib
import difflib
import http.client
import json
import shlex
import sys
import threading
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TextIO
from urllib.parse import quote, urlsplit

import typer
import yaml
from pydantic import ValidationError
from rich.markup import escape

from flow_atelier.cli._shared import (
    _exit_unknown_conduit,
    _parse_input_files,
    _parse_inputs,
    _resolve_flow_id,
    cli_error,
    console,
    json_diagnostics,
    mark_activity,
    parse_agents_option,
    parse_worktrees_option,
    seconds_since_activity,
)
from flow_atelier.cli.commands.recipe_diagnostics import RecipeSource, validation_errors
from flow_atelier.cli.main import AtelierCommand, app
from flow_atelier.cli.rendering.render import (
    _render_orchestration_msg,
    format_conduit_error,
    render_heartbeat,
    render_run_footer,
    render_task_event,
    render_task_start,
)
from flow_atelier.core.atelier import Atelier
from flow_atelier.core.settings import AtelierSettings
from flow_atelier.modules.binding import BindingError
from flow_atelier.modules.engine import (
    ConduitValidationError,
    accepted_input_keys,
    current_nested_path,
)
from flow_atelier.modules.loop_report import unmet_loop_messages
from flow_atelier.modules.nested_failure import brief_failure_reason
from flow_atelier.modules.workspace import WorkspaceError, check_selectors
from flow_atelier.schemas.conduit import (
    Conduit,
    display_conduit_name,
    parse_inline_conduit_name,
)
from flow_atelier.schemas.flow import parse_flow_id
from flow_atelier.schemas.log import TaskEvent
from flow_atelier.schemas.progress import TaskStatus

# How long the run stream must stay silent before the heartbeat speaks up.
HEARTBEAT_SECONDS = 30.0

# `run` and `ask` exit with this when `atelier stop` or any SIGTERM ended the
# run (128 + SIGTERM, as a shell reports it).
STOPPED_EXIT_CODE = 143

# How often the heartbeat wakes to check for silence.
_HEARTBEAT_TICK_SECONDS = 1.0


@dataclass
class _RunResult:
    """Reserve stdout for the record and collect diagnostics until execution starts."""

    stdout: TextIO
    startup: contextlib.ExitStack

    def emit(self, flow_id: str, status: str) -> None:
        typer.echo(json.dumps({"flow_id": flow_id, "status": status}), file=self.stdout)


class _RunCommand(AtelierCommand):
    """Keep argument and startup errors structured, then stream the run live."""

    def invoke(self, ctx: typer.Context) -> object:
        if not ctx.params.get("json_mode"):
            return super().invoke(ctx)
        # Executors and interactive prompts also write to stdout. Reserve the
        # original stream for the result before constructing any of them.
        stdout = sys.stdout
        with contextlib.redirect_stdout(sys.stderr), contextlib.ExitStack() as startup:
            startup.enter_context(json_diagnostics())
            ctx.obj = _RunResult(stdout, startup)
            return super().invoke(ctx)


def _run_page_available(base: str, flow_id: str, token: str) -> bool:
    """Bound the optional check even during DNS stalls or trickling HTTP responses."""
    result = [False]

    def probe() -> None:
        result[0] = _probe_run_page(base, flow_id, token)

    try:
        worker = threading.Thread(target=probe, daemon=True)
        worker.start()
        worker.join(timeout=0.6)
        return not worker.is_alive() and result[0]
    except Exception:  # noqa: BLE001 — even thread exhaustion must not break a run
        return False


def _probe_run_page(base: str, flow_id: str, token: str) -> bool:
    """Check that the configured server can load this run, without following redirects."""
    connection = None
    try:
        url = urlsplit(base)
        if url.scheme not in ("http", "https") or not url.hostname:
            return False
        transport = (
            http.client.HTTPSConnection if url.scheme == "https" else http.client.HTTPConnection
        )
        connection = transport(url.hostname, url.port, timeout=0.1)
        connection.connect()
        connection.sock.settimeout(0.5)
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        path = f"{url.path.rstrip('/')}/flows/{quote(flow_id, safe='')}"
        connection.request("GET", path, headers=headers)
        with connection.getresponse() as response:
            if response.status != 200:
                return False
            # A non-Atelier server must not make this optional check consume unbounded data.
            payload = json.loads(response.read(1024 * 1024))
            return isinstance(payload, dict) and payload.get("flow_id") == flow_id
    except Exception:  # noqa: BLE001 — an optional page must never break a run
        return False
    finally:
        if connection is not None:
            with contextlib.suppress(Exception):
                connection.close()


class _RunningTasks:
    """Tracks which tasks are in flight and since when, for the heartbeat.

    ``on_task_starting`` fires once per task while task events fire once
    per iteration, so a repeating task stays tracked until its final
    iteration lands or it reaches a non-completed disposition.
    """

    def __init__(self) -> None:
        """Initialize with no tasks in flight."""
        self._started: dict[str, float] = {}
        self.count = 0

    def start(self, task_name: str, *, counted: bool = True) -> int:
        """Record ``task_name`` as running and return its 1-based position.

        :param task_name: name of the task entering the running state.
        :returns: how many tasks have started so far this run.
        """
        self._started[task_name] = time.monotonic()
        if counted:
            self.count += 1
        return self.count

    def finish(self, event: TaskEvent) -> None:
        """Stop tracking ``event.task`` once it has no further iterations.

        :param event: the task event just emitted.
        """
        still_looping = (
            event.status == TaskStatus.completed
            and event.iteration < event.of
            and event.loop_condition_met is not True
        )
        if not still_looping:
            self._started.pop(event.task, None)

    def elapsed(self) -> dict[str, float]:
        """Return seconds elapsed for each in-flight task.

        :returns: mapping of task name to seconds since it started.
        """
        now = time.monotonic()
        return {name: now - started for name, started in self._started.items()}


async def _with_heartbeat(coro, running: _RunningTasks):
    """Await ``coro`` while emitting a periodic "still working" line.

    The heartbeat only prints during genuine silence, so an actively
    streaming run never sees it. Its job is the quiet stretches — an
    ``npx`` cold start, a multi-minute tool call, a ``tool:bash`` task
    that emits no steps — where the terminal is otherwise indistinguishable
    from a hang.

    :param coro: the engine coroutine to run.
    :param running: tracker naming the in-flight tasks.
    :returns: whatever ``coro`` returns.
    """

    async def _beat() -> None:
        """Print a status line whenever the stream has been quiet too long."""
        while True:
            await asyncio.sleep(_HEARTBEAT_TICK_SECONDS)
            if seconds_since_activity() >= HEARTBEAT_SECONDS:
                console.print(render_heartbeat(running.elapsed()))
                mark_activity()

    beat = asyncio.create_task(_beat())
    try:
        return await coro
    finally:
        beat.cancel()
        # Await the cancellation rather than leaving it to `asyncio.run`'s
        # shutdown sweep, so the task is provably done before the loop closes.
        with contextlib.suppress(asyncio.CancelledError):
            await beat


def drive_flow(
    start: Callable[..., Awaitable[str]],
    *,
    total_tasks: int,
    verb: str = "running",
    flow_id: str | None = None,
    resume_hint: bool = True,
    machine: _RunResult | None = None,
) -> str:
    """Run one flow to completion on the terminal and return its id.

    ``start`` receives the engine callbacks ``on_task_event``,
    ``on_flow_started`` and ``on_task_starting`` as keyword arguments and
    returns the engine coroutine. Task banners, events, the heartbeat, the
    footer and the stopped/failed epilogue all render here.

    :param start: builds the engine coroutine from the three callbacks.
    :param total_tasks: task count for the ``[i/total]`` banner, or 0 to omit it.
    :param verb: banner verb, ``running`` or ``resuming``.
    :param flow_id: the id when resuming an existing flow. A fresh run learns
        its id from the engine and announces it.
    :param resume_hint: print ``atelier run --resume <id>`` after a failure.
    :param machine: stream for the completion record when running with ``--json``.
    :returns: the flow id the engine returned.
    """
    collected: list[TaskEvent] = []
    # In JSON mode a resume must pass the engine's eligibility checks before
    # its existing ID can identify a result from this invocation.
    captured_id = None if machine else flow_id
    running = _RunningTasks()
    settings = AtelierSettings()
    page_base = settings.serve_url.rstrip("/")

    def _task_label(name: str) -> str:
        parents = [
            f"{task} {iteration}/{of}" if of > 1 else task
            for task, iteration, of in current_nested_path()
        ]
        return " > ".join((*parents, name))

    def _announce_page(fid: str) -> None:
        if _run_page_available(page_base, fid, settings.api_token):
            message = f"run page {page_base}/runs/{fid}"
        else:
            message = (
                f"run page: no atelier serve for this project at {page_base}; "
                "start one here with: atelier serve --port 0 --no-scheduler; "
                f"open /runs/{fid} at its printed address"
            )
        # soft_wrap keeps the URL on one line when the output is piped to a
        # log, where Rich would otherwise wrap at 80 columns.
        console.print(
            _render_orchestration_msg(message),
            soft_wrap=True,
        )

    def _on_event(event: TaskEvent) -> None:
        if flow_id and captured_id is None:
            _on_started(flow_id)
        if (
            event.flow_id != captured_id
            and "Traceback (most recent call last)" in event.stderr
        ):
            event = event.model_copy(update={"stderr": brief_failure_reason(event.stderr)})
        displayed = event.model_copy(update={"task": _task_label(event.task)})
        # The footer counts this flow's own tasks, as the [i/total] banner does;
        # a nested body's tasks are already inside their calling task's result.
        if not current_nested_path():
            collected.append(displayed)
        running.finish(displayed)
        mark_activity()
        render_task_event(displayed, console)

    def _on_started(fid: str) -> None:
        nonlocal captured_id
        first = captured_id is None
        if first:
            captured_id = flow_id or fid
            if machine:
                machine.startup.close()
        if flow_id is None:
            name, _, _ = parse_flow_id(fid)
            if current_nested_path():
                console.print(_render_orchestration_msg(
                    f"starting {display_conduit_name(name)}"
                ))
            else:
                console.print(_render_orchestration_msg(f"starting flow {fid}"))
        # The engine also reports each nested tool:conduit run it starts. The
        # flow this command drives is the first one (or the resumed one), and
        # its page links to the nested runs.
        if first:
            _announce_page(flow_id or fid)

    def _on_task_starting(task_name: str, tool: str) -> None:
        if flow_id and captured_id is None:
            _on_started(flow_id)
        nested = bool(current_nested_path())
        label = _task_label(task_name)
        index = running.start(label, counted=not nested)
        mark_activity()
        console.print()
        console.print(render_task_start(
            label, tool, index, 0 if nested else total_tasks, verb=verb,
        ))

    # The engine does not announce a resumed flow, so name its page here.
    if flow_id is not None and not machine:
        _announce_page(flow_id)
    coro = start(
        on_task_event=_on_event,
        on_flow_started=_on_started,
        on_task_starting=_on_task_starting,
    )
    try:
        result = asyncio.run(_with_heartbeat(coro, running))
    except (asyncio.CancelledError, KeyboardInterrupt) as exc:
        interrupted = isinstance(exc, KeyboardInterrupt)
        if interrupted and not machine:
            raise
        exit_code = 130 if interrupted else STOPPED_EXIT_CODE
        if machine and captured_id is None:
            raise cli_error("cancelled", "run cancelled", json_mode=True, exit_code=exit_code)
        status = "failed" if interrupted else "stopped"
        render_run_footer(collected, console)
        console.print(f"[yellow]flow {status}[/yellow]")
        if captured_id:
            console.print(f"[yellow]flow_id:[/yellow] {captured_id}")
            console.print(f"[dim]→ atelier diagnose {captured_id}[/dim]", soft_wrap=True)
            if machine:
                machine.emit(captured_id, status)
        raise typer.Exit(code=exit_code)
    except Exception as exc:  # noqa: BLE001
        if isinstance(exc, (BindingError, WorkspaceError)) and not (machine and captured_id):
            raise cli_error(
                "invalid_arguments", str(exc), exit_code=exc.code, json_mode=bool(machine),
                out=console, parameter="--agent" if isinstance(exc, BindingError) else "--worktree",
            ) from exc
        if machine and captured_id is None:
            code = (
                "invalid_conduit" if isinstance(exc, ConduitValidationError) else "command_failed"
            )
            raise cli_error(code, str(exc), json_mode=True) from exc
        render_run_footer(collected, console)
        console.print(f"[red]flow failed:[/red] {escape(str(exc))}")
        if captured_id:
            console.print(f"[red]flow_id:[/red] {captured_id}")
            console.print(f"[dim]→ atelier diagnose {captured_id}[/dim]", soft_wrap=True)
            if resume_hint:
                console.print(f"[dim]→ atelier run --resume {captured_id}[/dim]")
            if machine:
                machine.emit(captured_id, "failed")
        raise typer.Exit(code=1)
    render_run_footer(collected, console)
    for warning in unmet_loop_messages(Atelier().get_status(result)):
        console.print(f"[yellow]⚠ {escape(warning)}[/yellow]")
    console.print(f"[green]flow_id:[/green] {result}")
    if machine:
        machine.emit(result, "completed")
    return result


def _load_conduit(atelier: Atelier, name: str, *, json_mode: bool) -> Conduit | None:
    """Read conduit ``name``, exiting 1 with a readable message when it is malformed.

    :param atelier: Atelier whose store to read from.
    :param name: the conduit to load.
    :returns: the conduit, or ``None`` when no conduit of that name exists.
    """
    try:
        return atelier.store.read_conduit(name)
    except FileNotFoundError:
        return None
    except (yaml.YAMLError, ValueError) as exc:
        if isinstance(exc, ValidationError):
            path = atelier.store.conduit_dir(name) / "conduit.yaml"
            message = "\n".join(validation_errors(exc, RecipeSource(str(path), name)))
        else:
            message = format_conduit_error(exc)
        raise cli_error(
            "invalid_conduit", f"invalid conduit: {message}", conduit=name,
            hint=f"→ fix conduits/{name}/conduit.yaml", json_mode=json_mode, out=console,
        ) from exc


def _reject_unknown_inputs(conduit: Conduit, inputs: dict[str, str], *, json_mode: bool) -> None:
    """Exit 1 when an ``--input`` key is one the conduit can never use.

    A key needs no ``inputs:`` declaration: a ``{{inputs.<key>}}`` reference
    in any task body or task input map is enough. Anything else the engine
    silently drops, so a typo would otherwise prompt again for the "missing"
    key or quietly run with a default.

    :param conduit: the loaded conduit the inputs are meant for.
    :param inputs: the parsed ``--input`` map.
    """
    accepted = accepted_input_keys(conduit)
    unknown = [k for k in inputs if k not in accepted]
    if not unknown:
        return
    messages = []
    for key in unknown:
        close = difflib.get_close_matches(key, sorted(accepted), n=1)
        hint = f" — did you mean {close[0]}?" if close else ""
        messages.append(f"unknown input: {key}{hint}")
    accepts = (
        f"accepts --input: {', '.join(sorted(accepted))}"
        if accepted
        else "accepts no --input"
    )
    raise cli_error(
        "invalid_arguments", "\n".join(messages), hint=f"{conduit.name} {accepts}",
        parameter="--input", allowed=sorted(accepted), json_mode=json_mode, out=console,
    )


def _collect_missing_inputs(conduit: Conduit, inputs: dict[str, str], *, json_mode: bool) -> None:
    """Prompt for missing inputs in interactive human mode, otherwise exit 2.

    :param conduit: the conduit about to run.
    :param inputs: the ``--input`` map, extended in place with typed answers.
    """
    missing = [
        k for k, spec in conduit.inputs.items() if k not in inputs and spec.default is None
    ]
    if not missing:
        return
    if json_mode or not sys.stdin.isatty():
        # Flags only, never a whole command: a rebuilt command would drop the
        # inputs and options already given, and echoing them could leak secrets.
        flags = " ".join(f"--input {shlex.quote(f'{k}=<value>')}" for k in missing)
        if json_mode:
            raise cli_error(
                "missing_inputs", f"missing required inputs: {', '.join(missing)}",
                exit_code=2, json_mode=True, missing=missing, hint=f"Add {flags}",
            )
        console.print(f"[red]missing required inputs:[/red] {escape(', '.join(missing))}")
        for key in missing:
            description = conduit.inputs[key].description
            console.print(f"  {escape(key)}" + (f" — {escape(description)}" if description else ""))
        console.print(
            "[dim]→ add these flags to the command you just ran, keeping its other "
            "inputs and options; replace each <value> (or use --input-file key=path "
            "for a file), then run it again:[/dim]"
        )
        console.print(f"  {escape(flags)}", soft_wrap=True)
        raise typer.Exit(code=2)

    from flow_atelier.cli.rendering.multiline_input import (
        SUBMIT_HINT,
        multiline_input_sync,
    )

    try:
        for key in missing:
            inputs[key] = multiline_input_sync(
                f"  {key} ({conduit.inputs[key].description}): ",
                hint=SUBMIT_HINT,
            )
    except KeyboardInterrupt:
        print()
        raise typer.Exit(code=130)


@app.command(
    "run",
    cls=_RunCommand,
    help=(
        "Start a new flow for the named conduit. "
        "Use --input key=value to pass inputs, or --input-file key=path to "
        "pass a text file's contents as one input. "
        "Use --agent task=harness or call.task=harness to choose agents. "
        "Use --worktree task to give that task its own Git checkout. "
        "Use --resume <flow_id> to pick up a failed or crashed run. "
        "Use --again <flow_id> to start a fresh run reusing a past flow's inputs."
    ),
)
def run_cmd(
    ctx: typer.Context,
    conduit_name: str = typer.Argument(
        None, help="Name of the conduit to run (not needed with --resume or --again)."
    ),
    inputs_raw: list[str] = typer.Option(
        [],
        "--input",
        "-i",
        help="key=value input (repeatable).",
    ),
    input_files_raw: list[str] = typer.Option(
        [],
        "--input-file",
        help=(
            "key=path input read from a UTF-8 text file (repeatable). "
            "The loaded text is saved with the run, so --again replays it."
        ),
    ),
    agents_raw: list[str] = typer.Option(
        [],
        "--agent",
        help=(
            "task=harness or call.task=harness: choose an agent here or in a "
            "static child conduit for this run (repeatable). The files are never "
            "changed; with --resume or --again the run's saved choices are "
            "reused and these override them."
        ),
    ),
    worktrees_raw: list[str] = typer.Option(
        [],
        "--worktree",
        help=(
            "task: run that top-level agent or bash task in its own detached "
            "Git worktree, cut from this checkout's HEAD (repeatable). Two "
            "workers can then edit the same file without colliding. The "
            "checkouts are kept after the run and nothing is merged back; "
            "--resume continues in them, --again makes fresh ones."
        ),
    ),
    show_steps: bool = typer.Option(
        True,
        "--show-steps/--hide-steps",
        help="Stream intermediate thinking and tool activity live (default: on).",
    ),
    resume_from: str | None = typer.Option(
        None,
        "--resume",
        help="Resume a failed or crashed flow by its id (prefix or 'latest' ok).",
    ),
    again_from: str | None = typer.Option(
        None,
        "--again",
        help=(
            "Start a fresh run of a past flow by id (prefix or 'latest' ok), "
            "reusing its saved inputs."
        ),
    ),
    json_mode: bool = typer.Option(
        False, "--json", help="Write flow_id and final status as JSON. Stream progress to stderr.",
    ),
) -> None:
    """Start a new flow, resume a failed one, or re-run a past one.

    :param ctx: command context holding the JSON result stream when requested.
    :param conduit_name: name of the conduit to execute.
    :param inputs_raw: list of ``key=value`` input strings collected from ``--input``.
    :param input_files_raw: list of ``key=path`` strings collected from
        ``--input-file``; each file's text becomes that key's value.
    :param agents_raw: list of ``task=harness`` or dotted child selections
        collected from ``--agent`` for this run.
    :param worktrees_raw: list of task names collected from ``--worktree``;
        each gets its own Git checkout for this run.
    :param show_steps: when true, stream intermediate thinking and tool activity live.
    :param resume_from: flow id (or unique prefix) of a failed run to resume.
    :param again_from: flow id (or unique prefix) of a past run to re-run from
        scratch, reusing its saved inputs (overridable via ``--input``).
    :param json_mode: write one completion record on stdout and progress on stderr.
    """
    machine = ctx.obj if json_mode else None
    atelier = Atelier()
    agents = parse_agents_option(agents_raw, json_mode=json_mode)
    worktrees = parse_worktrees_option(worktrees_raw, json_mode=json_mode)

    if resume_from is not None and again_from is not None:
        raise cli_error(
            "invalid_arguments", "error: --resume and --again are mutually exclusive",
            exit_code=2, out=console, allowed=["--resume", "--again"], json_mode=json_mode,
        )

    # Resume continues the flow's saved inputs; there is nothing for a new
    # value to change. Refuse rather than read the file and ignore it.
    if resume_from is not None and input_files_raw:
        raise cli_error(
            "invalid_arguments", "error: --resume and --input-file are mutually exclusive",
            out=console, allowed=["--resume", "--input-file"], json_mode=json_mode,
            hint="→ --resume reuses the flow's saved inputs; use --again <flow_id> to change one",
        )

    # A resume continues in the directories the first attempt recorded — that
    # is what makes a failed worker's partial edits recoverable. Silently
    # ignoring a new selection, or honouring it and abandoning those edits,
    # would both be worse than saying no.
    if resume_from is not None and worktrees:
        raise cli_error(
            "invalid_arguments", "error: --resume and --worktree are mutually exclusive",
            exit_code=2, out=console, allowed=["--resume", "--worktree"], json_mode=json_mode,
            hint="→ --resume continues in the checkouts the run already has;"
                 " use --again <flow_id> to start fresh ones",
        )

    # --resume path: resolve the old flow, skip input prompts
    if resume_from is not None:
        flow_id = _resolve_flow_id(atelier, resume_from, json_mode=json_mode)
        # Surface a malformed conduit with a readable message before resuming,
        # so a YAML or schema error does not reach the generic `flow failed:`
        # handler as a raw exception.
        _load_conduit(atelier.for_flow(flow_id), parse_flow_id(flow_id)[0], json_mode=json_mode)
        if not json_mode:
            console.print(_render_orchestration_msg(f"resuming flow {flow_id}"))
        # No `[i/total]` on resume: the counter numbers the tasks this resume
        # starts, so pairing it with the conduit's full count would read as
        # "[1/5], just started" for a run finishing its last two tasks.
        drive_flow(
            lambda **callbacks: atelier.resume_flow(
                flow_id,
                show_steps=show_steps,
                stoppable=True,
                agents=agents,
                **callbacks,
            ),
            total_tasks=0,
            verb="resuming",
            flow_id=flow_id,
            resume_hint=False,
            machine=machine,
        )
        return

    if again_from is not None:
        flow_id = _resolve_flow_id(atelier, again_from, json_mode=json_mode)
        conduit = _load_conduit(
            atelier.for_flow(flow_id), parse_flow_id(flow_id)[0], json_mode=json_mode
        )
        overrides = _parse_inputs(inputs_raw)
        overrides.update(_parse_input_files(input_files_raw, overrides))
        if conduit is not None:
            _reject_unknown_inputs(conduit, overrides, json_mode=json_mode)
        if not json_mode:
            console.print(_render_orchestration_msg(f"re-running flow {flow_id}"))
        drive_flow(
            lambda **callbacks: atelier.rerun_flow(
                flow_id,
                overrides=overrides,
                show_steps=show_steps,
                stoppable=True,
                agents=agents,
                worktrees=worktrees,
                **callbacks,
            ),
            total_tasks=len(conduit.tasks) if conduit is not None else 0,
            machine=machine,
        )
        return

    if conduit_name is None:
        raise cli_error(
            "invalid_arguments",
            "error: conduit name is required (unless using --resume or --again)",
            json_mode=json_mode, exit_code=2, out=console,
        )

    if inline := parse_inline_conduit_name(conduit_name):
        raise cli_error(
            "invalid_arguments", f"error: {conduit_name} is the internal name of "
            f"the inline body of {inline[0]}.{inline[1]}, not a conduit you can run",
            hint=f"→ atelier run {inline[0]}", json_mode=json_mode, exit_code=2, out=console,
        )

    inputs = _parse_inputs(inputs_raw)
    inputs.update(_parse_input_files(input_files_raw, inputs))
    conduit = _load_conduit(atelier, conduit_name, json_mode=json_mode)
    if conduit is None:
        _exit_unknown_conduit(conduit_name, atelier.store.list_conduits(), json_mode=json_mode)
        return

    _reject_unknown_inputs(conduit, inputs, json_mode=json_mode)

    # Apply the agent selections before the readiness gate, so replacing an
    # agent you are not logged into is enough to run — and so an unavailable
    # replacement is the thing that stops the run.
    try:
        effective = atelier.bind_agents(conduit, agents)
        atelier.require_agent_ready(agents)
        # Before the readiness probe and before any input prompt: a selector
        # the recipe cannot honour should cost nothing at all.
        check_selectors(effective, worktrees)
    except (BindingError, WorkspaceError) as exc:
        raise cli_error(
            "invalid_arguments", str(exc), json_mode=json_mode, exit_code=exc.code, out=console,
            parameter="--agent" if isinstance(exc, BindingError) else "--worktree",
        ) from exc

    # Readiness gate: refuse to start an unrunnable conduit (unregistered tool
    # or a harness CLI missing from PATH) before prompting for inputs or
    # spending any wall-clock/tokens.
    problems = atelier.tool_readiness(effective)
    if problems:
        hint = ""
        if any("no executor registered for tool 'harness:" in problem for problem in problems):
            hint = "→ run atelier harness list to see installed agents"
        raise cli_error(
            "conduit_not_ready", "\n".join(f"cannot run: {problem}" for problem in problems),
            json_mode=json_mode, out=console, hint=hint,
        )

    _collect_missing_inputs(conduit, inputs, json_mode=json_mode)

    if not json_mode:
        console.print(_render_orchestration_msg(f'loading conduit "{conduit_name}"'))
    drive_flow(
        lambda **callbacks: atelier.run_conduit(
            conduit_name,
            inputs,
            show_steps=show_steps,
            stoppable=True,
            agents=agents,
            worktrees=worktrees,
            **callbacks,
        ),
        total_tasks=len(conduit.tasks),
        machine=machine,
    )
