"""`atelier check` command — validate conduits without running them."""
from __future__ import annotations

import asyncio
import json
import math
import re
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import typer
import yaml
from pydantic import ValidationError
from rich.markup import escape

from flow_atelier.cli._shared import (
    _exit_unknown_conduit,
    console,
    err_console,
    parse_agents_option,
)
from flow_atelier.cli.commands.recipe_diagnostics import (
    RecipeSource,
    semantic_error,
    unforwarded_inputs,
    validation_errors,
)
from flow_atelier.cli.main import app
from flow_atelier.cli.rendering.render import format_conduit_error
from flow_atelier.core.atelier import Atelier
from flow_atelier.modules.binding import child_bindings
from flow_atelier.modules.conditions import (
    ConditionalDependency,
    PlainDependency,
    parse_dependency,
)
from flow_atelier.modules.engine import (
    MAX_NESTED_CONDUIT_DEPTH,
    ConduitValidationError,
    check_unknown_inputs,
    resolve_executor,
    validate_conduit,
)
from flow_atelier.modules.plan import exhaustion_warnings
from flow_atelier.modules.templating import env_var_name, shell_env, shell_env_hints
from flow_atelier.schemas.conduit import (
    Conduit,
    TaskDefinition,
    ToolType,
    parse_inline_conduit_name,
)
from flow_atelier.services.executor.harness import PROBE_TIMEOUT_SECONDS, ProbeResult

# Errors the per-conduit job can legitimately produce; a programming error
# still crashes rather than becoming a polite failed row.
_EXPECTED = (ValidationError, OSError, UnicodeDecodeError, ConduitValidationError, ValueError)

# What a passing probe does *not* prove. Printed with every probe report so
# "ok" is never read as "this run will work".
_STARTUP_ONLY = (
    "startup only: the agent started and opened a session. That is not proof "
    "of prompt-time authentication, quota, model access or output quality."
)

# What a probe may cost. `initialize` and `session/new` are the only calls
# made — no prompt is ever sent — but an npx/uvx launcher fetches its package
# on first use and opening a session is a real request to the provider.
_PROBE_COST = (
    "no prompt is sent; a launcher may still fetch its package and opening a "
    "session can have provider-side effects"
)


class _NestedProblem(Exception):
    """A failure found below the root, already formatted with its call chain."""


def _recipe_source(path: str | None, name: str) -> RecipeSource | None:
    """Read source positions when a real, parseable recipe file is available."""
    if path is None:
        return None
    try:
        return RecipeSource(path, name)
    except (OSError, UnicodeDecodeError, ValueError, yaml.YAMLError):
        return None


def _diagnostic(e: Exception, where: str) -> str:
    """Render one expected failure as the text a conduit author has to act on.

    :param e: the caught failure.
    :param where: file path (or name) to blame for an I/O failure.
    :returns: a non-empty one-line-or-more diagnostic.
    """
    if isinstance(e, ValidationError):
        # The shared renderer, which prefixes the field path (`tasks[1].foo`).
        # Without it a rejected unknown field reads only as "Extra inputs are
        # not permitted" — true, and useless for finding the typo in `path`.
        # A failure always carries text: `or type(e).__name__` is the last
        # resort, because a blank diagnostic reads like a checker bug.
        return format_conduit_error(e) or "ValidationError"
    if isinstance(e, OSError | UnicodeDecodeError):
        return f"cannot read {where} — {e}"
    return str(e) or type(e).__name__


def _chain(hops: list[tuple[str, str]], target: str | None = None) -> str:
    """Render a call chain as ``parent.task -> child.task -> grandchild``.

    :param hops: ``(conduit, calling task)`` pairs from the root downwards.
    :param target: the called conduit name to append, when there is one.
    :returns: the chain as one readable arrow-separated string.
    """
    text = " -> ".join(f"{conduit}.{task}" for conduit, task in hops)
    return f"{text} -> {target}" if target is not None else text


@dataclass(frozen=True)
class _TeamTask:
    """One piece of agent work the selected scope would hand to a harness.

    :param path: where it sits — ``<conduit>.<task>`` for a task of the root,
        and the full call chain (``root.call -> child.task``) below it.
    :param tool: the harness tool that would run it, after ``--agent``.
    :param conditional: its dependencies carry an output predicate, so a run
        may skip it. Included anyway; a check that silently dropped it would
        miss exactly the agent a rare branch needs.
    """

    path: str
    tool: str
    conditional: bool


# A shell step reading a value Atelier sets: $INPUT_X, ${OUTPUT_PLAN_FILE}.
_VALUE_VAR_RE = re.compile(r"\$\{?((?:INPUT|OUTPUT|LOOP)_[A-Z0-9_]+)")
_LOOP_VARS = ("LOOP_ITERATION", "LOOP_PREVIOUS", "LOOP_HISTORY")


def _hitl_answers(tasks: list[TaskDefinition]) -> set[str]:
    """Name the inputs a ``tool:hitl`` task among ``tasks`` sets once answered.

    :param tasks: the tasks of one conduit level.
    :returns: the answer names, which need no ``inputs:`` declaration.
    """
    return {key for task in tasks if task.tool == ToolType.hitl for key in task.inputs}


