"""Per-task agent selection — one recipe, a different agent per run.

`--agent TASK=HARNESS` selects a root harness task; dotted paths select one
inside a statically named child call. The installed ``conduit.yaml`` is never touched: the mapping
is applied to the in-memory :class:`Conduit`, and the effective choices are
recorded on that run's ``progress.json`` so a later ``--resume`` or ``--again``
can reuse them without the user retyping anything.

Everything here is pure: no store, no executors, no I/O. Whether a named agent
is actually *registered* is answered by the layer holding the executor table
(:meth:`flow_atelier.core.atelier.Atelier.bind_agents`), and whether its CLI is
installed by the readiness gate that already exists.
"""
from __future__ import annotations

import difflib
import re
from collections.abc import Callable, Iterable, Mapping

from flow_atelier.schemas.conduit import Conduit
from flow_atelier.schemas.harness import HARNESS_TOOL_PATTERN
from flow_atelier.schemas.progress import Progress, TaskStatus

# The only prior dispositions a replacement may be asked for: a task that
# never started, or one that failed. Everything else either already produced
# the output downstream tasks were handed, or is still in flight.
REPLACEABLE = (TaskStatus.pending, TaskStatus.failed)


class BindingError(ValueError):
    """An agent selection the user has to fix, carrying its own exit code.

    :param message: the actionable diagnostic.
    :param code: process exit status — 2 for a misuse of the option itself,
        1 for a refusal about the world (no such task, unknown agent, a
        selector the recipe no longer has).
    """

    def __init__(self, message: str, code: int = 2) -> None:
        super().__init__(message)
        self.code = code


def normalize_harness(harness: str, where: str) -> str:
    """Return ``harness`` as a ``harness:<name>[:<model>[:<effort>]]`` tool.

    The same grammar a conduit's ``tool:`` field accepts, so a name that works
    in YAML works here — bare or prefixed, with or without model and effort.

    :param harness: the agent name as typed.
    :param where: what named it, for the diagnostic.
    :returns: the tool string.
    :raises BindingError: the value is not a usable harness name.
    """
    if harness.startswith("tool:"):
        raise BindingError(
            f"{where}: {harness!r} is not an agent — --agent re-points harness "
            "tasks only; edit the conduit.yaml to change a tool: task"
        )
    tool = harness if harness.startswith("harness:") else f"harness:{harness}"
    if not re.fullmatch(HARNESS_TOOL_PATTERN, tool):
        raise BindingError(
            f"{where}: {harness!r} is not a harness name — expected <name>, "
            "<name>:<model> or <name>:<model>:<effort>; run "
            "'atelier list harnesses' to see the names"
        )
    return tool


def parse_agent_bindings(
    raw: Iterable[str] | None, label: str = "--agent"
) -> dict[str, str]:
    """Parse repeated ``TASK=HARNESS`` values into a task-to-tool mapping.

    Only the first ``=`` separates; neither side may be empty and no task may
    appear twice, because either would make the run's effective assignment
    depend on argument order.

    :param raw: the option values as typed, in order.
    :param label: the option name, for the diagnostics.
    :returns: mapping of task path to harness tool.
    :raises BindingError: a value is malformed or repeats a task.
    """
    out: dict[str, str] = {}
    for item in raw or []:
        task, sep, harness = item.partition("=")
        task, harness = task.strip(), harness.strip()
        if not sep or not task or not harness:
            raise BindingError(
                f"{label} {item!r}: expected TASK=HARNESS, for example "
                f"{label} 'step_2=codex'"
            )
        if task in out:
            raise BindingError(
                f"{label} names task {task!r} twice ({out[task]!r} and then "
                f"{harness!r}); give one agent per task"
            )
        out[task] = normalize_harness(harness, f"{label} {item!r}")
    return out


def normalize_bindings(
    bindings: Mapping[str, str], *, where: str = "--agent"
) -> dict[str, str]:
    """Return ``bindings`` with every value checked as a harness tool.

    The one gate all three sources go through — the CLI option, a direct call
    on the facade, and a prior run's saved choices read back from disk — so
    nothing but an agent can ever be substituted for an agent. Values reaching
    here are usually already normalized tools, and normalizing one twice gives
    the same answer as once.

    :param bindings: mapping of task name to agent name or harness tool.
    :param where: what the mapping came from, for the diagnostics.
    :returns: the same mapping with every value a ``harness:`` tool.
    :raises BindingError: a key or a value is not a usable name.
    """
    out: dict[str, str] = {}
    for task, harness in bindings.items():
        if not isinstance(task, str) or not task.strip():
            raise BindingError(
                f"{where}: {task!r} is not a task name", code=1
            )
        if not isinstance(harness, str) or not harness.strip():
            raise BindingError(
                f"{where}: task {task!r} names no agent ({harness!r})", code=1
            )
        out[task] = normalize_harness(harness.strip(), f"{where} {task}")
    return out


