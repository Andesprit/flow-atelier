"""Template resolution for task prompts and inputs.

Supports five forms:
    {{inputs.<name>}}           — replaced with conduit/hitl input value
    {{<task_name>.output}}      — replaced with upstream task's output
    {{<task_name>.tool}}        — replaced with the tool that task actually ran
    {{loop.previous}}           — this task's previous-iteration output
    {{loop.history}}            — all prior iterations of this task, numbered

Rules:
    - Missing `inputs.x`              -> TemplateError (immediate failure)
    - Reference to task not in outputs (or marked skipped/failed) -> SkipSignal
    - `loop.*` resolves to "" before the first iteration completes
    - `.tool` is the run's *effective* tool, so a prompt that attributes an
      upstream result names the agent that produced it even when this run
      re-pointed that task at another one. It never skips: a tool is known for
      every task in the conduit whether or not that task has run.
"""
from __future__ import annotations

import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any, NamedTuple

_TEMPLATE_RE = re.compile(r"\{\{\s*([^}]+?)\s*\}\}")


def _truncate_entry(text: str, entry_chars: int) -> str:
    """Cap one history entry, keeping its head and tail around a marker.

    :param text: the entry text.
    :param entry_chars: max characters to keep; <= 0 means unlimited.
    :returns: the entry, truncated in the middle when over budget.
    """
    if entry_chars <= 0 or len(text) <= entry_chars:
        return text
    keep = entry_chars // 2
    cut = len(text) - 2 * keep
    return f"{text[:keep]}\n[... {cut} chars truncated ...]\n{text[-keep:]}"


def _format_history(
    history: list[str], limit: int = 0, entry_chars: int = 0
) -> str:
    """Render prior loop iterations as numbered, separated blocks.

    :param history: outputs of completed iterations, oldest first.
    :param limit: max iterations to render, newest kept; <= 0 means all.
    :param entry_chars: max characters per rendered entry; <= 0 means
        unlimited.
    :returns: numbered history string, or "" when no iterations have run.
    """
    omitted = 0
    shown = history
    if limit > 0 and len(history) > limit:
        omitted = len(history) - limit
        shown = history[-limit:]
    blocks = [
        f"--- iteration {i} ---\n{_truncate_entry(out, entry_chars)}"
        for i, out in enumerate(shown, omitted + 1)
    ]
    if omitted:
        blocks.insert(0, f"--- {omitted} earlier iterations omitted ---")
    return "\n\n".join(blocks)


def extract_task_refs(template: str) -> set[str]:
    """Return the task names referenced as ``{{<task>.output}}`` in ``template``.

    ``inputs.*`` and ``loop.*`` expressions are not task references and are
    ignored. Used at validation time to reject references to unknown tasks
    before a flow starts.

    :param template: the raw task/template string to scan.
    :returns: set of referenced task names (may be empty).
    """
    refs: set[str] = set()
    for match in _TEMPLATE_RE.finditer(template):
        expr = match.group(1).strip()
        if expr.startswith("inputs.") or expr in ("loop.previous", "loop.history"):
            continue
        if expr.endswith(".output"):
            refs.add(expr[: -len(".output")])
    return refs


class TemplateRef(NamedTuple):
    """One ``{{...}}`` expression classified against the resolve grammar."""

    kind: str   # "input" | "loop" | "task" | "task_tool" | "conduit_dir" | "unknown"
    value: str  # input name / loop expr / task name / raw expr
    raw: str    # original expression, for error messages


