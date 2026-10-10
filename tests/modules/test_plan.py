"""Unit tests for the pure static execution planner (no I/O, no executor)."""
from __future__ import annotations

from typing import Any

from flow_atelier.modules.engine import validate_conduit
from flow_atelier.modules.plan import build_plan, exhaustion_warnings
from flow_atelier.schemas.conduit import Conduit


def _conduit(tasks: list[dict[str, Any]], **kw: Any) -> Conduit:
    """Build a Conduit model from a list of task dicts plus optional fields.

    :param tasks: task dicts each containing a ``name`` and task fields.
    :param kw: extra top-level conduit fields (inputs, max_concurrency, ...).
    """
    body = {
        "name": "test",
        "description": "d",
        "tasks": [{t["name"]: {k: v for k, v in t.items() if k != "name"}} for t in tasks],
        **kw,
    }
    return Conduit.model_validate(body)


def _plan(conduit: Conduit):
    return build_plan(conduit, validate_conduit(conduit))


def _by_name(plan) -> dict[str, Any]:
    return {t.name: t for wave in plan.waves for t in wave}


def test_diamond_wave_levels():
    """a -> {b,c} -> d yields levels 0,1,1,2 with d the deepest sink."""
    conduit = _conduit(
        [
            {"name": "a", "description": "d", "task": "x", "tool": "tool:bash", "depends_on": []},
            {"name": "b", "description": "d", "task": "x", "tool": "tool:bash", "depends_on": ["a"]},
            {"name": "c", "description": "d", "task": "x", "tool": "tool:bash", "depends_on": ["a"]},
            {"name": "d", "description": "d", "task": "x", "tool": "tool:bash", "depends_on": ["b", "c"]},
        ]
    )
    plan = _plan(conduit)
    tasks = _by_name(plan)
    assert tasks["a"].level == 0
    assert tasks["b"].level == 1
    assert tasks["c"].level == 1
    assert tasks["d"].level == 2
    assert len(plan.waves) == 3
    assert [t.name for t in plan.waves[1]] == ["b", "c"]
    assert plan.sinks == ["d"]
    assert tasks["d"].is_sink is True
    assert tasks["a"].is_sink is False


def test_plain_vs_conditional_edges():
    """Conditional deps render as skip-edges with pattern/negate carried through."""
    conduit = _conduit(
        [
            {"name": "a", "description": "d", "task": "x", "tool": "tool:bash", "depends_on": []},
            {
                "name": "b",
                "description": "d",
                "task": "x",
                "tool": "tool:bash",
                "depends_on": ["a", "a.output.not_match(^SKIP)"],
            },
        ]
    )
    tasks = _by_name(_plan(conduit))
    b = tasks["b"]
    assert [e.task for e in b.plain_edges] == ["a"]
    assert len(b.conditional_edges) == 1
    edge = b.conditional_edges[0]
    assert edge.task == "a"
    assert edge.conditional is True
    assert edge.pattern == "^SKIP"
    assert edge.negate is True


def test_loop_predicate_surfaced():
    """A repeat>1 + until task is is_loop with loop_text carrying the predicate."""
    conduit = _conduit(
        [
            {"name": "a", "description": "d", "task": "x", "tool": "tool:bash", "depends_on": []},
            {
                "name": "loop",
                "description": "d",
                "task": "x",
                "tool": "tool:bash",
                "depends_on": ["a"],
                "repeat": 100,
                "until": "output.match(VERDICT:\\s*DONE)",
            },
        ]
    )
    tasks = _by_name(_plan(conduit))
    loop = tasks["loop"]
    assert loop.is_loop is True
    assert "x100" in loop.loop_text
    assert "VERDICT" in loop.loop_text
    assert tasks["a"].is_loop is False
    assert tasks["a"].loop_text is None


def test_exhaustion_warning_requires_predicate_default_and_dependent():
    loop = {
        "name": "loop", "description": "d", "task": "echo no",
        "tool": "tool:bash", "depends_on": [], "repeat": 3,
        "until": "output.match(YES)",
    }
    dependent = {
        "name": "review", "description": "d", "task": "echo review",
        "tool": "tool:bash", "depends_on": ["loop"],
    }
    verdict = {
        "name": "verdict", "description": "d", "task": "echo verdict",
        "tool": "tool:bash", "depends_on": ["review"],
    }
    conduit = _conduit([loop, dependent, verdict])
    warning = exhaustion_warnings(conduit, validate_conduit(conduit))["loop"]
    assert "review, verdict" in warning and "on_exhaust: fail" in warning
    while_loop = {**loop, "while": "output.match(YES)"}
    while_loop.pop("until")
    conduit = _conduit([while_loop, dependent])
    assert "stays true" in exhaustion_warnings(conduit, validate_conduit(conduit))["loop"]
    for tasks in (
        [loop],
        [{**loop, "on_exhaust": "fail"}, dependent],
        [{k: v for k, v in loop.items() if k != "until"}, dependent],
    ):
        conduit = _conduit(tasks)
        assert exhaustion_warnings(conduit, validate_conduit(conduit)) == {}


def test_gate_detection_and_prune_set():
    """A gate's prune set is its direct conditional dependents and their descendants."""
    conduit = _conduit(
        [
            {"name": "gate", "description": "d", "task": "x", "tool": "tool:bash", "depends_on": []},
            {
                "name": "child",
                "description": "d",
                "task": "x",
                "tool": "tool:bash",
                "depends_on": ["gate.output.match(^READY: )"],
            },
            {
                "name": "grandchild",
                "description": "d",
                "task": "x",
                "tool": "tool:bash",
                "depends_on": ["child"],
            },
        ]
    )
    tasks = _by_name(_plan(conduit))
    assert tasks["gate"].is_gate is True
    assert tasks["gate"].prunes == ["child", "grandchild"]
    assert tasks["child"].is_gate is False
    assert tasks["child"].prunes == []


def test_deep_linear_chain_no_recursion_error():
    """A long single-file chain layers without hitting the recursion limit."""
    n = 2000
    tasks = [
        {
            "name": f"t{i}",
            "description": "d",
            "task": "x",
            "tool": "tool:bash",
            "depends_on": [] if i == 0 else [f"t{i - 1}"],
        }
        for i in range(n)
    ]
    plan = _plan(_conduit(tasks))
    by_name = _by_name(plan)
    assert by_name["t0"].level == 0
    assert by_name[f"t{n - 1}"].level == n - 1
    assert len(plan.waves) == n


def test_max_concurrency_carried():
    """max_concurrency is reflected on the plan."""
    conduit = _conduit(
        [
            {"name": "a", "description": "d", "task": "x", "tool": "tool:bash", "depends_on": []},
        ],
        max_concurrency=7,
    )
    plan = _plan(conduit)
    assert plan.max_concurrency == 7
