"""Source-aware explanations for hand-written conduit recipes checked by the CLI."""

from __future__ import annotations

import difflib
import re
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError
from yaml.nodes import MappingNode, Node, SequenceNode

from flow_atelier.modules.templating import extract_template_refs
from flow_atelier.schemas.conduit import Conduit, TaskDefinition


class RecipeSource:
    """The YAML syntax tree and data, retained solely to locate diagnostics."""

    def __init__(self, path: str, name: str):
        self.path = path
        self.name = name
        contents = Path(path).read_text(encoding="utf-8")
        self.node = yaml.compose(contents)
        self.data = yaml.safe_load(contents)

    @staticmethod
    def _field(node: Node, key: str) -> Node | None:
        if not isinstance(node, MappingNode):
            return None
        return next((v for k, v in node.value if k.value == key), None)

    def locate(self, loc: tuple[Any, ...]) -> tuple[int, str, str | None]:
        """Return nearest YAML line, named task path and final field."""
        node = self.node
        parts: list[str] = []
        field: str | None = None
        index = 0
        while node is not None and index < len(loc):
            part = loc[index]
            if isinstance(part, int):
                if not isinstance(node, SequenceNode) or part >= len(node.value):
                    break
                node = node.value[part]
                if index and loc[index - 1] == "tasks":
                    raw = self._task_data(parts, part)
                    if isinstance(raw, dict):
                        task_name = raw.get("name")
                        if not task_name and len(raw) == 1:
                            task_name = next(iter(raw))
                        if task_name:
                            parts.append(str(task_name))
                    # A single-key task mapping puts its fields one level in.
                    if isinstance(node, MappingNode) and len(node.value) == 1:
                        key, value = node.value[0]
                        if key.value != "name" and isinstance(value, MappingNode):
                            node = value
            else:
                field = str(part)
                child = self._field(node, field)
                if child is None:
                    break
                node = child
            index += 1
        line = node.start_mark.line + 1 if node is not None else 1
        return line, ".".join([self.name, *parts]), field

    def _task_data(self, parents: list[str], index: int) -> Any:
        data = self.data
        for parent in parents:
            tasks = data.get("tasks", []) if isinstance(data, dict) else []
            data = next((t for t in tasks if _task_name(t) == parent), {})
            if isinstance(data, dict) and parent in data:
                data = data[parent]
        tasks = data.get("tasks", []) if isinstance(data, dict) else []
        return tasks[index] if isinstance(tasks, list) and index < len(tasks) else None

    def task_loc(self, task: str, field: str | None = None) -> tuple[int, str]:
        """Find a task in root or inline body and its named YAML field."""

        def walk(node: Node, data: Any, parents: list[str]) -> tuple[int, str] | None:
            if not isinstance(data, dict) or not isinstance(node, MappingNode):
                return None
            tasks_node = self._field(node, "tasks")
            tasks = data.get("tasks", [])
            if not isinstance(tasks_node, SequenceNode) or not isinstance(tasks, list):
                return None
            for item_node, item in zip(tasks_node.value, tasks, strict=False):
                name = _task_name(item)
                body = item.get(name, item) if isinstance(item, dict) else {}
                body_node = item_node
                if isinstance(item_node, MappingNode) and name in item:
                    body_node = self._field(item_node, name) or item_node
                path = ".".join([self.name, *parents, name])
                if name == task:
                    at = self._field(body_node, field) if field else None
                    return ((at or body_node).start_mark.line + 1, path)
                found = walk(body_node, body, [*parents, name])
                if found:
                    return found
            return None

        return walk(self.node, self.data, []) or (1, f"{self.name}.{task}")

    def prefix(self, line: int, task_path: str) -> str:
        return f"conduit.yaml:{line} {task_path}: "


def _task_name(item: Any) -> str:
    if not isinstance(item, dict):
        return "<task>"
    if "name" in item:
        return str(item["name"])
    return str(next(iter(item))) if item else "<task>"


def _suggest(word: str, options: list[str]) -> str:
    match = difflib.get_close_matches(word, options, n=1, cutoff=0.6)
    return f" Did you mean {match[0]}?" if match else ""


