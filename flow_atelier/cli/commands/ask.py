"""The atelier ask command."""

from __future__ import annotations

import asyncio
import json
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import get_args

import typer

from flow_atelier.cli._shared import _resolve_flow_id, cli_error, console
from flow_atelier.cli.commands.run import drive_flow
from flow_atelier.cli.main import app
from flow_atelier.cli.rendering.render import _render_orchestration_msg
from flow_atelier.core.atelier import Atelier
from flow_atelier.modules.liveness import display_status
from flow_atelier.schemas.conduit import AgentPermissions, Conduit, TaskDefinition
from flow_atelier.schemas.flow import new_flow_id, parse_flow_id
from flow_atelier.schemas.harness import HARNESS_TOOL_PATTERN
from flow_atelier.schemas.progress import FlowStatus, TaskStatus
from flow_atelier.services.executor.prompt_sink import AskTurnSink


def _result(atelier: Atelier, flow_id: str, json_mode: bool) -> None:
    progress = atelier.get_status(flow_id)
    result = {"flow_id": flow_id, "status": display_status(progress)}
    if pending := progress.pending_ask:
        result.update(
            status="needs_input", question=pending.question, request_id=pending.request_id
        )
    elif progress.status == FlowStatus.completed:
        result["answer"] = atelier.get_outputs(flow_id).get("chat", "")
    typer.echo(
        json.dumps(result)
        if json_mode
        else result.get("question", result.get("answer", result["status"]))
    )


def _run_turn(
    atelier: Atelier, sink: AskTurnSink | None, message: str, target: Path, tool: str,
    permissions: AgentPermissions | None, timeout: int | None, json_mode: bool,
    resume_from: str | None = None,
) -> None:
    """Drive one machine turn or the original terminal conversation."""
    harness = tool.removeprefix("harness:")
    conduit = Conduit(
        name="ask", description=f"Interactive {harness} conversation.", max_concurrency=1,
        timeout=atelier.settings.default_timeout,
        tasks=[
            TaskDefinition(
                name="chat", description=f"Ask {harness}", tool=tool, depends_on=[],
                task="{{inputs.message}}" if sink else message,
                interactive=True, permissions=permissions, timeout=timeout,
            )
        ],
    )
    if problems := atelier.tool_readiness(conduit):
        raise cli_error("harness_unavailable", "\n".join(problems), json_mode=json_mode)
    if sink is None:
        console.print(_render_orchestration_msg(f'asking {harness} in "{target}"'))
        drive_flow(
            lambda **callbacks: atelier.engine.run(
                conduit, {}, working_dir=target, stoppable=True, **callbacks
            ),
            total_tasks=1, resume_hint=False,
        )
        return

    # A turn refused for usage may already have acted. Never resubmit its reply.
    atelier.engine.usage_limit_max_wait = 0
    flow_id = resume_from or new_flow_id("ask")
    try:
        asyncio.run(atelier.engine.run(
            conduit, {"message": message}, working_dir=target, flow_id=flow_id,
            resume_from=resume_from, stoppable=True,
        ))
    except Exception as exc:  # noqa: BLE001
        raise cli_error(sink.error_code, str(exc), flow_id=flow_id, json_mode=json_mode) from exc
    except asyncio.CancelledError:
        raise cli_error(
            "ask_stopped", "ask stopped", flow_id=flow_id, json_mode=json_mode, exit_code=143,
        )
    _result(atelier, flow_id, json_mode)


