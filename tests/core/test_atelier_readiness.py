"""Atelier.tool_readiness — preflight runnability probe."""
from __future__ import annotations

from pathlib import Path

import pytest

from flow_atelier.core.atelier import Atelier
from flow_atelier.core.settings import AtelierSettings
from flow_atelier.schemas.conduit import Conduit


@pytest.fixture
def atelier(tmp_path, _isolate_global_atelier_dir):
    """Construct an Atelier rooted under tmp_path with an isolated global dir.

    :param tmp_path: pytest temp directory fixture.
    :param _isolate_global_atelier_dir: isolated global atelier dir fixture.
    """
    global_dir: Path = _isolate_global_atelier_dir
    return Atelier(
        base_dir=tmp_path / ".atelier",
        settings=AtelierSettings(
            atelier_dir=tmp_path / ".atelier",
            global_atelier_dir=global_dir,
        ),
    )


def _conduit(tasks: list[dict]) -> Conduit:
    """Build a minimal Conduit from a list of task dicts.

    :param tasks: task payloads (each with name/description/task/tool).
    """
    return Conduit.model_validate(
        {"name": "c", "description": "d", "tasks": tasks}
    )


def test_all_bash_conduit_is_ready(atelier):
    """A conduit using only always-available tools reports no problems."""
    conduit = _conduit(
        [
            {"name": "a", "description": "a", "task": "echo hi", "tool": "tool:bash"},
            {"name": "b", "description": "b", "task": "echo bye", "tool": "tool:bash"},
        ]
    )
    assert atelier.tool_readiness(conduit) == []


def test_unavailable_harness_is_reported(atelier):
    """A task whose harness probe fails surfaces a message naming task+tool."""
    atelier.executors["harness:claude-code"].is_available = lambda: (False, "no npx")
    conduit = _conduit(
        [
            {"name": "build", "description": "b", "task": "do", "tool": "harness:claude-code"},
        ]
    )
    problems = atelier.tool_readiness(conduit)
    assert problems == ["task 'build' [harness:claude-code]: no npx"]


def test_configured_harness_is_registered(tmp_path, _isolate_global_atelier_dir):
    """A harness named in settings becomes a runnable harness:<name> executor."""
    at = Atelier(
        settings=AtelierSettings(
            atelier_dir=tmp_path / ".atelier",
            global_atelier_dir=_isolate_global_atelier_dir,
            harnesses={"gemini": ["npx", "-y", "gemini-acp"]},
        ),
    )
    assert at.executors["harness:gemini"].launch_cmd == ["npx", "-y", "gemini-acp"]
    conduit = _conduit(
        [{"name": "x", "description": "x", "task": "do", "tool": "harness:gemini"}]
    )
    at.executors["harness:gemini"].is_available = lambda: (True, "")
    assert at.tool_readiness(conduit) == []


def test_configured_harness_overrides_a_bundled_one(
    tmp_path, _isolate_global_atelier_dir
):
    """Reusing a bundled name replaces its argv rather than adding a second key."""
    at = Atelier(
        settings=AtelierSettings(
            atelier_dir=tmp_path / ".atelier",
            global_atelier_dir=_isolate_global_atelier_dir,
            harnesses={"codex": ["my-codex", "--acp"]},
        ),
    )
    assert at.executors["harness:codex"].launch_cmd == ["my-codex", "--acp"]


def test_model_suffix_resolves_to_the_base_harness(atelier):
    """`harness:<name>:<model>` is ready whenever `harness:<name>` is."""
    atelier.executors["harness:claude-code"].is_available = lambda: (True, "")
    conduit = _conduit(
        [{"name": "x", "description": "x", "task": "do", "tool": "harness:claude-code:m1"}]
    )
    assert atelier.tool_readiness(conduit) == []
    conduit = _conduit(
        [{"name": "x", "description": "x", "task": "do", "tool": "harness:nope-9000:m1"}]
    )
    assert atelier.tool_readiness(conduit) == [
        "task 'x': no executor registered for tool 'harness:nope-9000:m1'"
    ]


def test_unconfigured_harness_name_is_reported(atelier):
    """A harness in neither the ACP registry nor settings fails preflight."""
    conduit = _conduit(
        [{"name": "x", "description": "x", "task": "do", "tool": "harness:nope-9000"}]
    )
    assert atelier.tool_readiness(conduit) == [
        "task 'x': no executor registered for tool 'harness:nope-9000'"
    ]


def test_unregistered_tool_is_reported(atelier):
    """Removing an executor key surfaces a 'no executor registered' message."""
    del atelier.executors["harness:codex"]
    conduit = _conduit(
        [
            {"name": "x", "description": "x", "task": "do", "tool": "harness:codex"},
        ]
    )
    problems = atelier.tool_readiness(conduit)
    assert problems == [
        "task 'x': no executor registered for tool 'harness:codex'"
    ]
