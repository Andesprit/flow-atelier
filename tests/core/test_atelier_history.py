"""Atelier facade: flow history (list_prior_flows, get_flow_logs)."""
from __future__ import annotations

import pytest

from flow_atelier.core.atelier import Atelier
from flow_atelier.schemas.api import PriorFlow, RunTaskInput
from flow_atelier.schemas.log import LogEntry


@pytest.fixture
def atelier(tmp_path):
    """Construct an Atelier instance rooted under tmp_path.

    :param tmp_path: pytest temp directory fixture.
    """
    return Atelier(base_dir=tmp_path / ".atelier")


async def test_list_prior_flows_after_run_returns_one(atelier, tmp_path):
    """Verify list_prior_flows returns the just-completed run.

    :param atelier: Atelier facade fixture.
    """
    payload = RunTaskInput(
        name="echo",
        description="d",
        task="echo hi",
        tool="tool:bash",
        run_path=str(tmp_path),
    )
    out = await atelier.run_single_task(payload)
    flows = atelier.list_prior_flows()
    assert len(flows) == 1
    assert isinstance(flows[0], PriorFlow)
    assert flows[0].flow_id == out.flow_id
    assert flows[0].status == "completed"


async def test_get_flow_logs_round_trips(atelier, tmp_path):
    """Verify get_flow_logs returns LogEntry objects from a completed run.

    :param atelier: Atelier facade fixture.
    """
    payload = RunTaskInput(
        name="echo",
        description="d",
        task="echo round-trip",
        tool="tool:bash",
        run_path=str(tmp_path),
    )
    out = await atelier.run_single_task(payload)
    logs = atelier.get_flow_logs(out.flow_id)
    assert all(isinstance(e, LogEntry) for e in logs)
    assert any("round-trip" in (e.stdout or "") for e in logs)


def test_get_flow_logs_unknown_raises(atelier):
    """Verify get_flow_logs raises FileNotFoundError for unknown flow ids.

    :param atelier: Atelier facade fixture.
    """
    with pytest.raises(FileNotFoundError):
        atelier.get_flow_logs("not_a_flow")


def test_list_prior_flows_empty(atelier):
    """Verify list_prior_flows returns [] before any run is executed.

    :param atelier: Atelier facade fixture.
    """
    assert atelier.list_prior_flows() == []
