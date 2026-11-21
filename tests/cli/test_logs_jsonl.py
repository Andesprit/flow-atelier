"""Replay and follow the JSON Lines stream at the CLI boundary."""
from __future__ import annotations

import asyncio
import json
import os
import signal
import socket
import subprocess
import sys
from contextlib import contextmanager

import pytest
from typer.testing import CliRunner

from flow_atelier.cli.main import app
from flow_atelier.core.atelier import Atelier
from flow_atelier.schemas.log import IntermediateStep, LogEntry, StepRecord
from flow_atelier.schemas.progress import FlowStatus, Progress


@pytest.fixture
def flow(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    store = Atelier().store
    fid = store.create_flow("demo", {})
    store.write_progress(fid, Progress())
    return store, fid


def _step(task, text, kind="thinking"):
    return StepRecord(task=task, iteration=2, step=IntermediateStep(kind=kind, text=text))


def _entry(step):
    return LogEntry(task=step.task, iteration=2, tool="demo", output="result",
                    started_at=step.step.timestamp, finished_at=step.step.timestamp,
                    steps=[step.step])


def _snapshot(fid, *args):
    result = CliRunner().invoke(app, ["logs", fid, "--jsonl", *args])
    assert result.exit_code == 0, result.output
    return [json.loads(line) for line in result.stdout.splitlines()]


def test_snapshot_replays_legacy_and_orphan_steps_with_filters_and_bounds(flow):
    store, fid = flow
    legacy = _step("done", "line one\nline two")
    asyncio.run(store.append_log(fid, _entry(legacy)))
    orphan = _step("unfinished", "last line", "stderr")
    asyncio.run(store.append_step(fid, orphan))
    store.write_progress(fid, Progress(status=FlowStatus.stopped))

    records = _snapshot("latest")
    assert [row["kind"] for row in records] == ["step", "entry", "step", "end"]
    assert records[0]["step"]["text"] == "line one\nline two"
    assert "steps" not in records[1]
    assert records[2] == {"kind": "step", **orphan.model_dump(mode="json")}
    assert records[3] == {"kind": "end", "flow_id": fid, "status": "stopped"}
    assert _snapshot(fid, "--last", "1") == records[-2:]
    assert _snapshot(fid, "--last", "0") == records[-1:]
    assert _snapshot(fid, "--task", "done", "--show", "steps") == [records[0], records[-1]]
    assert _snapshot(fid, "--task", "unfinished", "--show", "stderr") == records[-2:]
    assert _snapshot(fid, "--task", "unfinished", "--show", "stdout") == records[-1:]


def test_empty_snapshot_and_dead_runner(flow):
    store, fid = flow
    assert _snapshot(fid) == [{"kind": "end", "flow_id": fid, "status": "running"}]
    store.write_progress(fid, Progress(runner_host=socket.gethostname(), runner_pid=2**30))
    orphan = _step("unfinished", "before crash")
    asyncio.run(store.append_step(fid, orphan))
    assert _snapshot(fid, "--follow") == [
        {"kind": "step", **orphan.model_dump(mode="json")},
        {"kind": "end", "flow_id": fid, "status": "crashed"},
    ]


@contextmanager
def _follow(fid):
    with subprocess.Popen(
        [sys.executable, "-m", "flow_atelier.main", "logs", fid, "--jsonl", "--follow"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0,
    ) as proc:
        try:
            yield proc
        finally:
            if proc.poll() is None:
                proc.kill()


@pytest.mark.parametrize("interrupt", [False, pytest.param(True, marks=pytest.mark.skipif(
    os.name == "nt", reason="POSIX SIGINT exit convention",
))])
@pytest.mark.timeout(20)
def test_follow_flushes_history_then_new_steps_and_one_end(flow, interrupt):
    store, fid = flow
    first = _step("active", "before joining")
    asyncio.run(store.append_step(fid, first))
    with _follow(fid) as proc:
        assert json.loads(proc.stdout.readline()) == {"kind": "step", **first.model_dump(mode="json")}
        second = _step("active", "still running")
        asyncio.run(store.append_step(fid, second))
        assert json.loads(proc.stdout.readline()) == {"kind": "step", **second.model_dump(mode="json")}
        if interrupt:
            proc.send_signal(signal.SIGINT)
        else:
            entry = _entry(second)
            entry.steps = [first.step, second.step]
            asyncio.run(store.append_log(fid, entry))
            store.write_progress(fid, Progress(status=FlowStatus.completed))
        stdout, stderr = proc.communicate(timeout=10)
    records = [json.loads(line) for line in stdout.splitlines()]
    status = "interrupted" if interrupt else "completed"
    assert records[-1] == {"kind": "end", "flow_id": fid, "status": status}
    assert [r["kind"] for r in records[:-1]] == ([] if interrupt else ["entry"])
    if not interrupt:
        assert "steps" not in records[0]
        assert records[0]["output"] == "result"
    assert proc.returncode == (130 if interrupt else 0)
    assert stderr == b""


@pytest.mark.skipif(os.name == "nt", reason="POSIX SIGINT exit convention")
@pytest.mark.timeout(20)
def test_interrupt_during_a_pipe_write_finishes_the_json_line(flow):
    store, fid = flow
    large = _step("active", "x" * (4 * 1024 * 1024))
    asyncio.run(store.append_step(fid, large))
    with _follow(fid) as proc:
        prefix = proc.stdout.read(1)
        proc.send_signal(signal.SIGINT)
        rest, stderr = proc.communicate(timeout=10)
    step, end = map(json.loads, (prefix + rest).splitlines())
    assert step["step"]["text"] == large.step.text
    assert end == {"kind": "end", "flow_id": fid, "status": "interrupted"}
    assert proc.returncode == 130
    assert stderr == b""


@pytest.mark.parametrize(("flags", "code", "exit_code"), [
    ([], "unknown_flow", 1),
    (["--last", "bad"], "invalid_arguments", 2),
    (["--json"], "invalid_arguments", 2),
    (["--follow", "--last", "1"], "invalid_arguments", 2),
])
def test_jsonl_errors_on_stderr(flow, flags, code, exit_code):
    result = CliRunner().invoke(app, ["logs", "missing", "--jsonl", *flags])
    assert result.exit_code == exit_code
    assert result.stdout == ""
    assert json.loads(result.stderr)["error"]["code"] == code