def extract_template_refs(template: str) -> list[TemplateRef]:
    """Classify every ``{{...}}`` expression in ``template``.

    Mirrors :func:`resolve`'s check order exactly so validation and runtime
    never disagree on what a template expression means.

    :param template: the raw task/template string to scan.
    :returns: refs in source-appearance order (may be empty).
    """
    refs: list[TemplateRef] = []
    for match in _TEMPLATE_RE.finditer(template):
        expr = match.group(1).strip()
        if expr.startswith("inputs."):
            refs.append(TemplateRef("input", expr[len("inputs."):], expr))
        elif expr in ("loop.previous", "loop.history"):
            refs.append(TemplateRef("loop", expr, expr))
        elif expr == "conduit_dir":
            refs.append(TemplateRef("conduit_dir", expr, expr))
        elif expr.endswith(".output"):
            refs.append(TemplateRef("task", expr[: -len(".output")], expr))
        elif expr.endswith(".tool"):
            refs.append(TemplateRef("task_tool", expr[: -len(".tool")], expr))
        else:
            refs.append(TemplateRef("unknown", expr, expr))
    return refs


class TemplateError(ValueError):
    """Raised when a template cannot be resolved (hard failure)."""


class SkipSignal(Exception):
    """Raised when a template references a task that was skipped/failed.

    The engine catches this and marks the referencing task as skipped.
    """

    def __init__(self, reason: str):
        """Store the human-readable reason on the exception instance.

        :param reason: short explanation of why the skip was signalled.
        """
        super().__init__(reason)
        self.reason = reason


def resolve(
    template: str,
    inputs: dict[str, Any],
    task_outputs: dict[str, str],
    unavailable_tasks: set[str] | None = None,
    loop_history: list[str] | None = None,
    loop_history_limit: int = 0,
    loop_history_entry_chars: int = 0,
    conduit_dir: Path | str | None = None,
    task_tools: Mapping[str, str] | None = None,
) -> str:
    """Resolve `{{...}}` expressions in ``template``.

    :param template: the template string
    :param inputs: mapping of input name -> value
    :param task_outputs: mapping of task name -> completed output string
    :param unavailable_tasks: names of tasks whose outputs cannot be used
        (skipped / failed / cancelled)
    :param loop_history: this task's prior-iteration outputs, oldest first,
        backing ``{{loop.previous}}`` and ``{{loop.history}}``
    :param loop_history_limit: max iterations ``{{loop.history}}`` renders,
        newest kept; <= 0 means unlimited
    :param loop_history_entry_chars: max characters per rendered history
        entry; <= 0 means unlimited
    :param conduit_dir: absolute dir of the running conduit, backing
        ``{{conduit_dir}}``; when ``None`` the token is treated as unknown
    :param task_tools: mapping of task name -> the tool that task runs on in
        this flow, backing ``{{<task>.tool}}``
    :raises TemplateError: missing input or unknown identifier
    :raises SkipSignal: reference to a skipped/failed task output
    """
    unavailable = unavailable_tasks or set()
    history = loop_history or []
    tools = task_tools or {}

    def _sub(match: re.Match[str]) -> str:
        """Replace a single ``{{...}}`` occurrence with its resolved value.

        :param match: regex match for one template expression.
        :returns: the resolved replacement string.
        """
        expr = match.group(1).strip()
        if expr.startswith("inputs."):
            key = expr[len("inputs."):]
            if key not in inputs:
                raise TemplateError(f"missing input: {key!r}")
            return str(inputs[key])
        if expr == "loop.previous":
            return history[-1] if history else ""
        if expr == "loop.history":
            return _format_history(
                history, loop_history_limit, loop_history_entry_chars
            )
        if expr == "conduit_dir" and conduit_dir is not None:
            return str(conduit_dir)
        if expr.endswith(".output"):
            task = expr[: -len(".output")]
            if task in unavailable:
                raise SkipSignal(
                    f"references output of unavailable task {task!r}"
                )
            if task not in task_outputs:
                raise SkipSignal(
                    f"references output of task {task!r} which has not completed"
                )
            return task_outputs[task]
        if expr.endswith(".tool"):
            task = expr[: -len(".tool")]
            if task not in tools:
                raise TemplateError(f"unknown task in {expr!r}: {task!r}")
            return tools[task]
        raise TemplateError(f"unknown template expression: {expr!r}")

    return _TEMPLATE_RE.sub(_sub, template)
