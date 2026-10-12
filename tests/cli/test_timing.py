"""CLI tests for `atelier timing` (per-task timing breakdown)."""
import json
import os

import pytest
from typer.testing import CliRunner

from flow_atelier.cli import app


@pytest.fixture
def workdir(tmp_path, monkeypatch):
    """Provide an isolated working dir with a `.atelier/flows` tree.

    :param tmp_path: pytest temp directory fixture.
    :param monkeypatch: pytest monkeypatch fixture.
    """
    (tmp_path / ".atelier" / "flows").mkdir(parents=True)
    monkeypatch.chdir(tmp_path)
    for k in list(os.environ):
        if k.startswith("ATELIER_") and k not in (
            "ATELIER_GLOBAL_ATELIER_DIR",
            "ATELIER_NO_UPDATE_CHECK",
        ):
            monkeypatch.delenv(k, raising=False)
    return tmp_path


def _entry(task, duration, iteration=1, of=1):
    """Build a minimal logs.jsonl entry dict for `task` with `duration`.

    :param task: task name.
    :param duration: duration_seconds value.
    :param iteration: iteration index for the entry.
    :param of: total iterations the entry belongs to.
    :returns: a dict matching the LogEntry on-disk schema.
    """
    return {
        "task": task,
        "tool": "bash",
        "iteration": iteration,
        "of": of,
        "started_at": "2026-01-01T00:00:00Z",
        "finished_at": "2026-01-01T00:00:01Z",
        "duration_seconds": duration,
    }


def _make_flow(workdir, flow_id, entries):
    """Create a flow dir on disk with a logs.jsonl built from `entries`.

    :param workdir: working directory path.
    :param flow_id: full flow id.
    :param entries: list of entry dicts to serialize, one JSON doc per line.
    :returns: the flow's directory Path.
    """
    flow_dir = workdir / ".atelier" / "flows" / flow_id
    flow_dir.mkdir(parents=True)
    lines = "".join(json.dumps(e) + "\n" for e in entries)
    (flow_dir / "logs.jsonl").write_text(lines)
    return flow_dir


def test_timing_sorts_slowest_first(workdir):
    """Multi-task flow lists the slowest task first with summed durations.

    :param workdir: isolated working directory fixture.
    """
    _make_flow(
        workdir,
        "20260101_aaaaaaaa_hello",
        [_entry("fast", 1.0), _entry("slow", 9.0)],
    )
    runner = CliRunner()
    result = runner.invoke(app, ["timing", "20260101_aaaaaaaa_hello"])
    assert result.exit_code == 0, result.output
    assert result.output.index("slow") < result.output.index("fast")


def test_timing_sums_repeated_task(workdir):
    """A repeated task reports summed total and a runs count across iterations.

    :param workdir: isolated working directory fixture.
    """
    _make_flow(
        workdir,
        "20260101_bbbbbbbb_hello",
        [
            _entry("loop", 2.0, iteration=1, of=3),
            _entry("loop", 3.0, iteration=2, of=3),
            _entry("loop", 5.0, iteration=3, of=3),
        ],
    )
    runner = CliRunner()
    result = runner.invoke(
        app, ["timing", "20260101_bbbbbbbb_hello", "--json"]
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["total_seconds"] == 10.0
    loop = next(t for t in payload["tasks"] if t["task"] == "loop")
    assert loop["total_seconds"] == 10.0
    assert loop["runs"] == 3
    assert loop["pct"] == 100.0


def test_timing_json_payload_shape(workdir):
    """`--json` emits flow_id, total_seconds and per-task pct summing to ~100.

    :param workdir: isolated working directory fixture.
    """
    _make_flow(
        workdir,
        "20260101_cccccccc_hello",
        [_entry("a", 3.0), _entry("b", 1.0)],
    )
    runner = CliRunner()
    result = runner.invoke(
        app, ["timing", "20260101_cccccccc_hello", "--json"]
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["flow_id"] == "20260101_cccccccc_hello"
    assert payload["total_seconds"] == 4.0
    assert {t["task"] for t in payload["tasks"]} == {"a", "b"}
    assert round(sum(t["pct"] for t in payload["tasks"])) == 100


def test_timing_unknown_flow(workdir):
    """An unknown flow id exits 1 with an `unknown flow` message.

    :param workdir: isolated working directory fixture.
    """
    runner = CliRunner()
    result = runner.invoke(app, ["timing", "nope"])
    assert result.exit_code == 1
    assert "unknown flow" in result.output


def test_timing_no_entries(workdir):
    """A flow dir with an empty logs.jsonl exits 1 with `no log entries`.

    :param workdir: isolated working directory fixture.
    """
    _make_flow(workdir, "20260101_dddddddd_hello", [])
    runner = CliRunner()
    result = runner.invoke(app, ["timing", "20260101_dddddddd_hello"])
    assert result.exit_code == 1
    assert "no log entries" in result.output


def test_timing_reports_wall_time_for_parallel_tasks(workdir):
    """Two overlapping tasks sum to more task time than the run's wall time.

    :param workdir: isolated working directory fixture.
    """
    a = {**_entry("a", 10.0), "finished_at": "2026-01-01T00:00:10Z"}
    b = {**_entry("b", 10.0), "finished_at": "2026-01-01T00:00:10Z"}
    _make_flow(workdir, "20260101_eeeeeeee_hello", [a, b])
    result = CliRunner().invoke(app, ["timing", "20260101_eeeeeeee_hello", "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["total_seconds"] == 20.0
    assert payload["wall_seconds"] == 10.0


def test_timing_splits_agent_startup(workdir):
    """Agent iterations sum their startup and first-output times per task.

    :param workdir: isolated working directory fixture.
    """
    timed = {"timings": {"startup_seconds": 1.5, "first_output_seconds": 2.0}}
    _make_flow(
        workdir,
        "20260101_ffffffff_hello",
        [
            {**_entry("agent", 8.0, iteration=1, of=2), "extra": timed},
            {**_entry("agent", 8.0, iteration=2, of=2), "extra": timed},
            _entry("shell", 1.0),
        ],
    )
    result = CliRunner().invoke(app, ["timing", "20260101_ffffffff_hello", "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    agent = next(t for t in payload["tasks"] if t["task"] == "agent")
    assert agent["startup_seconds"] == 3.0
    assert agent["first_output_seconds"] == 4.0
    assert payload["startup_seconds"] == 3.0
    shell = next(t for t in payload["tasks"] if t["task"] == "shell")
    assert shell["startup_seconds"] == 0.0

    table = CliRunner().invoke(app, ["timing", "20260101_ffffffff_hello"])
    assert "agent startup: 3.0s" in table.output