def bind_conduit(
    conduit: Conduit, bindings: Mapping[str, str], *, origin: str = "--agent"
) -> Conduit:
    """Return ``conduit`` with each named top-level task re-pointed.

    A copy: the caller's conduit — and therefore the installed recipe it was
    read from — is left exactly as it was. Selection is top-level only, so a
    task of the same name inside a nested ``tool:conduit`` is untouched; that
    child is read from the store on its own.

    :param conduit: the recipe as installed.
    :param bindings: mapping of task name to harness tool.
    :param origin: what asked for the change, for the diagnostics.
    :returns: the conduit to run, or ``conduit`` itself when nothing is bound.
    :raises BindingError: a selector names no top-level task, names one that
        is not a harness task, or the agent it asks for is not a harness name.
    """
    if not bindings:
        return conduit
    bindings = normalize_bindings(bindings, where=origin)
    by_name = {t.name: t for t in conduit.tasks}
    for task, tool in bindings.items():
        target = by_name.get(task)
        if target is None:
            close = difflib.get_close_matches(task, sorted(by_name), n=1)
            hint = f" — did you mean {close[0]!r}?" if close else ""
            raise BindingError(
                f"{origin}: conduit {conduit.name!r} has no task {task!r}{hint}; "
                f"its top-level tasks are {sorted(by_name)}",
                code=1,
            )
        if not target.tool.startswith("harness:"):
            raise BindingError(
                f"{origin}: task {task!r} runs {target.tool!r}, which is not an "
                f"agent — {tool!r} cannot replace it; --agent re-points harness "
                "tasks only",
                code=1,
            )
    tasks = [
        t.model_copy(update={"tool": bindings[t.name]}) if t.name in bindings else t
        for t in conduit.tasks
    ]
    return conduit.model_copy(update={"tasks": tasks})


def child_bindings(bindings: Mapping[str, str], call: str) -> dict[str, str]:
    """Return selections below one calling task, relative to its child."""
    prefix = f"{call}."
    return {key[len(prefix):]: value for key, value in bindings.items()
            if key.startswith(prefix)}


def valid_agent_selectors(
    conduit: Conduit, load_child: Callable[[str], Conduit],
    *, prefix: str = "", ancestors: frozenset[str] = frozenset(),
) -> list[str]:
    """List statically addressable harness tasks below a recipe."""
    if conduit.name in ancestors or len(ancestors) >= 10:
        return []
    ancestors = ancestors | {conduit.name}
    found: list[str] = []
    for task in conduit.tasks:
        path = f"{prefix}{task.name}"
        if task.tool.startswith("harness:"):
            found.append(path)
        elif task.tool == "tool:conduit" and "{{" not in task.task:
            try:
                child = load_child(task.task.strip())
            except (FileNotFoundError, ValueError):
                continue
            found.extend(valid_agent_selectors(
                child, load_child, prefix=f"{path}.", ancestors=ancestors,
            ))
    return found