_APPROVAL_WORDS = re.compile(r"approv|confirm", re.IGNORECASE)


def _approval_gate_warnings(conduit: Conduit) -> list[str]:
    """Warn for each task that runs after an approval gate whatever the answer.

    A ``tool:hitl`` task finishes once it has an answer, whether that answer
    is yes, no or empty, so a plain dependency on it does not wait for an
    approval. Only gates that read like approvals (their name or one of
    their inputs mentions approve or confirm) are judged, so a gate that only
    collects values stays quiet.

    :param conduit: the conduit to scan, inline loop bodies included.
    :returns: one warning per dependent task and gate, in task order.
    """
    warnings: list[str] = []
    for prefix, tasks in [("", conduit.tasks)] + [
        (f"{t.name}.", t.tasks) for t in conduit.tasks if t.tasks is not None
    ]:
        gates = {
            t.name: next(iter(t.inputs), "answer")
            for t in tasks
            if t.tool == ToolType.hitl
            and _APPROVAL_WORDS.search(
                " ".join([t.name, *t.inputs, *map(str, t.inputs.values())])
            )
        }
        for step in tasks:
            for dep in step.depends_on:
                parsed = parse_dependency(dep)
                if isinstance(parsed, PlainDependency) and parsed.task in gates:
                    warnings.append(
                        f"{prefix}{step.name}: runs after the human gate "
                        f"{parsed.task} whatever the answer, even 'no' or empty; "
                        f"to run it only on approval, depend on "
                        f"\"{parsed.task}.output.match({gates[parsed.task]}: 'yes')\""
                    )
    return warnings


def _shell_value_warnings(conduit: Conduit) -> list[str]:
    """Warn for each value a ``tool:bash`` task reads the unsafe or the empty way.

    The shell parses a substituted ``{{...}}`` value as code, so an input or an
    agent's output can run commands there; the environment variable cannot.
    A ``$LOOP_*`` or ``$OUTPUT_<task>`` the step never gets is just empty,
    where the template form of the same value fails validation. Only names
    Atelier sets are judged, so a script's own ``$OUTPUT_DIR`` stays quiet
    unless a task is called ``dir``.

    :param conduit: the conduit to scan, inline loop bodies included.
    :returns: one warning per task and placeholder or variable, in task order.
    """
    outer_tasks = {t.name for t in conduit.tasks}
    scopes: list[tuple[str, list[TaskDefinition], set[str], TaskDefinition | None]] = [
        ("", conduit.tasks, set(conduit.inputs) | _hitl_answers(conduit.tasks), None)
    ] + [
        (f"{task.name}.", task.tasks, set(task.inputs) | _hitl_answers(task.tasks), task)
        for task in conduit.tasks
        if task.tasks is not None
    ]
    warnings: list[str] = []
    for prefix, tasks, declared, call in scopes:
        deps = {t.name: [parse_dependency(d).task for d in t.depends_on] for t in tasks}
        own = {}
        for name in sorted(deps):
            own.setdefault(env_var_name("OUTPUT_", name), name)
        outer = {}
        if call is not None:
            for name in sorted(outer_tasks):
                outer.setdefault(env_var_name("OUTPUT_", name), name)
            for key in sorted(conduit.inputs):
                if key not in declared:
                    outer.setdefault(env_var_name("INPUT_", key), key)
        for step in tasks:
            if step.tool != ToolType.bash:
                continue
            upstream: set[str] = set()
            frontier = [step.name]
            while frontier:
                for dep in deps.get(frontier.pop(), []):
                    if dep not in upstream:
                        upstream.add(dep)
                        frontier.append(dep)
            where = f"{prefix}{step.name}"
            warnings += _placeholder_warnings(where, step, declared, upstream, call)
            warnings += _unset_variable_warnings(where, step, upstream, own, outer, call)
    return warnings


