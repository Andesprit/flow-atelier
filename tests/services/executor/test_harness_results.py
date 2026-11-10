"""How a finished agent turn becomes a task result, and how the agent is closed.

A calling agent acts on the exit code, so a turn the agent ended normally but
that holds no answer, or only the agent's own error message, must not be
reported as success.
"""
from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path
from typing import Any

import pytest

from flow_atelier.schemas.conduit import TaskDefinition
from flow_atelier.services.executor.base import FlowContext
from flow_atelier.services.executor.harness import (
    AGENT_CLOSE_GRACE_SECONDS,
    AcpHarnessExecutor,
    _close_agent_quickly,
)

FAKE_AGENT = Path(__file__).resolve().parents[2] / "fixtures" / "fake_acp_agent.py"


class _Sink:
    """PromptSink double for a non-interactive run."""

    async def display(self, text: str) -> None:
        """Ignore displayed text.

        :param text: the text shown.
        """

    async def request_input(self, prompt: str) -> str:
        """Fail: a single-turn task never asks.

        :param prompt: the question.
        """
        raise AssertionError("not asked")


async def _run(turn: dict[str, Any]) -> Any:
    """Run one non-interactive agent task whose single turn is ``turn``.

    :param turn: the fake agent's scripted turn.
    :returns: the task's ExecutionResult.
    """
    argv = [sys.executable, str(FAKE_AGENT), "--script", json.dumps({"turns": [turn]})]
    executor = AcpHarnessExecutor(launch_cmd=argv, sink=_Sink())
    task = TaskDefinition(name="h", description="d", task="x", tool="harness:fake")
    context = FlowContext(flow_id="fake", store=None, inputs={}, timeout=30)  # type: ignore[arg-type]
    return await executor.execute(task, "x", context)


@pytest.mark.parametrize(("chunks", "reason"), [
    ([], "agent returned no answer"),
    (["  \n"], "agent returned no answer"),
    (["Error: Not logged in. Please run /login"], "agent reported an error"),
    (["Invalid API key · Please run /login"], "agent reported an error"),
    (["The 'gpt-x' model is not supported when using Codex with a ChatGPT account."],
     "agent reported an error"),
])
async def test_a_turn_with_no_real_answer_fails(chunks: list[str], reason: str) -> None:
    """An empty reply, or one that is only a login or account error, failed."""
    result = await _run({"chunks": chunks})
    assert result.exit_code == 1
    assert result.stderr.startswith(reason)
    assert result.usage_limit is None


async def test_a_usage_limit_said_in_words_gets_the_limit_and_its_reset() -> None:
    """The engine's wait-and-retry needs the limit, not a plain failure."""
    result = await _run({"chunks": ["You've hit your usage limit. Try again in 2 hours."]})
    assert result.exit_code == 1
    assert result.stderr.startswith("agent reported a usage limit")
    assert result.usage_limit is not None
    assert result.usage_limit.resets_at is not None


@pytest.mark.parametrize("text", [
    "Claude Code plans have a 5-hour session limit.",
    "Not logged in? Then run gh auth login. " + "Details. " * 80,
    "The answer is 42.",
])
async def test_a_real_answer_that_mentions_limits_or_logins_succeeds(text: str) -> None:
    """Only a short reply that matches a narrow notice is judged a failure."""
    result = await _run({"chunks": [text]})
    assert result.exit_code == 0, result.stderr


async def test_a_turn_that_did_work_is_not_judged_by_its_words(tmp_path) -> None:
    """A turn with tool calls succeeds even with no closing text."""
    result = await _run({
        "ask_permission": {"summary": "edit NOTES.md", "kind": "edit",
                           "options": [{"id": "allow", "label": "Allow"}]},
        "chunks": [],
    })
    assert result.exit_code == 0, result.stderr


@pytest.mark.skipif(sys.platform == "win32", reason="the quick close is POSIX only")
async def test_an_agent_that_ignores_stdin_closing_is_ended_promptly() -> None:
    """codex-acp takes ~2 s to exit once stdin closes; the close does not wait it out."""
    proc = await asyncio.create_subprocess_exec(
        sys.executable, "-c", "import time; time.sleep(30)",
        stdin=asyncio.subprocess.PIPE,
    )
    started = time.monotonic()
    await _close_agent_quickly(proc)
    await asyncio.wait_for(proc.wait(), 5)
    assert time.monotonic() - started < AGENT_CLOSE_GRACE_SECONDS + 1.5
    assert proc.returncode is not None and proc.returncode != 0


def test_the_api_token_never_reaches_an_agent(monkeypatch) -> None:
    """A delegated agent must not be able to drive the local server."""
    monkeypatch.setenv("ATELIER_API_TOKEN", "secret")
    monkeypatch.setenv("SOME_KEY", "kept")
    env = AcpHarnessExecutor(launch_cmd=["x"], sink=_Sink())._spawn_env()
    assert "ATELIER_API_TOKEN" not in env
    assert env["SOME_KEY"] == "kept"


@pytest.mark.skipif(sys.platform == "win32", reason="process groups are POSIX only")
async def test_a_timed_out_agent_takes_what_it_started_with_it(tmp_path) -> None:
    """A tool or server the agent started does not outlive a timeout."""
    import os
    import signal as signal_module

    pidfile = tmp_path / "child.pid"
    script = json.dumps({"turns": [{"chunks": ["late"], "delay_before": 60}]})
    # The shell starts a background child, then becomes the agent.
    launch = [
        "sh", "-c",
        f'sleep 300 & echo $! > "{pidfile}"; exec "$0" "$@"',
        sys.executable, str(FAKE_AGENT), "--script", script,
    ]
    executor = AcpHarnessExecutor(launch_cmd=launch, sink=_Sink())
    task = TaskDefinition(name="h", description="d", task="x", tool="harness:fake")
    context = FlowContext(flow_id="fake", store=None, inputs={}, timeout=3)  # type: ignore[arg-type]
    result = await executor.execute(task, "x", context)
    assert result.exit_code == 124
    child = int(pidfile.read_text())
    for _ in range(50):
        try:
            os.kill(child, 0)
        except ProcessLookupError:
            break
        await asyncio.sleep(0.1)
    else:
        os.kill(child, signal_module.SIGKILL)
        raise AssertionError("the agent's background child outlived the timeout")
