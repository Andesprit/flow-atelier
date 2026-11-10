"""A cancel during an until/while try is a cancel, not a failed try.

Fail-fast, ``atelier stop`` and Ctrl-C all cancel the running task. The loop
must not count that as a try that failed, and must not start the next one.
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest

from flow_atelier.modules.engine import Engine
from flow_atelier.schemas.conduit import Conduit
from flow_atelier.schemas.progress import FlowStatus, TaskStatus
from flow_atelier.services.executor.bash import BashExecutor
from flow_atelier.services.executor.conduit import ConduitExecutor
from flow_atelier.services.executor.harness import AcpHarnessExecutor
from flow_atelier.services.store.filesystem import FilesystemStore

FAKE_AGENT = Path(__file__).resolve().parents[1] / "fixtures" / "fake_acp_agent.py"
BODIES = ["tool:bash", "tool:conduit", "harness:slow"]

pytestmark = pytest.mark.skipif(
    sys.platform == "win32", reason="the shell bodies are POSIX scripts"
)


@pytest.fixture
def store(tmp_path):
    """Provide a FilesystemStore rooted under the pytest temp path.

    :param tmp_path: pytest temp directory fixture.
    """
    return FilesystemStore(tmp_path / ".atelier")


def _loop(body: str, record: Path) -> dict:
    """Build an until loop whose every try takes 10 s and leaves a trace.

    :param body: ``tool:bash``, ``tool:conduit`` or ``harness:slow``.
    :param record: directory each try leaves one file or line in.
    """
    loop = {"description": "d", "repeat": 3, "until": "output.match(DONE)"}
    shell = f"echo start >> '{record / 'tries'}'\nsleep 10\necho not yet"
    if body == "tool:conduit":
        return {"looper": {**loop, "tool": body, "tasks": [
            {"inner": {"description": "d", "tool": "tool:bash", "task": shell}}
        ]}}
    return {"looper": {**loop, "tool": body,
                       "task": shell if body == "tool:bash" else "try"}}


def _tries(record: Path) -> int:
    """Count the tries started: shell lines, or agent processes.

    :param record: the directory :func:`_loop` was given.
    """
    if (record / "tries").exists():
        return len((record / "tries").read_text().splitlines())
    return len(list(record.glob("*.events")))


def _engine(store, record: Path) -> Engine:
    """Build an engine with the real shell, conduit and agent executors.

    :param store: FilesystemStore fixture.
    :param record: directory the fake agent records each process in.
    """
    script = {"record_path": str(record),
              "turns": [{"chunks": ["not yet"], "delay_before": 10}] * 3}
    agent = [sys.executable, str(FAKE_AGENT), "--script", json.dumps(script)]
    return Engine({
        "tool:bash": BashExecutor(),
        "tool:conduit": ConduitExecutor(),
        "harness:slow": AcpHarnessExecutor(launch_cmd=agent),
    }, store)


def _assert_one_cancelled_try(store, flow_id: str, record: Path) -> None:
    """Check the loop ran one try, logged none, and ended cancelled.

    :param store: FilesystemStore fixture.
    :param flow_id: the parent flow.
    :param record: the directory :func:`_loop` was given.
    """
    task = store.read_progress(flow_id).tasks["looper"]
    assert task.status == TaskStatus.cancelled
    assert task.iteration == 1
    assert [e for e in store.read_logs(flow_id) if e.task == "looper"] == []
    assert _tries(record) == 1


@pytest.mark.parametrize("body", BODIES)
async def test_fail_fast_during_a_try_is_not_a_failed_try(store, tmp_path, body):
    """A failing sibling cancels the try; the loop does not go on.

    :param store: FilesystemStore fixture.
    :param tmp_path: pytest temp directory fixture.
    :param body: the loop body's tool.
    """
    record = tmp_path / "record"
    record.mkdir()
    conduit = Conduit.model_validate({"name": "p", "description": "d", "tasks": [
        _loop(body, record),
        # Fails once the loop's first try has started.
        {"breaker": {"description": "d", "tool": "tool:bash",
                     "task": f"until ls '{record}'/* >/dev/null 2>&1; "
                             "do sleep 0.05; done; sleep 0.3; exit 3"}},
    ]})
    store.write_conduit(conduit)
    with pytest.raises(RuntimeError, match="task 'breaker' failed"):
        await asyncio.wait_for(
            _engine(store, record).run(conduit, {}, working_dir=tmp_path), timeout=8
        )

    _assert_one_cancelled_try(store, store.list_flows()[0], record)


@pytest.mark.parametrize("body", BODIES)
async def test_stop_during_a_try_is_not_a_failed_try(store, tmp_path, body):
    """Cancelling the run, as ``atelier stop`` and Ctrl-C do, ends the loop.

    :param store: FilesystemStore fixture.
    :param tmp_path: pytest temp directory fixture.
    :param body: the loop body's tool.
    """
    record = tmp_path / "record"
    record.mkdir()
    conduit = Conduit.model_validate(
        {"name": "p", "description": "d", "tasks": [_loop(body, record)]}
    )
    store.write_conduit(conduit)
    run = asyncio.create_task(
        _engine(store, record).run(conduit, {}, working_dir=tmp_path)
    )
    for _ in range(200):
        if _tries(record):
            break
        await asyncio.sleep(0.05)
    await asyncio.sleep(0.3)
    run.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(run, timeout=8)

    flow_id = store.list_flows()[0]
    assert store.read_progress(flow_id).status == FlowStatus.failed
    _assert_one_cancelled_try(store, flow_id, record)