def _placeholder_warnings(
    where: str,
    step: TaskDefinition,
    declared: set[str],
    upstream: set[str],
    call: TaskDefinition | None,
) -> list[str]:
    """Name the variable to read in place of each ``{{...}}`` value in ``step``.

    :param where: the step as warnings name it.
    :param step: a ``tool:bash`` task.
    :param declared: inputs its level declares, forwards or gets from a human.
    :param upstream: the tasks in its depends_on chain.
    :param call: the looping task whose inline body ``step`` is, or ``None``.
    :returns: one warning per placeholder.
    """
    hints = shell_env_hints(step.task)
    referenced = {
        p.removeprefix("{{inputs.").removesuffix("}}")
        for p, _ in hints if p.startswith("{{inputs.")
    }
    # Ask shell_env itself who owns each variable, so a name that loses a
    # collision (my-name and my_name, report and report_file) is never sent
    # to a variable that holds another value.
    owners = shell_env(
        {name: f"inputs.{name}" for name in declared | referenced},
        {name: f"{name}.output" for name in upstream},
    )
    forwarded_loop = {
        key for key, value in (call.inputs if call is not None else {}).items()
        if isinstance(value, str) and "{{loop." in value
    }
    warnings: list[str] = []
    for placeholder, var in hints:
        ref = placeholder[2:-2]
        name = ref.removeprefix("inputs.")
        if not ref.startswith("loop.") and owners.get(var) != ref:
            warnings.append(
                f"{where}: {placeholder} gets no variable, because ${var} "
                "holds another value; rename it to read it safely"
            )
            continue
        raw = ref.startswith("loop.") or (ref.startswith("inputs.") and name in forwarded_loop)
        warning = (
            f'{where}: use "${var}" instead of {placeholder}, so the value'
            + (", which can hold a failed try's raw output," if raw else "")
            + " cannot run as a command; "
        )
        if not ref.startswith("inputs."):
            warning += "it keeps the trailing newline, and "
        warning += (
            "a value past the step's 100,000-byte budget comes only as "
            f'"${var}_FILE"'
        )
        if ref.startswith("inputs.") and name not in declared:
            # run accepts an undeclared input only while a {{inputs.*}}
            # names it, so the variable form needs the declaration.
            warning += f"; declare {name} under inputs: too"
        warnings.append(warning)
    return warnings


def _unset_variable_warnings(
    where: str,
    step: TaskDefinition,
    upstream: set[str],
    own: dict[str, str],
    outer: dict[str, str],
    call: TaskDefinition | None,
) -> list[str]:
    """Warn for each Atelier variable ``step`` reads but never gets.

    :param where: the step as warnings name it.
    :param step: a ``tool:bash`` task.
    :param upstream: the tasks in its depends_on chain.
    :param own: ``OUTPUT_<X>`` -> task, for the tasks of its own level.
    :param outer: in an inline body, ``OUTPUT_<X>`` / ``INPUT_<X>`` -> the
        enclosing conduit's task or unforwarded input; else empty.
    :param call: the looping task whose inline body ``step`` is, or ``None``.
    :returns: one warning per value, however many spellings read it.
    """
    warnings: list[str] = []
    seen: set[str] = set()
    for var in dict.fromkeys(_VALUE_VAR_RE.findall(step.task)):
        base = next(
            (v for v in (var, var.removesuffix("_FILE"))
             if v in _LOOP_VARS or v in own or v in outer),
            None,
        )
        if base is None or base in seen:
            continue
        seen.add(base)
        if base in _LOOP_VARS:
            if step.repeat > 1:
                continue
            hint = "this step does not loop (repeat is 1)"
            value = base.removeprefix("LOOP_").lower()
            # {{loop.previous}} and {{loop.history}} can be forwarded; the
            # iteration has no template form.
            if call is not None and call.repeat > 1 and value != "iteration":
                hint += (
                    f"; a loop body gets only what {call.name} forwards, so add "
                    f"{value}: '{{{{loop.{value}}}}}' under its inputs: and read "
                    f"${env_var_name('INPUT_', value)}"
                )
            warnings.append(f"{where}: ${var} is empty here: {hint}")
        elif base in own:
            if own[base] in upstream or own[base] == step.name:
                continue
            warnings.append(
                f"{where}: ${var} is empty here: {own[base]} is not in its "
                "depends_on chain; add it as a dependency"
            )
        elif base.startswith("OUTPUT_"):
            name = outer[base]
            warnings.append(
                f"{where}: ${var} is empty here: {name} is a task of the "
                f"enclosing conduit; add {name}: '{{{{{name}.output}}}}' under "
                f"{call.name}'s inputs: and read ${env_var_name('INPUT_', name)}"
            )
        else:
            key = outer[base]
            warnings.append(
                f"{where}: ${var} comes from the environment, not from input "
                f"{key}, because this inline body gets only what its call "
                f"forwards; to use the input, add {key}: '{{{{inputs.{key}}}}}' "
                f"under {call.name}'s inputs:"
            )
    return warnings


def _is_conditional(task: TaskDefinition) -> bool:
    """Report whether a run could skip ``task`` on a dependency's verdict.

    :param task: an already-validated task.
    :returns: true when any dependency is an output predicate.
    """
    return any(
        isinstance(parse_dependency(dep), ConditionalDependency)
        for dep in task.depends_on
    )


def _team_of(conduit: Conduit, hops: list[tuple[str, str]]) -> list[_TeamTask]:
    """List the harness work ``conduit`` itself defines, in task order.

    Its own tasks only: a ``tool:conduit`` call contributes the child's tasks,
    which are collected when that child is walked.

    :param conduit: an already-validated conduit.
    :param hops: ``(conduit, calling task)`` pairs above it; empty at the root.
    :returns: one entry per harness task, plus the supervisor when the
        conduit's interaction policy actually consults one.
    """
    def path(name: str) -> str:
        """Render one task's place in the call tree.

        :param name: the task's own name.
        :returns: the qualified path.
        """
        inline = parse_inline_conduit_name(conduit.name)
        if inline is not None:
            return f"{inline[0]}.{inline[1]}.{name}"
        here = f"{conduit.name}.{name}"
        return _chain(hops, here) if hops else here

    out = [
        _TeamTask(path(task.name), task.tool, _is_conditional(task))
        for task in conduit.tasks
        if task.tool.startswith("harness:")
    ]
    # The same supervisor condition `Atelier.tool_readiness` gates on, so the
    # probe never checks fewer agents than the static check already did.
    policy = conduit.interaction
    if (
        policy
        and policy.supervisor is not None
        and {policy.questions, policy.permissions} & {"supervisor", "hybrid"}
        and policy.supervisor.tool.startswith("harness:")
    ):
        out.append(_TeamTask(path("supervisor"), policy.supervisor.tool, False))
    return out