def validation_errors(exc: ValidationError, source: RecipeSource) -> list[str]:
    """Render every Pydantic problem with a file line, task and concrete repair."""
    messages = []
    for error in exc.errors():
        loc = tuple(error.get("loc", ()))
        line, task_path, field = source.locate(loc)
        kind = error.get("type", "")
        raw = str(error.get("msg", ""))
        raw = raw.removeprefix("Value error, ")
        if kind == "missing" and field == "description":
            message = "missing description; add description: <what this step does> here"
        elif kind == "extra_forbidden" and field:
            choices = (
                list(TaskDefinition.model_fields) if "tasks" in loc else list(Conduit.model_fields)
            )
            message = (
                f"unknown key {field!r}; remove it or correct its spelling."
                f"{_suggest(field, choices)}"
            )
        elif field in ("until", "while") or "predicate must start" in raw:
            value = error.get("input")
            if isinstance(value, dict):
                field = "until" if value.get("until") is not None else "while"
                line, task_path = source.task_loc(task_path.rsplit(".", 1)[-1], field)
                value = value.get("until") or value.get("while")
            predicate = str(value) if isinstance(value, str) else "TESTS PASSED"
            if predicate.startswith("output."):
                predicate = "TESTS PASSED"
            message = f"invalid loop condition; use {field or 'until'}: output.match({predicate})"
        elif "on_exhaust requires" in raw:
            line, task_path = source.task_loc(task_path.rsplit(".", 1)[-1], "on_exhaust")
            message = (
                "on_exhaust needs a loop condition; add until: "
                "output.match(TESTS PASSED) or while: output.match(CONTINUE)"
            )
        elif field == "on_exhaust":
            message = "on_exhaust must be complete or fail; use on_exhaust: fail to stop the run"
        elif field == "tool" and "invalid tool" in raw:
            supplied = error.get("input")
            message = (
                f"invalid tool {supplied!r}; use tool:bash, tool:conduit "
                "or harness:<name>. Run atelier harness list for installed agents"
            )
        else:
            message = f"{field + ': ' if field else ''}{raw}"
        messages.append(source.prefix(line, task_path) + message)
    return messages


def semantic_error(message: str, source: RecipeSource, harnesses: list[str] | None = None) -> str:
    """Locate a graph, template or readiness error produced after schema load."""
    match = re.search(r"task ['\"]?([A-Za-z0-9_]+)['\"]?", message)
    task = match.group(1) if match else None
    field = None
    if "depends on unknown task" in message or "depends_on" in message:
        field = "depends_on"
    elif "references unknown task" in message or "template" in message:
        field = "task"
    elif "executor registered" in message or "invalid tool" in message:
        field = "tool"
    line, task_path = source.task_loc(task, field) if task else (1, source.name)
    if task:
        message = re.sub(rf"^task ['\"]{re.escape(task)}['\"]:?\s*", "", message)
    if "depends on unknown task" in message:
        unknown = re.search(r"unknown task ['\"]([^'\"]+)", message)
        if unknown:
            names = [_task_name(t) for t in source.data.get("tasks", [])]
            message += _suggest(unknown.group(1), names)
    if "no executor registered for tool 'harness:" in message:
        wrong = re.search(r"harness:([^']+)", message)
        if wrong:
            options = [
                s.removeprefix("harness:") for s in (harnesses or []) if s.startswith("harness:")
            ]
            match_name = difflib.get_close_matches(wrong.group(1), options, n=1, cutoff=0.5)
            if match_name:
                message += f"; did you mean harness:{match_name[0]}?"
        message += "; run atelier harness list to see installed agents"
    if "references unknown task" in message and task:
        unknown = re.search(r"unknown task ['\"]([^'\"]+)", message)
        if unknown and isinstance(source.data, dict):
            outer = {_task_name(t) for t in source.data.get("tasks", [])}
            if unknown.group(1) in outer and task_path.count(".") > 1:
                key = unknown.group(1)
                loop = task_path.split(".")[1]
                message += (
                    f"; forward it through {source.name}.{loop} inputs: "
                    f"{key}: '{{{{{key}.output}}}}' and read {{{{inputs.{key}}}}} in the body"
                )
    return source.prefix(line, task_path) + message


def unforwarded_inputs(child: Conduit, task: TaskDefinition, source: RecipeSource) -> list[str]:
    """Catch body inputs that would be absent when the child starts."""
    supplied = set(task.inputs)
    defaulted = {key for key, spec in child.inputs.items() if spec.default is not None}
    missing: set[str] = set()
    for body_task in child.tasks:
        templates = [body_task.task, *(v for v in body_task.inputs.values() if isinstance(v, str))]
        for template in templates:
            missing.update(
                ref.value
                for ref in extract_template_refs(template)
                if ref.kind == "input" and ref.value not in supplied | defaulted
            )
    line, path = source.task_loc(task.name, "inputs")
    return [
        source.prefix(line, path)
        + f"body reads {{{{inputs.{key}}}}} but this call does not forward {key}; "
        f"add {key}: '{{{{inputs.{key}}}}}' under this task's inputs:"
        for key in sorted(missing)
    ]
