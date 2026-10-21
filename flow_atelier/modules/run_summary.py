"""The run page's result and next step, read from what ``diagnose`` reads.

Recovery advice comes from :func:`~flow_atelier.modules.diagnose.build_report`
unchanged: this module only picks what a page needs, masks it, bounds it, and
withholds advice the page cannot stand behind (a run that moved while it was
read, or a nested run whose recovery belongs to its parent).
"""
from __future__ import annotations

import shlex
from typing import Any

import yaml

from flow_atelier.modules.diagnose import UNREADABLE, _bound, build_report
from flow_atelier.schemas.api import RunFailure, RunNextStep, RunResult, RunSummary
from flow_atelier.schemas.progress import Progress, TaskStatus

#: Preview size of one saved result; ``atelier outputs`` prints all of it.
RESULT_MAX_CHARS = 2000


def _shape(progress: Progress) -> dict[str, Any]:
    """The whole progress record: what has to stay the same for two readings
    to describe one state. Not a chosen subset, because diagnose reads the
    runner's pid, host and start time to decide what to advise.
    """
    return progress.model_dump()


def _changes_things(command: str | None) -> bool:
    """Whether a suggested command starts, resumes or stops work."""
    return bool(command) and command.startswith(("atelier run", "atelier stop"))


def _result(task: str, status: str, outputs: dict[str, Any]) -> RunResult:
    """One task's saved result, masked before it is cut so no secret is split."""
    from flow_atelier.cli.rendering.render import _redact

    if task not in outputs:
        return RunResult(task=task, status=status, state="missing")
    value = outputs[task]
    if value is None:
        return RunResult(task=task, status=status, state="none")
    text = value if isinstance(value, str) else yaml.safe_dump(value, sort_keys=False)
    if not text:
        return RunResult(task=task, status=status, state="empty")
    masked = _redact(text)
    return RunResult(
        task=task,
        status=status,
        state="text",
        text=masked[:RESULT_MAX_CHARS],
        chars=len(text),
        truncated=len(masked) > RESULT_MAX_CHARS,
    )


def build_summary(atelier: Any, flow_id: str) -> RunSummary:
    """Read one run's saved results and diagnose's advice as one snapshot.

    :param atelier: the :class:`~flow_atelier.core.atelier.Atelier` facade.
    :param flow_id: the full flow id.
    :returns: the summary; nothing is written and nothing is run.
    :raises DiagnoseError: the run is unknown or its progress is unreadable.
    """
    from flow_atelier.cli.rendering.render import _redact

    store = atelier.store
    try:
        before = store.read_progress(flow_id)
    except Exception:  # noqa: BLE001 - build_report raises the user-facing error
        before = None
    report = build_report(atelier, flow_id)
    try:
        outputs: dict[str, Any] | None = store.read_outputs(flow_id)
    except UNREADABLE:
        outputs = None
    try:
        entries = store.read_logs(flow_id)
    except UNREADABLE:
        entries = []
    # Last, like diagnose's own check: the outputs and logs above were read
    # after its snapshot, so only an unchanged progress record after them
    # makes this one settled reading.
    try:
        after = store.read_progress(flow_id)
    except UNREADABLE:
        after = None
    settled = (
        report.snapshot.consistent
        and before is not None
        and after is not None
        and _shape(before) == _shape(after)
    )
    progress = after or before

    holder = store._flow_dir(flow_id).parent.parent
    parent_flow_id = None if holder == store.base_dir else holder.name

    base = store.base_dir.resolve()
    if base.name == ".atelier":
        workspace: str | None = str(base.parent)
        prefix = f"cd {shlex.quote(workspace)} && "
        store_note = None
    else:
        workspace = None
        prefix = f"ATELIER_ATELIER_DIR={shlex.quote(str(base))} "
        store_note = (
            f"this server reads runs from {base}, not a project's .atelier folder; "
            "the commands below select that store with ATELIER_ATELIER_DIR"
        )

    statuses = (
        {n: t.status.value for n, t in progress.tasks.items()} if progress else {}
    )
    results: list[RunResult] = []
    if outputs is not None:
        results = [
            _result(task, status, outputs)
            for task, status in statuses.items()
            if task in outputs or status == TaskStatus.completed.value
        ]

    last = {entry.task: entry for entry in entries}
    failures = []
    for failure in report.failures:
        excerpt = None
        entry = last.get(failure.task)
        if entry is not None:
            for channel in ("stderr", "output", "stdout"):
                excerpt = _bound(_redact(getattr(entry, channel) or ""))
                if excerpt is not None:
                    excerpt = excerpt.model_copy(update={"channel": channel})
                    break
        failures.append(
            RunFailure(
                task=failure.task,
                kind=failure.kind,
                exit_code=failure.exit_code,
                reason=_redact(failure.reason) if failure.reason else None,
                channel=excerpt.channel if excerpt else None,
                excerpt=excerpt.text if excerpt else None,
                truncated=excerpt.truncated if excerpt else False,
            )
        )

    settled_note = None
    if not settled:
        settled_note = (
            "the run changed while this page read it, so results are not final and "
            "no recovery command is offered until it settles"
        )
    steps = []
    for step in report.next_steps:
        if _changes_things(step.command) and (not settled or parent_flow_id):
            continue
        steps.append(
            RunNextStep(
                what=step.what,
                command=step.command,
                copy_text=prefix + step.command if step.command else None,
                side_effects=step.side_effects,
                acts=_changes_things(step.command),
            )
        )
    if parent_flow_id and settled and report.observed.state != "completed":
        steps.append(
            RunNextStep(
                what=(
                    f"this run was started by task {progress.invoking_task!r} of run "
                    f"{parent_flow_id}; recover it from that run's page, which gives "
                    "the advice for the whole run"
                    if progress and progress.invoking_task
                    else f"this run is part of run {parent_flow_id}; recover it from "
                    "that run's page, which gives the advice for the whole run"
                ),
            )
        )

    quoted = shlex.quote(flow_id)
    return RunSummary(
        flow_id=flow_id,
        status=progress.status.value if progress else report.saved.status,
        observed_state=report.observed.state,
        observed_certainty=report.observed.certainty,
        observed_note=report.observed.note,
        settled=settled,
        settled_note=settled_note,
        final=settled and report.observed.state == "completed"
        and report.saved.status == "completed",
        workspace=workspace,
        store=str(base),
        store_note=store_note,
        parent_flow_id=parent_flow_id,
        parent_task=progress.invoking_task if progress else None,
        results=results,
        results_unavailable=(
            "outputs.yaml cannot be read, so whether each task kept a result is unknown"
            if outputs is None
            else None
        ),
        full_results_command=prefix + f"atelier outputs {quoted}",
        failures=failures,
        unavailable=report.unavailable,
        next_steps=steps,
    )