def _load_child(
    atelier: Atelier, name: str, chain: str, cache: dict[str, tuple[Conduit, str]]
) -> tuple[Conduit, str]:
    """Resolve, load and fully validate one called conduit.

    :param atelier: configured :class:`Atelier` providing the store.
    :param name: the called conduit's name.
    :param chain: the call chain that reached it, for the diagnostic.
    :param cache: invocation-local name -> ``(conduit, file path)`` cache.
    :returns: the loaded child conduit and the file it came from.
    :raises _NestedProblem: it is missing, unreadable, invalid or unrunnable.
    """
    if name in cache:
        return cache[name]
    path: str | None = None
    try:
        path = str((atelier.store.conduit_dir(name) / "conduit.yaml").absolute())
        child = atelier.store.read_conduit(name)
        validate_conduit(child)
    except FileNotFoundError as e:
        # Name the caller and the missing name; there is no source file to
        # point at, and guessing where it "should" live would be a fiction.
        raise _NestedProblem(f"{chain} — conduit not found") from e
    except _EXPECTED as e:
        where = f" ({path})" if path else ""
        raise _NestedProblem(f"{chain}{where} — {_diagnostic(e, path or name)}") from e
    cache[name] = (child, path)
    return child, path


def _check_bindings(
    child: Conduit, path: str, task: TaskDefinition, chain: str,
    source: RecipeSource | None,
) -> None:
    """Check what a calling task forwards against the child's own inputs.

    The executor forwards this task's ``inputs`` map and nothing else — a
    called conduit does not inherit the caller's inputs — so the two failure
    modes are entirely decidable from the definitions: a key the child can
    never use (dropped, so a typo of a defaulted key quietly runs with the
    default), and a declared key with no default that nobody supplies (the
    engine refuses, but only once the earlier steps have already run).

    Names only. Whether a supplied template resolves, and whether a value
    the child merely references (from its own caller, an upstream output, a
    loop or a human answer) will exist, are run-time questions.

    :param child: the loaded child conduit being called.
    :param path: the child's conduit file, for the diagnostic.
    :param task: the calling ``tool:conduit`` task.
    :param chain: the call chain that reached ``child``.
    :raises _NestedProblem: a forwarded key is unusable or a required one
        is missing.
    """
    try:
        check_unknown_inputs(child, task.inputs, subject=f"task {task.name!r}")
    except ValueError as e:
        raise _NestedProblem(f"{chain} ({path}) — {e}") from e
    missing_references = unforwarded_inputs(child, task, source) if source else []
    missing = sorted(
        key
        for key, spec in child.inputs.items()
        if spec.default is None and key not in task.inputs
    )
    if missing:
        raise _NestedProblem(
            f"{chain} ({path}) — task {task.name!r} supplies no value for "
            f"required inputs: {missing}; add them under the task's own "
            f"'inputs:' map, which is all a called conduit receives"
            + ("\n" + "\n".join(missing_references) if missing_references else "")
        )
    if missing_references:
        raise _NestedProblem("\n".join(missing_references))