def bind_agent_paths(
    conduit: Conduit, bindings: Mapping[str, str],
    load_child: Callable[[str], Conduit], *, origin: str = "--agent",
) -> Conduit:
    """Validate all selectors and apply the root choices in memory.

    Child choices are applied by the nested runner to each invocation. Loading
    here establishes that every path is static and valid before a run starts.
    """
    if not bindings:
        return conduit
    choices = valid_agent_selectors(conduit, load_child)
    hint = f"; valid agent selectors: {choices}" if choices else "; no static agent selectors found"
    direct: dict[str, str] = {}
    for path, tool in bindings.items():
        parts = path.split(".")
        if any(not re.fullmatch(r"[A-Za-z0-9_]+", part) for part in parts):
            raise BindingError(f"{origin}: malformed task path {path!r}{hint}", code=1)
        current = conduit
        for call in parts[:-1]:
            task = next((t for t in current.tasks if t.name == call), None)
            if task is None or task.tool != "tool:conduit":
                raise BindingError(
                    f"{origin}: {path!r} has no conduit call at {call!r}{hint}", code=1
                )
            if "{{" in task.task:
                raise BindingError(
                    f"{origin}: {path!r} enters dynamic conduit target "
                    f"{task.task!r}; use a static child name{hint}", code=1,
                )
            try:
                current = load_child(task.task.strip())
            except (FileNotFoundError, ValueError) as exc:
                raise BindingError(
                    f"{origin}: cannot load child {task.task.strip()!r} "
                    f"for {path!r}: {exc}{hint}", code=1,
                ) from exc
        if len(parts) == 1 and not any(t.name == path for t in current.tasks):
            nested = [choice for choice in choices if choice.endswith(f".{path}")]
            if nested:
                raise BindingError(
                    f"{origin}: {path!r} is inside a called conduit; use a "
                    f"dotted selector such as {nested[0]!r}{hint}", code=1,
                )
        try:
            bind_conduit(current, {parts[-1]: tool}, origin=origin)
        except BindingError as exc:
            raise BindingError(f"{exc}{hint}", code=exc.code) from exc
        if len(parts) == 1:
            direct[path] = tool
    return bind_conduit(conduit, direct, origin=origin)


# A composed recipe quotes an upstream result under a marker naming who
# produced it. `atelier compose` writes that name as a `{{<task>.tool}}`
# reference now, but a recipe composed before it did carries the tool as a
# literal, and the same is true of any hand-written label.
_LABELLED_RESULT = re.compile(r"RESULT FROM (\S+) \((harness:[^)\s]+)\)")


def provenance_note(prompt: str, tools: Mapping[str, str]) -> str:
    """Return the correction to append when a prompt mislabels its material.

    A static label that disagrees with the tool a task actually runs on would
    tell the receiving agent the wrong provenance, and rewriting the user's own
    prompt text is not this code's business. So the literals are left alone and
    one authoritative block is appended naming what really ran. A label already
    written as ``{{<task>.tool}}`` resolves to that same tool, so nothing is
    appended for a recipe composed by the current ``atelier compose``.

    :param prompt: the task prompt, with its templates already resolved.
    :param tools: the tool every task of this flow runs on.
    :returns: the block to append, or ``""`` when every label is right.
    """
    wrong = {
        task: (labelled, tools[task])
        for task, labelled in _LABELLED_RESULT.findall(prompt)
        if task in tools and tools[task] != labelled
    }
    if not wrong:
        return ""
    lines = "\n".join(
        f"{task} ran on {actual}, not the {labelled} its marker above names."
        for task, (labelled, actual) in sorted(wrong.items())
    )
    return (
        "\n--- BEGIN AGENT PROVENANCE (authoritative) ---\n"
        f"{lines}\n"
        "--- END AGENT PROVENANCE (authoritative) ---\n"
    )


def check_replaceable(
    progress: Progress, bindings: Mapping[str, str], *, partial: Iterable[str] = ()
) -> None:
    """Refuse a mid-flight change of assignment on a task that already ran.

    A completed task's output is already in downstream prompts, and a running
    one is in flight, so re-pointing either would rewrite history rather than
    recover from a failure. A looping task that recorded successful iterations
    is refused for the same reason even while it is failed or pending: its
    ``{{loop.history}}`` was produced by the agent it started with, and nothing
    here can attribute half of it to a different one.

    :param progress: the prior run's saved progress.
    :param bindings: only the assignments this invocation asks to change.
    :param partial: names of looping tasks with completed iterations on record.
    :raises BindingError: a selector names a task whose assignment is fixed.
    """
    partial_names = set(partial)
    for task in bindings:
        tp = progress.tasks.get(task)
        if tp is None:
            continue
        if tp.status not in REPLACEABLE:
            raise BindingError(
                f"--agent {task}=...: task {task!r} is {tp.status.value} in this "
                "flow, and only a pending or failed task can be given another "
                "agent — start a fresh run with `atelier run --again <flow_id> "
                "--agent ...` to redo it",
                code=1,
            )
        if task in partial_names:
            raise BindingError(
                f"--agent {task}=...: task {task!r} already recorded "
                f"iteration {tp.iteration} of {tp.of} under "
                f"{progress.task_agents.get(task) or 'its recipe agent'}; "
                "switching agents mid-loop would attribute that history to the "
                "wrong one — start a fresh run with `atelier run --again "
                "<flow_id> --agent ...`",
                code=1,
            )
