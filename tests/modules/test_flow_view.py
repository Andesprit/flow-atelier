"""Run page read models: the task map and one task's log."""
from __future__ import annotations

import json

from flow_atelier.modules.flow_view import build_flow_view, build_task_log
from flow_atelier.schemas.conduit import Conduit
from flow_atelier.schemas.log import IntermediateStep, LogEntry, StepKind, StepRecord
from flow_atelier.schemas.progress import FlowStatus, Progress, TaskProgress, TaskStatus


def _entry(**kw) -> LogEntry:
    """Build a finished attempt with sensible defaults.

    :param kw: fields to override.
    :returns: a :class:`LogEntry`.
    """
    base = {
        "task": "t",
        "tool": "tool:bash",
        "command": "make test",
        "started_at": "2026-09-25T14:00:00Z",
        "finished_at": "2026-09-25T14:00:10Z",
        "duration_seconds": 10.0,
    }
    return LogEntry(**{**base, **kw})


def _step(kind: StepKind, at: str, iteration: int = 1, **kw) -> StepRecord:
    """Build a live step record for task ``t``.

    :param kind: step kind.
    :param at: ISO timestamp.
    :param iteration: round the step belongs to.
    :param kw: step fields.
    :returns: a :class:`StepRecord`.
    """
    return StepRecord(
        task="t", iteration=iteration, step=IntermediateStep(kind=kind, timestamp=at, **kw)
    )


def test_flow_view_lists_tasks_with_plain_dependency_names():
    """Each task carries its dependency names without conditions, and its progress."""
    conduit = Conduit.model_validate(
        {
            "name": "c",
            "description": "d",
            "tasks": [
                {"a": {"description": "", "task": "echo a", "tool": "tool:bash"}},
                {
                    "b": {
                        "description": "",
                        "task": "echo b",
                        "tool": "tool:bash",
                        "depends_on": ["a"],
                        "repeat": 3,
                        "until": "output.match(ok)",
                    }
                },
                {
                    "c": {
                        "description": "last",
                        "task": "echo c",
                        "tool": "tool:bash",
                        "depends_on": ["b.output.match(ok)"],
                    }
                },
            ],
        }
    )
    progress = Progress(
        status=FlowStatus.running,
        current_tasks=["b"],
        tasks={
            "a": TaskProgress(status=TaskStatus.completed),
            "b": TaskProgress(status=TaskStatus.running, iteration=2, of=3),
        },
    )

    view = build_flow_view("c_x", "c", progress, conduit)

    assert [t.name for t in view.tasks] == ["a", "b", "c"]
    assert [t.depends_on for t in view.tasks] == [[], ["a"], ["b"]]
    assert [t.status for t in view.tasks] == ["completed", "running", "pending"]
    assert (view.tasks[1].iteration, view.tasks[1].of) == (2, 3)
    assert view.tasks[2].description == "last"
    assert view.current_tasks == ["b"]
    assert view.status == "running"


def test_flow_view_keeps_tasks_the_definition_no_longer_has():
    """A task that ran still shows when its conduit is gone or edited."""
    progress = Progress(tasks={"x": TaskProgress(status=TaskStatus.completed)})

    view = build_flow_view("gone_x", "gone", progress, None)

    assert [(t.name, t.status, t.tool) for t in view.tasks] == [("x", "completed", "")]


def test_task_log_groups_attempts_into_rounds():
    """A loop's rounds come back in order, each ending in its exit line."""
    entries = [
        _entry(iteration=2, of=2, stdout="3 passed\n", started_at="2026-09-25T14:01:00Z"),
        _entry(iteration=1, of=2, exit_code=1, stdout="1 failed\n", stderr="E boom\n"),
    ]
    progress = TaskProgress(status=TaskStatus.completed, iteration=2, of=2)

    log = build_task_log("t", "tool:bash", progress, entries, [])

    assert [r.iteration for r in log.rounds] == [1, 2]
    assert [r.status for r in log.rounds] == ["failed", "completed"]
    first = [(line.kind, line.text, line.level) for line in log.rounds[0].lines]
    assert first == [
        ("run", "$ make test", "info"),
        ("out", "1 failed", "info"),
        ("err", "E boom", "error"),
        ("failed", "exit 1 · 10s", "error"),
    ]
    assert log.of == 2


def test_task_log_folds_results_into_their_calls():
    """A failed result marks its call; a successful one adds no line."""
    steps = [
        _step(
            StepKind.tool_call,
            "2026-09-25T14:00:01Z",
            tool_call_id="1",
            tool_kind="read",
            tool_name="Read",
            locations=["payments/retry.py"],
        ),
        _step(StepKind.tool_result, "2026-09-25T14:00:02Z", tool_call_id="1", tool_status="completed"),
        _step(
            StepKind.tool_call,
            "2026-09-25T14:00:03Z",
            tool_call_id="2",
            tool_kind="execute",
            tool_name="Bash",
            tool_input=json.dumps({"command": "make test"}),
        ),
        _step(
            StepKind.tool_result,
            "2026-09-25T14:00:04Z",
            tool_call_id="2",
            tool_status="failed",
            tool_output="exit status 2",
        ),
        _step(StepKind.thinking, "2026-09-25T14:00:05Z", text="Cap after jitter."),
    ]
    progress = TaskProgress(status=TaskStatus.running)

    log = build_task_log("t", "harness:codex", progress, [], steps)

    (only,) = log.rounds
    assert only.status == "running"
    assert [(line.kind, line.text, line.level) for line in only.lines] == [
        ("read", "payments/retry.py", "info"),
        ("run", "make test → failed: exit status 2", "error"),
        ("thought", "Cap after jitter.", "info"),
    ]


def test_task_log_masks_credentials():
    """Secrets are masked the way the terminal masks them."""
    entries = [
        _entry(
            command="curl -H 'Authorization: Bearer abcdefghijklmnop' https://x",
            stdout="GH_TOKEN=ghp_abcdefghijklmnopqrstuvwx\n",
        )
    ]

    log = build_task_log("t", "tool:bash", None, entries, [])

    text = " ".join(line.text for line in log.rounds[0].lines)
    assert "abcdefghijklmnop" not in text
    assert "ghp_" not in text
    assert "***" in text


def test_task_log_gives_each_retry_the_steps_it_recorded():
    """Steps stamped before an attempt finished belong to that attempt."""
    entries = [
        _entry(
            tool="harness:codex",
            command="fix it",
            exit_code=1,
            finished_at="2026-09-25T14:00:10Z",
            extra={"attempt": 1, "of_attempts": 2},
        ),
        _entry(
            tool="harness:codex",
            command="fix it",
            output="Fixed.",
            started_at="2026-09-25T14:00:11Z",
            finished_at="2026-09-25T14:00:20Z",
            extra={"attempt": 2, "of_attempts": 2},
        ),
    ]
    steps = [
        _step(StepKind.thinking, "2026-09-25T14:00:05Z", text="first try"),
        _step(StepKind.thinking, "2026-09-25T14:00:15Z", text="second try"),
    ]

    log = build_task_log("t", "harness:codex", None, entries, steps)

    kinds = [(line.kind, line.text) for line in log.rounds[0].lines]
    assert kinds == [
        ("prompt", "fix it  (attempt 1 of 2)"),
        ("thought", "first try"),
        ("failed", "exit 1 · 10s"),
        ("prompt", "fix it  (attempt 2 of 2)"),
        ("thought", "second try"),
        ("said", "Fixed."),
        ("done", "exit 0 · 10s"),
    ]
    assert log.rounds[0].status == "completed"