def _walk_calls(
    atelier: Atelier,
    conduit: Conduit,
    level: int,
    ancestors: frozenset[str],
    hops: list[tuple[str, str]],
    cache: dict[str, tuple[Conduit, str]],
    seen: set[tuple[str, int]],
    team: list[_TeamTask] | None = None,
    agents: Mapping[str, str] | None = None,
    source: RecipeSource | None = None,
) -> None:
    """Follow ``conduit``'s ``tool:conduit`` calls depth-first, in order.

    Conservative by design: a call is inspected even when a runtime condition
    might skip it, and a templated target is refused rather than guessed.

    ``seen`` is keyed by ``(name, level)``, not by name alone, so a child
    shared by a short and a long path is still walked on the long one. With
    nested choices, each call is walked separately to retain path-specific
    agents. A repeated call is not a cycle; only the live ancestry is.

    :param atelier: configured :class:`Atelier` providing the store.
    :param conduit: an already-validated conduit whose calls to follow.
    :param level: ``conduit``'s nesting level; 0 for the selected root.
    :param ancestors: conduit names on the call stack, including ``conduit``.
    :param hops: ``(conduit, calling task)`` pairs above ``conduit``.
    :param cache: invocation-local name -> ``(conduit, file path)`` cache.
    :param seen: ``(name, level)`` pairs already walked for this root.
    :param team: when given, every walked child's harness work is appended to
        it, under the call chain that reached it. A child walked once for a
        given depth contributes once, however many callers share it.
    :raises _NestedProblem: the first failure reachable from ``conduit``.
    """
    for task in conduit.tasks:
        if task.tool != ToolType.conduit:
            continue
        # The executor strips the resolved target, so the same name that
        # would run is the one checked here.
        target = task.task.strip()
        here = [*hops, (conduit.name, task.name)]
        if "{{" in target:
            raise _NestedProblem(
                f"{_chain(here)} — cannot recursively check the dynamic conduit "
                f"target {target!r}; check its resolved target separately"
            )
        chain = _chain(here, target)
        if target in ancestors:
            raise _NestedProblem(f"{chain} — nested conduit cycle detected")
        if level + 1 >= MAX_NESTED_CONDUIT_DEPTH:
            raise _NestedProblem(
                f"{chain} — nested conduit depth exceeded {MAX_NESTED_CONDUIT_DEPTH}"
            )
        child, path = _load_child(atelier, target, chain, cache)
        selected = child_bindings(agents or {}, task.name)
        child = atelier.bind_agents(child, selected)
        problems = atelier.tool_readiness(child)
        if problems:
            raise _NestedProblem(f"{chain} ({path}) — {'; '.join(problems)}")
        child_source = source if parse_inline_conduit_name(target) else _recipe_source(path, target)
        _check_bindings(child, path, task, chain, source)
        # Preserve the existing one-probe-per-child presentation when no
        # nested choice exists. With path choices, each call has its own agent
        # assignment and must contribute its own probe row.
        if (target, level + 1) in seen and not any(
            "." in key for key in (agents or {})
        ):
            continue
        seen.add((target, level + 1))
        if team is not None:
            team.extend(_team_of(child, here))
        _walk_calls(
            atelier, child, level + 1, ancestors | {target}, here, cache, seen, team,
            selected, child_source,
        )


def _check_one(
    atelier: Atelier,
    name: str,
    source: str,
    recursive: bool = False,
    cache: dict[str, tuple[Conduit, str]] | None = None,
    agents: Mapping[str, str] | None = None,
    team: list[_TeamTask] | None = None,
) -> dict[str, Any]:
    """Validate one conduit fully without executing it.

    Combines structural validation with a readiness probe: even a
    structurally-valid conduit FAILs here if a referenced tool is unregistered
    or its harness CLI is missing from PATH, so authors learn before a run.

    The whole per-conduit job lives inside one error boundary — resolving the
    path, reading the file, loading the model, validating the DAG, probing
    the tools and, with ``recursive``, doing all of that for every conduit
    this one calls — so one unreadable conduit becomes a failed row rather
    than aborting the conduits queued after it. Only the failures this work
    can legitimately produce are caught; a programming error still crashes.

    :param atelier: configured :class:`Atelier` providing the store.
    :param name: conduit name to load and validate.
    :param source: ``project`` or ``global``, as reported by the store.
    :param recursive: also follow this conduit's ``tool:conduit`` calls.
    :param cache: invocation-local child cache shared across a batch.
    :param agents: ``--agent`` selections for this root, applied in memory
        before readiness is judged — so replacing an agent you cannot run is
        enough to pass, and an unusable *replacement* is what fails. Top-level
        only: a child's same-named task keeps its own recipe's agent.
    :param team: when given, the harness work of the selected scope is
        appended to it, ready to probe.
    :returns: one result row, the same shape ``--json`` emits.
    """
    path: str | None = None
    error: str | None = None
    required: list[str] | None = None
    warnings: list[str] = []
    recipe_source: RecipeSource | None = None
    try:
        path = str((atelier.store.conduit_dir(name) / "conduit.yaml").absolute())
        conduit = atelier.store.read_conduit(name)
        parsed = validate_conduit(conduit)
        warnings = list(exhaustion_warnings(conduit, parsed).values())
        warnings += _shell_value_warnings(conduit)
        warnings += _approval_gate_warnings(conduit)
        # A selection that no longer fits the recipe, or names an agent this
        # machine has nothing registered for, is a failed row like any other.
        effective = atelier.bind_agents(conduit, agents) if agents else conduit
        problems = atelier.tool_readiness(effective)
        if problems:
            recipe_source = _recipe_source(path, name)
            error = "\n".join(
                semantic_error(problem, recipe_source, list(atelier.executors))
                if recipe_source else problem for problem in problems
            )
        else:
            if team is not None:
                team.extend(_team_of(effective, []))
            if recursive:
                recipe_source = _recipe_source(path, name)
                _walk_calls(
                    atelier, effective, 0, frozenset({name}), [],
                    {} if cache is None else cache, set(), team, agents, recipe_source,
                )
            # Child inputs are the parent's business at runtime, not extra
            # `--input` keys: the root's own declarations are what a caller
            # has to supply.
            required = sorted(
                key for key, spec in conduit.inputs.items() if spec.default is None
            )
    except _NestedProblem as e:
        if "~inline~" in str(e) and "references unknown task" in str(e):
            recipe_source = recipe_source or _recipe_source(path, name)
            error = (semantic_error(str(e), recipe_source) if recipe_source else str(e))
        else:
            error = str(e)
    except ValidationError as e:
        recipe_source = recipe_source or _recipe_source(path, name)
        error = ("\n".join(validation_errors(e, recipe_source))
                 if recipe_source else _diagnostic(e, path or name))
    except _EXPECTED as e:
        recipe_source = recipe_source or _recipe_source(path, name)
        error = (semantic_error(_diagnostic(e, path or name), recipe_source)
                 if recipe_source else _diagnostic(e, path or name))
    row = {
        "name": name,
        "source": source,
        "path": path,
        "ok": error is None,
        "error": error,
        "required_inputs": required,
    }
    if warnings:
        row["warnings"] = warnings
    return row


