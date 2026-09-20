"""Atelier facade: run_single_task (ad-hoc one-task conduit)."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from flow_atelier.core.atelier import Atelier
from flow_atelier.core.settings import AtelierSettings
from flow_atelier.schemas.api import RunTaskInput, RunTaskOutput

FAKE_AGENT = Path(__file__).resolve().parents[1] / "fixtures" / "fake_acp_agent.py"


@pytest.fixture
def atelier(tmp_path, monkeypatch):
    """Construct an Atelier instance rooted under tmp_path.

    :param tmp_path: pytest temp directory fixture.
    :param monkeypatch: pytest monkeypatch fixture.
    """
    monkeypatch.delenv("ATELIER_GLOBAL_ATELIER_DIR", raising=False)
    return Atelier(base_dir=tmp_path / ".atelier")


async def test_run_single_task_runs_bash_echo_and_returns_logs(atelier, tmp_path):
    """Verify run_single_task runs bash echo and returns logs with exit 0.

    :param atelier: Atelier facade fixture.
    """
    payload = RunTaskInput(
        name="echo",
        description="ad-hoc echo",
        task="echo hello-from-task",
        tool="tool:bash",
        run_path=str(tmp_path),
    )
    out = await atelier.run_single_task(payload)
    assert isinstance(out, RunTaskOutput)
    assert out.flow_id
    assert out.logs
    assert any("hello-from-task" in (e.stdout or "") for e in out.logs)
    assert out.logs[-1].exit_code == 0


async def test_run_single_task_persists_flow_dir(atelier, tmp_path):
    """Verify run_single_task writes the flow's logs.jsonl to disk.

    :param atelier: Atelier facade fixture.
    """
    payload = RunTaskInput(
        name="echo",
        description="ad-hoc echo",
        task="echo persisted",
        tool="tool:bash",
        run_path=str(tmp_path),
    )
    out = await atelier.run_single_task(payload)
    flow_dir = atelier.store._flow_dir(out.flow_id)
    assert (flow_dir / "logs.jsonl").exists()


async def test_run_single_task_drives_a_settings_declared_harness(
    tmp_path, monkeypatch, _isolate_global_atelier_dir
):
    """An ACP agent known only to ATELIER_HARNESSES runs end to end.

    Covers the whole chain the bundled harnesses get for free: settings ->
    executor registry -> task schema -> engine dispatch -> ACP session.

    :param tmp_path: pytest temp directory fixture.
    :param monkeypatch: pytest monkeypatch fixture.
    :param _isolate_global_atelier_dir: isolated global atelier dir fixture.
    """
    script = json.dumps({"turns": [{"chunks": ["custom agent ok"], "stop": "end_turn"}]})
    at = Atelier(
        settings=AtelierSettings(
            atelier_dir=tmp_path / ".atelier",
            global_atelier_dir=_isolate_global_atelier_dir,
            harnesses={"fake": [sys.executable, str(FAKE_AGENT), "--script", script]},
        ),
    )
    out = await at.run_single_task(
        RunTaskInput(
            name="ask",
            description="ad-hoc harness task",
            task="hello",
            tool="harness:fake",
            run_path=str(tmp_path),
        )
    )
    assert out.logs[-1].exit_code == 0
    assert "custom agent ok" in (out.logs[-1].stdout or "")


async def test_run_single_task_pins_a_model_through_the_tool_name(
    tmp_path, _isolate_global_atelier_dir
):
    """`harness:<name>:<model>` reaches the ACP session as a model selection.

    :param tmp_path: pytest temp directory fixture.
    :param _isolate_global_atelier_dir: isolated global atelier dir fixture.
    """
    script = json.dumps(
        {
            "models": {
                "current": "m1",
                "available": [{"id": "m1", "name": "One"}, {"id": "m2", "name": "Two"}],
            },
            "turns": [{"chunks": ["on m2"], "stop": "end_turn"}],
        }
    )
    at = Atelier(
        settings=AtelierSettings(
            atelier_dir=tmp_path / ".atelier",
            global_atelier_dir=_isolate_global_atelier_dir,
            harnesses={"fake": [sys.executable, str(FAKE_AGENT), "--script", script]},
        ),
    )
    out = await at.run_single_task(
        RunTaskInput(
            name="ask",
            description="pinned model",
            task="hello",
            tool="harness:fake:m2",
            run_path=str(tmp_path),
        )
    )
    assert out.logs[-1].exit_code == 0, out.logs[-1].stderr
    assert "[config_set:model=m2]" in (out.logs[-1].stdout or "")
    assert out.logs[-1].tool == "harness:fake:m2"


async def test_run_single_task_failure_returns_non_zero_exit(atelier, tmp_path):
    """Verify run_single_task returns a non-zero exit code on failure.

    :param atelier: Atelier facade fixture.
    """
    payload = RunTaskInput(
        name="boom",
        description="explode",
        task="exit 7",
        tool="tool:bash",
        run_path=str(tmp_path),
    )
    out = await atelier.run_single_task(payload)
    assert out.flow_id
    assert out.logs
    assert out.logs[-1].exit_code != 0


async def test_run_single_task_failure_is_reported_as_unsuccessful(atelier, tmp_path):
    """A task that exits non-zero must say so in the response body.

    The logs already carried the exit code; `success` exists for the case
    below, where there are no logs to read it from.

    :param atelier: Atelier facade fixture.
    :param tmp_path: pytest temp directory fixture.
    """
    out = await atelier.run_single_task(
        RunTaskInput(
            name="boom",
            description="explode",
            task="exit 7",
            tool="tool:bash",
            run_path=str(tmp_path),
        )
    )
    assert out.success is False
    assert out.error


async def test_run_single_task_that_never_started_is_not_reported_as_success(
    atelier, tmp_path
):
    """A run that dies before its first task must not look like a quiet success.

    An unavailable tool fails inside the engine, before any log entry exists,
    so the response is an empty `logs` and a blank `flow_id` — byte-identical
    to a task that simply printed nothing.

    :param atelier: Atelier facade fixture.
    :param tmp_path: pytest temp directory fixture.
    """
    out = await atelier.run_single_task(
        RunTaskInput(
            name="ghost",
            description="unregistered harness",
            task="hello",
            tool="harness:definitely-not-registered",
            run_path=str(tmp_path),
        )
    )
    assert out.success is False
    assert out.error
    assert not out.logs


async def test_run_single_task_success_reports_success(atelier, tmp_path):
    """The happy path keeps `success` true and `error` empty.

    :param atelier: Atelier facade fixture.
    :param tmp_path: pytest temp directory fixture.
    """
    out = await atelier.run_single_task(
        RunTaskInput(
            name="ok",
            description="fine",
            task="echo fine",
            tool="tool:bash",
            run_path=str(tmp_path),
        )
    )
    assert out.success is True
    assert out.error == ""
