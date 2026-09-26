"""Bounds and flag handling for the orphan-step view in `atelier logs`.

Orphans are the step records of a task that never returned — stopped, crashed,
or still running. They are the only record of that work, which also makes them
the unbounded case: an agent task can emit tens of thousands of steps, and
`steps.jsonl` has no rotation.
"""
from __future__ import annotations

import asyncio
import os

import pytest
from typer.testing import CliRunner

from flow_atelier.cli.commands.logs import _orphan_steps
from flow_atelier.cli.main import app
from flow_atelier.cli.rendering.render import (
    TIMELINE_MAX_STEPS,
    _render_steps_timeline,
)
from flow_atelier.core.atelier import Atelier
from flow_atelier.schemas.log import IntermediateStep, StepKind, StepRecord

CONDUIT_YAML = """
name: solo
description: one task
tasks:
  - name: alpha
    description: alpha
    task: "echo alpha-output"
    tool: tool:bash
"""


def _steps(count: int) -> list[IntermediateStep]:
    """Build ``count`` distinct thinking steps.

    :param count: how many steps to build.
    :returns: the constructed steps, numbered in order.
    """
    return [
        IntermediateStep(kind=StepKind.thinking, text=f"step-{i}")
        for i in range(count)
    ]


def test_timeline_drops_oldest_beyond_cap():
    """A timeline over the cap keeps the tail and says what it dropped."""
    rendered = _render_steps_timeline(_steps(TIMELINE_MAX_STEPS + 50)).plain
    assert "50 earlier steps not shown" in rendered
    assert "step-0" not in rendered
    assert f"step-{TIMELINE_MAX_STEPS + 49}" in rendered


def test_timeline_under_cap_is_untouched():
    """A short timeline renders every step with no truncation notice."""
    rendered = _render_steps_timeline(_steps(3)).plain
    assert "not shown" not in rendered
    for i in range(3):
        assert f"step-{i}" in rendered


@pytest.fixture
def workdir(tmp_path, monkeypatch):
    """Isolated cwd holding a one-task bash conduit, with an isolated global dir.

    :param tmp_path: pytest temp directory fixture.
    :param monkeypatch: pytest monkeypatch fixture.
    :returns: the working directory root.
    """
    d = tmp_path / ".atelier" / "conduits" / "solo"
    d.mkdir(parents=True)
    (d / "conduit.yaml").write_text(CONDUIT_YAML)
    global_dir = tmp_path / "global"
    (global_dir / "conduits").mkdir(parents=True)
    monkeypatch.chdir(tmp_path)
    for k in list(os.environ):
        if k.startswith("ATELIER_"):
            monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("ATELIER_GLOBAL_ATELIER_DIR", str(global_dir))
    monkeypatch.setenv("ATELIER_NO_UPDATE_CHECK", "1")
    return tmp_path


@pytest.fixture
def flow_with_orphans(workdir):
    """Run a flow, then append orphan step records for a task with no log entry.

    :param workdir: isolated working directory fixture.
    :returns: tuple of the CliRunner and the flow id.
    """
    runner = CliRunner()
    res = runner.invoke(app, ["run", "solo"])
    flow_id = [l for l in res.output.splitlines() if "flow_id" in l][0].split()[-1]

    atelier = Atelier()
    for step in _steps(TIMELINE_MAX_STEPS + 10):
        asyncio.run(
            atelier.store.append_step(
                flow_id, StepRecord(task="ghost", iteration=1, step=step)
            )
        )
    return runner, flow_id


def test_orphan_group_is_capped(flow_with_orphans, workdir):
    """Grouping keeps only the render cap's worth of steps per iteration."""
    _, flow_id = flow_with_orphans
    atelier = Atelier()
    entries = atelier.store.read_logs(flow_id)
    grouped = _orphan_steps(atelier, flow_id, entries=entries, task=None)
    assert list(grouped) == [("ghost", 1)]
    steps = grouped[("ghost", 1)]
    assert len(steps) == TIMELINE_MAX_STEPS
    # The tail is what survives — a post-mortem reads from the end.
    assert steps[-1].text == f"step-{TIMELINE_MAX_STEPS + 9}"


def test_show_stdout_suppresses_orphans(flow_with_orphans):
    """An explicit channel request hides orphans, which have no such channel."""
    runner, flow_id = flow_with_orphans
    result = runner.invoke(app, ["logs", flow_id, "--show", "stdout"])
    assert result.exit_code == 0, result.output
    assert "ghost" not in result.output


def test_default_show_still_surfaces_orphans(flow_with_orphans):
    """The default `--show output` keeps them: for a killed task they are all
    there is, so hiding them by default would defeat recording them."""
    runner, flow_id = flow_with_orphans
    result = runner.invoke(app, ["logs", flow_id])
    assert result.exit_code == 0, result.output
    assert "ghost" in result.output


def test_last_counts_orphan_panels(flow_with_orphans):
    """`--last 1` prints one thing total, not one entry plus every orphan."""
    runner, flow_id = flow_with_orphans
    result = runner.invoke(app, ["logs", flow_id, "--last", "1"])
    assert result.exit_code == 0, result.output
    # The orphan panel is last in render order, so it is the one kept and the
    # completed `alpha` entry is the one dropped.
    assert "ghost" in result.output
    assert "alpha-output" not in result.output