def _probe_plan(team: list[_TeamTask]) -> dict[str, list[_TeamTask]]:
    """Group the team's work by the exact configuration that would run it.

    The key is the whole tool string, so ``codex:gpt-5.1:high`` and
    ``codex:gpt-5.1:low`` are two checks and not one: a model or an effort the
    agent refuses is a per-selection failure, and merging them by base harness
    would report a refusal that never happened, or miss one that would.

    :param team: the harness work of the selected scope, in encounter order.
    :returns: tool string -> the tasks it would run, first appearance first.
    """
    plan: dict[str, list[_TeamTask]] = {}
    for item in team:
        plan.setdefault(item.tool, []).append(item)
    return plan


async def _probe_each(
    atelier: Atelier,
    plan: dict[str, list[_TeamTask]],
    cwd: str,
    timeout: float,
    on_done: Callable[[str, ProbeResult, float], None],
) -> dict[str, tuple[ProbeResult, float]]:
    """Start each configuration once, in order, and report how far it got.

    One agent at a time. A check is bounded by ``timeout`` per agent and the
    executor's own lifecycle reaps the process on every exit — success,
    refusal, timeout or a Ctrl-C that cancels this coroutine. Failures are
    collected rather than raised: a team with two logged-out agents should
    need one check, not two.

    :param atelier: configured :class:`Atelier` holding the executor table.
    :param plan: the grouping from :func:`_probe_plan`.
    :param cwd: working directory to open each session in.
    :param timeout: seconds allowed per agent.
    :param on_done: called with ``(tool, result, seconds)`` as each finishes.
    :returns: tool -> ``(result, seconds it took)``.
    """
    out: dict[str, tuple[ProbeResult, float]] = {}
    for tool in plan:
        executor = resolve_executor(atelier.executors, tool)
        started = time.monotonic()
        result = await executor.probe(cwd=cwd, timeout=timeout)  # type: ignore[union-attr]
        elapsed = time.monotonic() - started
        out[tool] = (result, elapsed)
        on_done(tool, result, elapsed)
    return out


def _probe_hint(tool: str, result: ProbeResult) -> list[str]:
    """Return the instructions for one failed configuration.

    :param tool: the harness tool that was checked.
    :param result: what the check observed.
    :returns: the guidance lines, plus the one only the tool string explains.
    """
    lines = result.guidance()
    if result.stage == "selection":
        base = tool.split(":")[1] if tool.count(":") >= 1 else tool
        lines = [
            *lines,
            f"this agent refused the model or effort in {tool!r} — run "
            f"'atelier harness check {base}' to list what it offers",
        ]
    return lines


def _render_team(
    plan: dict[str, list[_TeamTask]], scope: str, calls_conduits: bool
) -> None:
    """Print what the probe is about to check, before it starts.

    :param plan: the grouping from :func:`_probe_plan`.
    :param scope: ``recursive`` or ``root``.
    :param calls_conduits: the root calls at least one nested workflow.
    """
    tasks = sum(len(v) for v in plan.values())
    console.print(
        f"    [dim]team ({escape(scope)} scope): {tasks} agent "
        f"task(s) on {len(plan)} configuration(s)[/dim]"
    )
    if scope == "root" and calls_conduits:
        console.print(
            "    [yellow]root only[/yellow] — this workflow calls other "
            "workflows and their agents were not checked; add --recursive"
        )
    console.print(f"    [dim]{escape(_PROBE_COST)}[/dim]")


def _render_agent(tool: str, result: ProbeResult, seconds: float, tasks: list[_TeamTask]) -> None:
    """Print one configuration's outcome and everything it is answerable for.

    :param tool: the harness tool that was checked.
    :param result: what the check observed.
    :param seconds: how long this one took.
    :param tasks: every task the result applies to.
    """
    verdict = (
        f"[green]ok[/green] — {escape(result.detail)}"
        if result.ok
        else f"[red]not usable[/red] — {escape(result.detail)}"
    )
    console.print(f"    {escape(tool)} [dim]({seconds:.1f}s)[/dim] — {verdict}")
    for task in tasks:
        suffix = " [dim](conditional; a run may skip it)[/dim]" if task.conditional else ""
        console.print(f"      [dim]·[/dim] {escape(task.path)}{suffix}")
    for line in _probe_hint(tool, result):
        console.print(f"      [yellow]{escape(line)}[/yellow]")
    # A healthy agent's own startup logging is noise here; it only explains
    # a failure, as `atelier harness check` shows it.
    if not result.ok:
        for line in result.stderr.splitlines()[-5:]:
            console.print(f"      [dim]{escape(line)}[/dim]")