@app.command("ask")
def ask_cmd(
    query: str = typer.Argument(..., help="Initial question, or the flow ID of a previous ask."),
    path: Path | None = typer.Option(
        None,
        "--path",
        "-p",
        exists=True,
        file_okay=False,
        dir_okay=True,
        readable=True,
        resolve_path=True,
        help="Working directory for a new conversation.",
    ),
    harness: str | None = typer.Option(
        None,
        "--harness",
        help="Agent name with optional :model:effort (default: claude-code).",
    ),
    permissions: str | None = typer.Option(
        None,
        "--permissions",
        help="Agent permissions: full, edits or read-only (default: ATELIER_AGENT_PERMISSIONS).",
    ),
    json_mode: bool = typer.Option(False, "--json", help="Return the answer or pending question."),
    answer: str | None = typer.Option(None, "--answer", help="Reply to the pending question."),
    request_id: str | None = typer.Option(None, "--request-id", help="Pending question ID."),
    cancel: bool = typer.Option(False, "--cancel", help="Cancel a pending ask."),
    timeout: int | None = typer.Option(
        None,
        "--timeout",
        min=1,
        help="Active seconds per turn. A pending question never expires.",
    ),
) -> None:
    """Ask an agent in a directory, or read and answer a saved question.

    With --path the conversation streams to your terminal. Add --json to return
    after one turn. Read it again with atelier ask FLOW --json. Reply using
    --answer TEXT --request-id ID, or end a pending conversation with --cancel.
    """
    writing = answer is not None or cancel
    if answer is not None and (not answer.strip() or cancel):
        raise typer.BadParameter("--answer needs nonempty text and cannot be used with --cancel")
    if request_id and answer is None:
        raise typer.BadParameter("--request-id requires --answer")

    sink = AskTurnSink() if json_mode or writing else None
    atelier = Atelier(prompt_sink=sink)
    if path is None and sink:
        if harness is not None or permissions is not None or timeout is not None:
            raise typer.BadParameter("start a new ask with --path to set the harness or limits")
        flow_id = _resolve_flow_id(atelier, query, json_mode=json_mode)
        if parse_flow_id(flow_id)[0] != "ask":
            raise cli_error(
                "not_an_ask", "flow is not an ask", json_mode=json_mode, flow_id=flow_id
            )
        if not writing:
            _result(atelier, flow_id, json_mode)
            return
        with atelier.store.lock_ask(flow_id) as acquired:
            if not acquired:
                raise cli_error(
                    "ask_busy", "another process is answering this ask",
                    flow_id=flow_id, json_mode=json_mode,
                )
            progress = atelier.get_status(flow_id)
            pending = progress.pending_ask
            if not pending or (
                answer is not None and (request_id or flow_id) != pending.request_id
            ):
                raise cli_error(
                    "no_pending_question",
                    "no matching pending question; read the flow and pass its --request-id",
                    flow_id=flow_id,
                    json_mode=json_mode,
                )
            if cancel:
                progress.pending_ask = None
                progress.status = FlowStatus.stopped
                progress.tasks["chat"].status = TaskStatus.cancelled
                progress.current_tasks = []
                progress.finished_at = datetime.now(UTC).isoformat()
                atelier.store.write_progress(flow_id, progress)
                _result(atelier, flow_id, json_mode)
                return
            if not progress.run_path or not Path(progress.run_path).is_dir():
                raise cli_error(
                    "ask_directory_unavailable", "the original ask directory is unavailable",
                    flow_id=flow_id, json_mode=json_mode,
                )
            assert answer is not None
            _run_turn(
                atelier, sink, answer, Path(progress.run_path), pending.tool,
                pending.permissions, pending.timeout, json_mode, resume_from=flow_id,
            )
        return

    if path is None:
        raise typer.BadParameter("a new ask requires --path")
    if writing:
        raise typer.BadParameter("use a flow ID without --path to answer or cancel")
    if not query.strip():
        raise typer.BadParameter("query cannot be empty")
    if harness is None:
        harness = "claude-code"
    tool = f"harness:{harness}"
    if not re.fullmatch(HARNESS_TOOL_PATTERN, tool):
        raise typer.BadParameter(
            "expected a lowercase harness name, optionally followed by :model or :model:effort",
            param_hint="--harness",
        )
    levels = get_args(AgentPermissions)
    if permissions is not None and permissions not in levels:
        raise typer.BadParameter(
            f"expected one of: {', '.join(levels)}", param_hint="--permissions"
        )

    _run_turn(atelier, sink, query, path, tool, permissions, timeout, json_mode)