def _probe_row(
    plan: dict[str, list[_TeamTask]],
    results: dict[str, tuple[ProbeResult, float]],
    *,
    scope: str,
    cwd: str,
    timeout: float,
    elapsed: float,
) -> dict[str, Any]:
    """Build the machine-readable half of a probe report.

    :param plan: the grouping from :func:`_probe_plan`.
    :param results: what each configuration answered.
    :param scope: ``recursive`` or ``root``.
    :param cwd: the working directory each session was opened in.
    :param timeout: the per-agent budget that was allowed.
    :param elapsed: seconds the whole probe took locally.
    :returns: the ``probe`` object attached to the conduit's result row.
    """
    agents = []
    for tool, tasks in plan.items():
        result, seconds = results[tool]
        agents.append(
            {
                "tool": tool,
                "ok": result.ok,
                "stage": result.stage,
                "detail": result.detail,
                "agent": result.agent,
                "elapsed_seconds": round(seconds, 3),
                "guidance": _probe_hint(tool, result),
                "tasks": [
                    {"path": t.path, "conditional": t.conditional} for t in tasks
                ],
            }
        )
    return {
        "scope": scope,
        "ran": True,
        "working_dir": cwd,
        "timeout_seconds": timeout,
        "elapsed_seconds": round(elapsed, 3),
        "ok": all(a["ok"] for a in agents),
        "cost": _PROBE_COST,
        "limitation": _STARTUP_ONLY,
        "agents": agents,
    }


@app.command("check")
def check_cmd(
    conduit_name: str = typer.Argument(
        None, help="Conduit to check; omit to check all."
    ),
    json_mode: bool = typer.Option(
        False, "--json", help="Emit one JSON result row per checked conduit."
    ),
    recursive: bool = typer.Option(
        False,
        "--recursive",
        help="Also check every conduit reachable through tool:conduit calls.",
    ),
    probe: bool = typer.Option(
        False,
        "--probe",
        help=(
            "Start each agent the workflow would use and open a session, to "
            "catch a missing or logged-out agent before a run. Needs a "
            "conduit name. Sends no prompt."
        ),
    ),
    agents_raw: list[str] = typer.Option(
        [],
        "--agent",
        help=(
            "task=harness or call.task=harness: check a root or nested agent "
            "task (repeatable). The same mapping you would "
            "pass to `atelier run`; nothing is saved."
        ),
    ),
    timeout: float = typer.Option(
        None,
        "--timeout",
        help=(
            f"Seconds to allow each agent to start and open a session "
            f"(default {PROBE_TIMEOUT_SECONDS:.0f}). Requires --probe."
        ),
    ),
) -> None:
    """Validate hand-authored conduits without running any task.

    `--json` writes one array to stdout — `name`, `source`, `path`, `ok`,
    `error` and `required_inputs` per conduit — so a script or a coding agent
    can find the file to repair instead of reading terminal text. Every
    selected conduit is reported even when an earlier one is broken, and a
    broken conduit is still a result, not a crash: exit 1 with a parseable
    report. Check the exit status *and* parse stdout. A failure before any
    conduit can be selected (an unknown name, an unreadable store) leaves
    stdout empty and explains itself on stderr.

    `--recursive` also follows `tool:conduit` steps, so a missing, invalid or
    unrunnable child — or a call that misspells one of its inputs or leaves a
    required one unsupplied — fails the parent here instead of mid-run. It
    reports one row per selected conduit still; a nested failure names the
    calling chain and the child's file inside the parent's `error`. It is a
    static check of definitions and binding *names*: a call is inspected even
    if a condition would skip it, a templated target cannot be followed and
    fails the check, and passing proves nothing about input *values* or
    runtime success.

    `--probe` goes one step further and asks the agents themselves. It needs a
    conduit name — without one it would start every agent you have installed —
    and it starts each distinct `harness:<name>[:<model>[:<effort>]]` the
    selected scope would use exactly once, completes the ACP handshake, opens
    a session and stops. No prompt is sent. Every affected task is named under
    the configuration that answers for it, failures are collected across the
    whole team rather than stopping at the first, and each agent is bounded by
    `--timeout`. A launcher may still fetch its own package, and opening a
    session is a real request to the provider. What passing proves is startup:
    not prompt-time authentication, quota, model access or output quality.

    `--agent <task>=<harness>` checks a root task; a dotted selector such as
    `loop.fix=codex` checks only that child call's task. The same mapping goes
    to `atelier run`. It is applied in memory, and with `--probe` the chosen
    agents are started without receiving prompts.

    :param conduit_name: a single conduit to check; when omitted, all
        project and global conduits are checked.
    :param json_mode: when true, emit machine-readable JSON instead of the
        labelled terminal report.
    :param recursive: when true, also validate conduits called with
        ``tool:conduit``, detecting missing children, unusable or missing
        input bindings, call cycles and excessive nesting.
    :param probe: when true, start the selected scope's agents and open a
        session with each, after the static check passes.
    :param agents_raw: list of ``task=harness`` strings from ``--agent``.
    :param timeout: seconds allowed per agent; requires ``--probe``.
    """
    out = err_console if json_mode else console
    agents = parse_agents_option(agents_raw, out)
    if timeout is not None and not probe:
        out.print("[red]--timeout applies to --probe; pass both or neither[/red]")
        raise typer.Exit(code=2)
    if timeout is None:
        timeout = PROBE_TIMEOUT_SECONDS
    elif not math.isfinite(timeout) or timeout <= 0:
        out.print(f"[red]--timeout must be a positive number of seconds, not {timeout}[/red]")
        raise typer.Exit(code=2)
    # An omitted name means "every conduit installed", which is a fine default
    # for reading files and a terrible one for starting processes.
    if conduit_name is None and (probe or agents):
        flag = "--probe" if probe else "--agent"
        out.print(f"[red]{flag} needs a conduit name — it acts on one workflow[/red]")
        raise typer.Exit(code=2)

    try:
        atelier = Atelier()
        entries = atelier.store.list_conduits_with_source()
    except OSError as e:
        # No target set could be established, so there is nothing to report:
        # an empty `[]` here would read as "checked everything, all fine".
        err_console.print(f"[red]FAIL: cannot list conduits — {escape(str(e))}[/red]")
        raise typer.Exit(code=1) from e

    if conduit_name is not None:
        sources = dict(entries)
        if conduit_name not in sources:
            _exit_unknown_conduit(conduit_name, list(sources), out=out)
        targets = [(conduit_name, sources[conduit_name])]
    else:
        targets = entries

    cache: dict[str, tuple[Conduit, str]] = {}
    team: list[_TeamTask] = []
    rows = [
        _check_one(
            atelier, name, source, recursive, cache, agents, team if probe else None
        )
        for name, source in targets
    ]

    def _render_row(row: dict[str, Any]) -> None:
        """Print one conduit's verdict the way the terminal report does.

        :param row: the result row to render.
        """
        label = rf"{escape(row['name'])} \[{escape(row['source'])}]"
        if not row["ok"]:
            console.print(f"{label} — [red]FAIL: {escape(row['error'])}[/red]")
            return
        console.print(f"{label} — [green]OK[/green]")
        for warning in row.get("warnings", []):
            console.print(f"    [yellow]⚠ {escape(warning)}[/yellow]")
        if row["required_inputs"]:
            keys = ", ".join(escape(k) for k in row["required_inputs"])
            console.print(f"    [dim]requires --input: {keys}[/dim]")

    # Nothing is started until the definitions hold: probing a workflow that
    # cannot run would report on agents the run would never reach, and a
    # dynamic `tool:conduit` target has already failed the recursive check
    # rather than been guessed at.
    probing = probe and all(row["ok"] for row in rows)
    if not json_mode:
        for row in rows:
            _render_row(row)
        if not rows:
            console.print("[yellow]no conduits found[/yellow]")

    scope = "recursive" if recursive else "root"
    if probe and not probing:
        # Say it rather than leave the key off: a caller that asked for a
        # probe has to be able to tell "every agent answered" from "no agent
        # was ever started" without inferring it from a missing field.
        rows[0]["probe"] = {
            "scope": scope,
            "ran": False,
            "ok": False,
            "reason": "the static check failed, so no agent was started",
            "agents": [],
        }
        if not json_mode:
            console.print("    [yellow]no agent was started[/yellow] — fix the above first")
    elif probing:
        plan = _probe_plan(team)
        cwd = str(Path.cwd())
        if not json_mode:
            calls = any(
                t.tool == ToolType.conduit
                for t in atelier.store.read_conduit(rows[0]["name"]).tasks
            )
            _render_team(plan, scope, calls)
        started = time.monotonic()
        results = asyncio.run(
            _probe_each(
                atelier,
                plan,
                cwd,
                timeout,
                (lambda tool, result, secs: None)
                if json_mode
                else (
                    lambda tool, result, secs: _render_agent(
                        tool, result, secs, plan[tool]
                    )
                ),
            )
        )
        rows[0]["probe"] = _probe_row(
            plan, results, scope=scope, cwd=cwd, timeout=timeout,
            elapsed=time.monotonic() - started,
        )
        if not json_mode:
            console.print(f"    [dim]{escape(_STARTUP_ONLY)}[/dim]")

    if json_mode:
        typer.echo(json.dumps(rows, indent=2))

    failed = any(not row["ok"] for row in rows) or (
        probing and not rows[0]["probe"]["ok"]
    )
    if failed:
        raise typer.Exit(code=1)
