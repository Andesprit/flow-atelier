"""HarnessExecutor unit tests using the fake ACP agent fixture."""
from __future__ import annotations

import asyncio
import contextlib
import json
import sys
from pathlib import Path
from typing import Any

from acp.connection import StreamDirection, StreamEvent
from acp.schema import AgentMessageChunk, TextContentBlock

from flow_atelier.schemas.conduit import TaskDefinition, ToolType
from flow_atelier.services.executor.base import FlowContext
from flow_atelier.services.executor.harness import (
    AGENT_STDERR_LINES,
    DEFAULT_DONE_MARKER,
    AcpHarnessExecutor,
    build_interactive_suffix,
)

FAKE_AGENT = Path(__file__).resolve().parents[2] / "fixtures" / "fake_acp_agent.py"


def _fake_cmd(script: dict[str, Any]) -> list[str]:
    """Build a launch command that runs the fake ACP agent with a script.

    :param script: JSON-serializable ACP scenario for the fake agent.
    """
    return [sys.executable, str(FAKE_AGENT), "--script", json.dumps(script)]


def _task(prompt: str, *, interactive: bool = False) -> TaskDefinition:
    """Build a TaskDefinition for harness tests.

    :param prompt: task body / prompt sent to the harness.
    :param interactive: whether the task should run in interactive mode.
    """
    return TaskDefinition(
        name="h",
        description="d",
        task=prompt,
        tool=ToolType.claude,
        depends_on=[],
        interactive=interactive,
    )


def _ctx(timeout: int = 30) -> FlowContext:
    """Build a minimal FlowContext for harness tests.

    :param timeout: per-task timeout in seconds.
    """
    return FlowContext(
        flow_id="fake", store=None, inputs={}, timeout=timeout  # type: ignore[arg-type]
    )


class RecordingSink:
    """PromptSink double for tests."""

    def __init__(
        self,
        replies: list[str] | None = None,
        perm_choice: str | None = None,
    ) -> None:
        """Initialize the recording sink with scripted replies and perm choice.

        :param replies: scripted user replies, popped in order on each request_input.
        :param perm_choice: option id to return from request_permission, or None to pick first.
        """
        self.display_log: list[str] = []
        self.input_prompts: list[str] = []
        self.agent_turn_starts: list[str] = []
        self._replies = list(replies or [])
        self.perm_log: list[str] = []
        self._perm_choice = perm_choice

    async def display(self, text: str) -> None:
        """Record a chunk of displayed text.

        :param text: text chunk forwarded by the harness.
        """
        self.display_log.append(text)

    async def start_agent_turn(self, label: str = "agent") -> None:
        """Record an agent-turn marker.

        :param label: label for the agent turn.
        """
        self.agent_turn_starts.append(label)

    async def request_input(self, prompt: str) -> str:
        """Return the next scripted reply or raise EOFError.

        :param prompt: prompt shown to the user.
        """
        self.input_prompts.append(prompt)
        if not self._replies:
            raise EOFError("no more scripted replies")
        return self._replies.pop(0)

    async def request_permission(self, summary, options):
        """Canary: nothing in production may route permissions to a sink.

        :class:`PromptSink` deliberately has no permission method — the
        harness answers `session/request_permission` itself. This records
        any call so the `perm_log == []` assertions stay meaningful instead
        of passing vacuously.

        :param summary: human-readable permission summary.
        :param options: available permission options.
        """
        self.perm_log.append(summary)
        if self._perm_choice is not None:
            return self._perm_choice
        return options[0].id


class TestNonInteractive:
    async def test_single_turn_success(self) -> None:
        """Verify a single-turn agent run returns its output with exit 0."""
        sink = RecordingSink()
        executor = AcpHarnessExecutor(
            launch_cmd=_fake_cmd(
                {"turns": [{"chunks": ["answer: ok"], "stop": "end_turn"}]}
            ),
            sink=sink,
        )
        result = await executor.execute(_task("hello"), "hello", _ctx())
        assert result.exit_code == 0
        assert "answer: ok" in result.output

    async def test_chunks_concatenated(self) -> None:
        """Verify multiple agent chunks are concatenated into final output."""
        sink = RecordingSink()
        executor = AcpHarnessExecutor(
            launch_cmd=_fake_cmd(
                {
                    "turns": [
                        {
                            "chunks": ["hello ", "from ", "agent"],
                            "stop": "end_turn",
                        }
                    ]
                }
            ),
            sink=sink,
        )
        result = await executor.execute(_task("x"), "x", _ctx())
        assert result.exit_code == 0
        assert "hello from agent" in result.output

    async def test_refusal_marks_failure(self) -> None:
        """Verify a refusal stop reason maps to a non-zero exit code."""
        sink = RecordingSink()
        executor = AcpHarnessExecutor(
            launch_cmd=_fake_cmd(
                {"turns": [{"chunks": ["cannot"], "stop": "refusal"}]}
            ),
            sink=sink,
        )
        result = await executor.execute(_task("x"), "x", _ctx())
        assert result.exit_code != 0
        assert "refusal" in result.stderr

    async def test_max_tokens_marks_failure(self) -> None:
        """Verify a max_tokens stop maps to exit 1 with truncation stderr."""
        sink = RecordingSink()
        executor = AcpHarnessExecutor(
            launch_cmd=_fake_cmd(
                {"turns": [{"chunks": ["partial answer"], "stop": "max_tokens"}]}
            ),
            sink=sink,
        )
        result = await executor.execute(_task("x"), "x", _ctx())
        assert result.exit_code == 1
        assert "max_tokens" in result.stderr
        assert "partial answer" in result.output

    async def test_timeout(self) -> None:
        """Verify the harness times out a slow agent with exit code 124."""
        sink = RecordingSink()
        executor = AcpHarnessExecutor(
            launch_cmd=_fake_cmd(
                {
                    "turns": [
                        {"delay_before": 5, "chunks": ["late"], "stop": "end_turn"}
                    ]
                }
            ),
            sink=sink,
        )
        result = await executor.execute(_task("slow"), "slow", _ctx(timeout=2))
        assert result.exit_code == 124
        assert "timeout" in result.stderr.lower()

    async def test_usage_captured_from_prompt_and_usage_update(self) -> None:
        """A single-turn run reflects both the per-turn token breakdown
        (PromptResponse.usage) and the cumulative cost (UsageUpdate)."""
        sink = RecordingSink()
        executor = AcpHarnessExecutor(
            launch_cmd=_fake_cmd(
                {
                    "turns": [
                        {
                            "chunks": ["ok"],
                            "stop": "end_turn",
                            "cost": 0.0123,
                            "usage": {
                                "input_tokens": 1000,
                                "output_tokens": 200,
                                "total_tokens": 1200,
                            },
                        }
                    ]
                }
            ),
            sink=sink,
        )
        result = await executor.execute(_task("x"), "x", _ctx())
        assert result.exit_code == 0
        assert result.usage is not None
        assert result.usage.input_tokens == 1000
        assert result.usage.output_tokens == 200
        assert result.usage.total_tokens == 1200
        assert result.usage.cost == 0.0123

    async def test_usage_none_when_agent_reports_nothing(self) -> None:
        """A harness that emits neither usage nor cost yields usage=None,
        not a fabricated all-zero record."""
        sink = RecordingSink()
        executor = AcpHarnessExecutor(
            launch_cmd=_fake_cmd(
                {"turns": [{"chunks": ["ok"], "stop": "end_turn"}]}
            ),
            sink=sink,
        )
        result = await executor.execute(_task("x"), "x", _ctx())
        assert result.exit_code == 0
        assert result.usage is None

    async def test_non_interactive_does_not_stream_to_sink(self) -> None:
        """Non-interactive harness tasks must not double-render: chunks
        are captured into the result but NOT mirrored to the sink (the
        engine renders a final panel from result.output afterwards)."""
        sink = RecordingSink()
        executor = AcpHarnessExecutor(
            launch_cmd=_fake_cmd(
                {"turns": [{"chunks": ["A", "B"], "stop": "end_turn"}]}
            ),
            sink=sink,
        )
        result = await executor.execute(_task("x"), "x", _ctx())
        assert "AB" in result.output
        assert sink.display_log == []


class TestInteractiveStreaming:
    async def test_interactive_streams_chunks_to_sink(self) -> None:
        """Interactive mode keeps a live stream so the user can follow
        the agent while deciding their next reply."""
        sink = RecordingSink()
        executor = AcpHarnessExecutor(
            launch_cmd=_fake_cmd(
                {
                    "turns": [
                        {
                            "chunks": ["A", "B", "[ATELIER_DONE]"],
                            "stop": "end_turn",
                        }
                    ]
                }
            ),
            sink=sink,
        )
        await executor.execute(_task("x", interactive=True), "x", _ctx())
        assert "A" in "".join(sink.display_log)
        assert "B" in "".join(sink.display_log)


class TestInteractive:
    async def test_interactive_calls_start_agent_turn_per_turn(self) -> None:
        """The interactive loop must signal each agent turn to the sink so
        the terminal UI can bracket it with a styled rule."""
        sink = RecordingSink(replies=["my answer"])
        executor = AcpHarnessExecutor(
            launch_cmd=_fake_cmd(
                {
                    "turns": [
                        {"chunks": ["asking?"], "stop": "end_turn"},
                        {"chunks": ["thanks [ATELIER_DONE]"], "stop": "end_turn"},
                    ]
                }
            ),
            sink=sink,
        )
        await executor.execute(
            _task("go", interactive=True), "go", _ctx()
        )
        # Two agent turns ⇒ two calls.
        assert len(sink.agent_turn_starts) == 2

    async def test_marker_hidden_from_live_stream(self) -> None:
        """The done marker must not be displayed to the user even in
        interactive mode (live stream path)."""
        sink = RecordingSink()
        executor = AcpHarnessExecutor(
            launch_cmd=_fake_cmd(
                {
                    "turns": [
                        {
                            "chunks": ["here is the answer ", "[ATELIER_DONE]"],
                            "stop": "end_turn",
                        }
                    ]
                }
            ),
            sink=sink,
        )
        await executor.execute(
            _task("x", interactive=True), "x", _ctx()
        )
        displayed = "".join(sink.display_log)
        assert "here is the answer" in displayed
        assert "[ATELIER_DONE]" not in displayed

    async def test_marker_first_turn_terminates(self) -> None:
        """Verify the done marker on the first turn ends the interactive loop."""
        sink = RecordingSink()
        executor = AcpHarnessExecutor(
            launch_cmd=_fake_cmd(
                {
                    "turns": [
                        {
                            "chunks": ["doing work ", "[ATELIER_DONE]"],
                            "stop": "end_turn",
                        }
                    ]
                }
            ),
            sink=sink,
        )
        result = await executor.execute(
            _task("do it", interactive=True), "do it", _ctx()
        )
        assert result.exit_code == 0
        # The done marker is an internal protocol sentinel — it must NOT
        # be returned to callers as user-visible content (would otherwise
        # also persist into logs.json).
        assert "[ATELIER_DONE]" not in result.output
        assert "doing work" in result.output
        assert sink.input_prompts == []

    async def test_done_marker_from_previous_turn_does_not_terminate(self) -> None:
        """A marker already in the buffer from an earlier turn (echoed or
        late-dispatched chunk) must not end the session: only the current
        turn's chunks count for termination."""
        from flow_atelier.services.executor.harness import _BufferingClient

        sink = RecordingSink(replies=["continue"])
        executor = AcpHarnessExecutor(launch_cmd=["unused"], sink=sink)
        client = _BufferingClient(sink)
        client.buffer.append(f"stale text {DEFAULT_DONE_MARKER}")

        turns = [
            {"chunks": ["no marker this turn"], "stop": "end_turn"},
            {"chunks": [f"done now {DEFAULT_DONE_MARKER}"], "stop": "end_turn"},
        ]

        class FakeResp:
            def __init__(self, stop: str) -> None:
                self.stop_reason = stop
                self.usage = None

        class FakeConn:
            def __init__(self) -> None:
                self.calls = 0

            async def prompt(self, prompt, session_id):
                turn = turns[self.calls]
                self.calls += 1
                for chunk in turn["chunks"]:
                    client.buffer.append(chunk)
                return FakeResp(turn["stop"])

        result = await executor._run_interactive(FakeConn(), "s", "go", client)
        assert result.exit_code == 0
        # The stale marker must not have ended turn 1: the loop asked for
        # a reply and only terminated on turn 2's fresh marker.
        assert len(sink.input_prompts) == 1
        assert "done now" in result.last_turn_output

    async def test_max_tokens_continues_without_asking_user(self) -> None:
        """A truncated (max_tokens) interactive turn must re-prompt the agent to
        continue rather than handing control back to the human as if done."""
        from flow_atelier.services.executor.harness import _BufferingClient

        sink = RecordingSink()  # no replies: a request_input would raise EOFError
        executor = AcpHarnessExecutor(launch_cmd=["unused"], sink=sink)
        client = _BufferingClient(sink)

        turns = [
            {"chunks": ["partial..."], "stop": "max_tokens"},
            {"chunks": [f"...and the rest {DEFAULT_DONE_MARKER}"], "stop": "end_turn"},
        ]

        class FakeResp:
            def __init__(self, stop: str) -> None:
                self.stop_reason = stop
                self.usage = None

        class FakeConn:
            def __init__(self) -> None:
                self.calls = 0
                self.prompts: list[str] = []

            async def prompt(self, prompt, session_id):
                self.prompts.append(prompt[0].text)
                turn = turns[self.calls]
                self.calls += 1
                for chunk in turn["chunks"]:
                    client.buffer.append(chunk)
                return FakeResp(turn["stop"])

        conn = FakeConn()
        result = await executor._run_interactive(conn, "s", "go", client)

        assert result.exit_code == 0
        # The truncated turn was NOT surfaced to the human.
        assert sink.input_prompts == []
        # Turn 2's prompt was a continuation nudge to the agent, not a user reply.
        assert "cut off" in conn.prompts[1]
        # Final output stitches both turns and drops the protocol sentinel.
        assert "partial..." in result.output
        assert "and the rest" in result.output
        assert DEFAULT_DONE_MARKER not in result.output

    async def test_multi_turn_with_user_reply(self) -> None:
        """Verify a multi-turn interaction stitches replies into the output."""
        sink = RecordingSink(replies=["luis"])
        executor = AcpHarnessExecutor(
            launch_cmd=_fake_cmd(
                {
                    "turns": [
                        {"chunks": ["what is your name?"], "stop": "end_turn"},
                        {
                            "chunks": ["hello luis [ATELIER_DONE]"],
                            "stop": "end_turn",
                        },
                    ]
                }
            ),
            sink=sink,
        )
        result = await executor.execute(
            _task("greet me", interactive=True), "greet me", _ctx()
        )
        assert result.exit_code == 0
        assert "what is your name?" in result.output
        assert "hello luis" in result.output
        # Marker stripped from the user-visible result.
        assert "[ATELIER_DONE]" not in result.output
        assert len(sink.input_prompts) == 1

    async def test_missing_marker_fails_when_sink_exhausted(self) -> None:
        """Verify a missing done marker plus exhausted sink yields failure."""
        sink = RecordingSink(replies=[])
        executor = AcpHarnessExecutor(
            launch_cmd=_fake_cmd(
                {"turns": [{"chunks": ["ready"], "stop": "end_turn"}]}
            ),
            sink=sink,
        )
        result = await executor.execute(
            _task("go", interactive=True), "go", _ctx()
        )
        assert result.exit_code != 0
        assert "[ATELIER_DONE]" not in result.output

    async def test_custom_marker(self) -> None:
        """Verify a custom done marker is honored and stripped from output."""
        sink = RecordingSink()
        executor = AcpHarnessExecutor(
            launch_cmd=_fake_cmd(
                {"turns": [{"chunks": ["done <<FIN>>"], "stop": "end_turn"}]}
            ),
            sink=sink,
            done_marker="<<FIN>>",
        )
        result = await executor.execute(
            _task("x", interactive=True), "x", _ctx()
        )
        assert result.exit_code == 0
        # Custom markers are stripped just like the default.
        assert "<<FIN>>" not in result.output
        assert "done" in result.output

    async def test_permission_auto_approved_without_sink(self) -> None:
        """Verify permission requests with an allow option auto-approve."""
        sink = RecordingSink()
        executor = AcpHarnessExecutor(
            launch_cmd=_fake_cmd(
                {
                    "turns": [
                        {
                            "ask_permission": {
                                "summary": "run rm?",
                                "options": [
                                    {"id": "allow", "label": "Allow", "kind": "allow_once"},
                                    {"id": "deny", "label": "Deny", "kind": "reject_once"},
                                ],
                            },
                            "chunks": [" [ATELIER_DONE]"],
                            "stop": "end_turn",
                        }
                    ]
                }
            ),
            sink=sink,
        )
        result = await executor.execute(
            _task("x", interactive=True), "x", _ctx()
        )
        assert result.exit_code == 0
        assert sink.perm_log == []
        assert "[perm:allow]" in result.output

    async def test_permission_prefers_allow_always(self) -> None:
        """Verify allow_always is preferred over allow_once when both exist."""
        sink = RecordingSink()
        executor = AcpHarnessExecutor(
            launch_cmd=_fake_cmd(
                {
                    "turns": [
                        {
                            "ask_permission": {
                                "summary": "run rm?",
                                "options": [
                                    {"id": "once", "label": "Once", "kind": "allow_once"},
                                    {"id": "always", "label": "Always", "kind": "allow_always"},
                                ],
                            },
                            "chunks": [" [ATELIER_DONE]"],
                            "stop": "end_turn",
                        }
                    ]
                }
            ),
            sink=sink,
        )
        result = await executor.execute(
            _task("x", interactive=True), "x", _ctx()
        )
        assert result.exit_code == 0
        assert "[perm:always]" in result.output

    async def test_permission_denies_when_only_reject_options(self) -> None:
        """Verify reject-only option lists produce a denial outcome."""
        sink = RecordingSink()
        executor = AcpHarnessExecutor(
            launch_cmd=_fake_cmd(
                {
                    "turns": [
                        {
                            "ask_permission": {
                                "summary": "run rm?",
                                "options": [
                                    {"id": "no", "label": "No", "kind": "reject_once"},
                                ],
                            },
                            "chunks": [" [ATELIER_DONE]"],
                            "stop": "end_turn",
                        }
                    ]
                }
            ),
            sink=sink,
        )
        result = await executor.execute(
            _task("x", interactive=True), "x", _ctx()
        )
        assert result.exit_code == 0
        assert sink.perm_log == []
        # Empty option_id from DeniedOutcome echoes as [perm:].
        assert "[perm:]" in result.output

    async def test_session_mode_switched_to_bypass(self) -> None:
        """Verify the harness switches to bypassPermissions when offered."""
        sink = RecordingSink()
        executor = AcpHarnessExecutor(
            launch_cmd=_fake_cmd(
                {
                    "modes": {
                        "current": "default",
                        "available": [
                            {"id": "default", "name": "Default"},
                            {"id": "bypassPermissions", "name": "Bypass"},
                        ],
                    },
                    "turns": [
                        {"chunks": ["done [ATELIER_DONE]"], "stop": "end_turn"},
                    ],
                }
            ),
            sink=sink,
        )
        result = await executor.execute(
            _task("x", interactive=True), "x", _ctx()
        )
        assert result.exit_code == 0
        assert "[mode_set:bypassPermissions]" in result.output
        assert sink.perm_log == []

    async def test_session_mode_unchanged_when_no_permissive_option(self) -> None:
        """Verify the harness leaves the mode alone when no permissive option exists."""
        sink = RecordingSink()
        executor = AcpHarnessExecutor(
            launch_cmd=_fake_cmd(
                {
                    "modes": {
                        "current": "default",
                        "available": [
                            {"id": "default", "name": "Default"},
                            {"id": "plan", "name": "Plan"},
                        ],
                    },
                    "turns": [
                        {"chunks": ["done [ATELIER_DONE]"], "stop": "end_turn"},
                    ],
                }
            ),
            sink=sink,
        )
        result = await executor.execute(
            _task("x", interactive=True), "x", _ctx()
        )
        assert result.exit_code == 0
        assert "[mode_set:" not in result.output

    async def test_model_selected_through_config_option(self) -> None:
        """A pinned model is set through the `model` config option before the prompt."""
        executor = AcpHarnessExecutor(
            launch_cmd=_fake_cmd(
                {
                    "models": {
                        "current": "m1",
                        "available": [{"id": "m1", "name": "One"}, {"id": "m2", "name": "Two"}],
                    },
                    "turns": [{"chunks": ["done"], "stop": "end_turn"}],
                }
            ),
            sink=RecordingSink(),
        ).with_model("m2")
        result = await executor.execute(_task("x"), "x", _ctx())
        assert result.exit_code == 0, result.output
        assert "[config_set:model=m2]" in result.output

    async def test_model_selected_through_legacy_set_model(self) -> None:
        """Without a config option the older session/set_model path is used."""
        executor = AcpHarnessExecutor(
            launch_cmd=_fake_cmd(
                {
                    "models": {
                        "via": "legacy",
                        "current": "m1",
                        "available": [{"id": "m1", "name": "One"}, {"id": "m2", "name": "Two"}],
                    },
                    "turns": [{"chunks": ["done"], "stop": "end_turn"}],
                }
            ),
            sink=RecordingSink(),
        ).with_model("m2")
        result = await executor.execute(_task("x"), "x", _ctx())
        assert result.exit_code == 0, result.output
        assert "[model_set:m2]" in result.output

    async def test_model_already_current_is_not_reset(self) -> None:
        """Asking for the model the session opened on sends no request."""
        executor = AcpHarnessExecutor(
            launch_cmd=_fake_cmd(
                {
                    "models": {"current": "m1", "available": [{"id": "m1", "name": "One"}]},
                    "turns": [{"chunks": ["done"], "stop": "end_turn"}],
                }
            ),
            sink=RecordingSink(),
        ).with_model("m1")
        result = await executor.execute(_task("x"), "x", _ctx())
        assert result.exit_code == 0, result.output
        assert "[config_set:" not in result.output

    async def test_unlisted_model_fails_before_the_prompt(self) -> None:
        """A model the agent does not offer fails the task and names the offer."""
        executor = AcpHarnessExecutor(
            launch_cmd=_fake_cmd(
                {
                    "models": {
                        "current": "m1",
                        "available": [{"id": "m1", "name": "One"}, {"id": "m2", "name": "Two"}],
                    },
                    "turns": [{"chunks": ["done"], "stop": "end_turn"}],
                }
            ),
            sink=RecordingSink(),
        ).with_model("m9")
        result = await executor.execute(_task("x"), "x", _ctx())
        assert result.exit_code != 0
        assert "'m9' is not offered" in result.stderr
        assert "m1, m2" in result.stderr
        assert "done" not in result.output

    async def test_model_on_agent_without_choice_fails(self) -> None:
        """An agent that advertises no models cannot honour a pinned one."""
        executor = AcpHarnessExecutor(
            launch_cmd=_fake_cmd({"turns": [{"chunks": ["done"], "stop": "end_turn"}]}),
            sink=RecordingSink(),
        ).with_model("m1")
        result = await executor.execute(_task("x"), "x", _ctx())
        assert result.exit_code != 0
        assert "does not support model selection" in result.stderr

    async def test_session_mode_absent_is_no_op(self) -> None:
        """Verify the harness is a no-op when the agent advertises no modes."""
        sink = RecordingSink()
        executor = AcpHarnessExecutor(
            launch_cmd=_fake_cmd(
                {
                    "turns": [
                        {"chunks": ["done [ATELIER_DONE]"], "stop": "end_turn"},
                    ],
                }
            ),
            sink=sink,
        )
        result = await executor.execute(
            _task("x", interactive=True), "x", _ctx()
        )
        assert result.exit_code == 0
        assert "[mode_set:" not in result.output


class TestSpawnEnvironment:
    def test_parent_environment_is_inherited(self, monkeypatch) -> None:
        """The agent must see our environment, not the transport's trimmed set.

        Without this the ACP transport hands the agent only HOME/PATH/SHELL/
        TERM/USER/LOGNAME, silently breaking proxies, custom CAs, API-key auth
        and any `git push` the agent attempts (no SSH_AUTH_SOCK).

        :param monkeypatch: pytest monkeypatch fixture.
        """
        monkeypatch.setenv("SSH_AUTH_SOCK", "/tmp/agent.sock")
        env = AcpHarnessExecutor(launch_cmd=["x"], sink=RecordingSink())._spawn_env()
        assert env["SSH_AUTH_SOCK"] == "/tmp/agent.sock"

    def test_agent_env_overrides_the_inherited_value(self, monkeypatch) -> None:
        """A harness's own env wins over the same name in our environment.

        :param monkeypatch: pytest monkeypatch fixture.
        """
        monkeypatch.setenv("AGENT_AUTO_UPDATE", "1")
        executor = AcpHarnessExecutor(
            launch_cmd=["x"], sink=RecordingSink(), env={"AGENT_AUTO_UPDATE": "0"}
        )
        assert executor._spawn_env()["AGENT_AUTO_UPDATE"] == "0"

    async def test_declared_env_reaches_the_agent_process(self, tmp_path) -> None:
        """An end-to-end check that env actually lands in the spawned process.

        :param tmp_path: pytest temp directory fixture.
        """
        probe = tmp_path / "probe.py"
        probe.write_text(
            "import os, pathlib, sys\n"
            f"pathlib.Path({str(tmp_path / 'seen.txt')!r}).write_text("
            "os.environ.get('ATELIER_PROBE', ''))\n"
        )
        executor = AcpHarnessExecutor(
            launch_cmd=[sys.executable, str(probe)],
            sink=RecordingSink(),
            env={"ATELIER_PROBE": "reached"},
        )
        await executor.execute(_task("hi"), "hi", _ctx(timeout=15))
        assert (tmp_path / "seen.txt").read_text() == "reached"


class TestAtelierHarnessWiring:
    def test_atelier_exposes_registry_and_legacy_harness_keys(
        self, monkeypatch, tmp_path
    ) -> None:
        """Verify Atelier registers registry agents plus the legacy names.

        :param monkeypatch: pytest monkeypatch fixture.
        :param tmp_path: pytest temp directory fixture.
        """
        monkeypatch.chdir(tmp_path)
        from flow_atelier.core.atelier import Atelier

        a = Atelier()
        # The names that predate the ACP registry keep working...
        for legacy in (
            "harness:claude-code",
            "harness:codex",
            "harness:copilot",
            "harness:cursor",
            "harness:opencode",
        ):
            assert legacy in a.executors, legacy
        # ...alongside every agent the registry lists, by its own id.
        assert "harness:gemini" in a.executors
        assert "gemini-cli" in " ".join(a.executors["harness:gemini"].launch_cmd)

    def test_legacy_alias_tracks_the_registry_entry(self, monkeypatch, tmp_path) -> None:
        """Verify harness:claude-code resolves to the registry's claude agent.

        :param monkeypatch: pytest monkeypatch fixture.
        :param tmp_path: pytest temp directory fixture.
        """
        monkeypatch.chdir(tmp_path)
        from flow_atelier.core.atelier import Atelier

        a = Atelier()
        assert (
            a.executors["harness:claude-code"].launch_cmd
            == a.executors["harness:claude-acp"].launch_cmd
        )
        assert "claude-agent-acp" in " ".join(
            a.executors["harness:claude-code"].launch_cmd
        )

    def test_opencode_launch_cmd_env_override(self, monkeypatch, tmp_path) -> None:
        """Verify ATELIER_OPENCODE_LAUNCH_CMD overrides the opencode launch cmd.

        :param monkeypatch: pytest monkeypatch fixture.
        :param tmp_path: pytest temp directory fixture.
        """
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("ATELIER_OPENCODE_LAUNCH_CMD", '["x","y"]')
        from flow_atelier.core.atelier import Atelier

        a = Atelier()
        assert a.executors["harness:opencode"].launch_cmd == ["x", "y"]

    def test_copilot_launch_cmd_env_override(self, monkeypatch, tmp_path) -> None:
        """Verify ATELIER_COPILOT_LAUNCH_CMD overrides the copilot launch cmd.

        :param monkeypatch: pytest monkeypatch fixture.
        :param tmp_path: pytest temp directory fixture.
        """
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("ATELIER_COPILOT_LAUNCH_CMD", '["x","y"]')
        from flow_atelier.core.atelier import Atelier

        a = Atelier()
        assert a.executors["harness:copilot"].launch_cmd == ["x", "y"]

    def test_cursor_launch_cmd_env_override(self, monkeypatch, tmp_path) -> None:
        """Verify ATELIER_CURSOR_LAUNCH_CMD overrides the cursor launch cmd.

        :param monkeypatch: pytest monkeypatch fixture.
        :param tmp_path: pytest temp directory fixture.
        """
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("ATELIER_CURSOR_LAUNCH_CMD", '["x","y"]')
        from flow_atelier.core.atelier import Atelier

        a = Atelier()
        assert a.executors["harness:cursor"].launch_cmd == ["x", "y"]


def test_default_marker_constant() -> None:
    """Verify the default done-marker constant matches the protocol value."""
    assert DEFAULT_DONE_MARKER == "[ATELIER_DONE]"


def test_interactive_suffix_contains_marker() -> None:
    """Verify build_interactive_suffix injects the marker and an instruction."""
    suffix = build_interactive_suffix("[ATELIER_DONE]")
    assert "[ATELIER_DONE]" in suffix
    assert "do not" in suffix.lower() or "not echo" in suffix.lower()


class TestLastTurnOutput:
    async def test_single_turn_last_turn_matches_full_output(self) -> None:
        """When the done marker fires on the first turn, last_turn_output
        equals the cleaned full output (single turn → no slicing needed)."""
        sink = RecordingSink()
        executor = AcpHarnessExecutor(
            launch_cmd=_fake_cmd(
                {
                    "turns": [
                        {
                            "chunks": ["only turn ", "[ATELIER_DONE]"],
                            "stop": "end_turn",
                        }
                    ]
                }
            ),
            sink=sink,
        )
        result = await executor.execute(
            _task("x", interactive=True), "x", _ctx()
        )
        assert result.exit_code == 0
        assert result.last_turn_output == "only turn"
        assert result.output == "only turn"

    async def test_multi_turn_last_turn_is_only_final_turn(self) -> None:
        """In a multi-turn interactive session, last_turn_output contains
        only the final agent turn's text — not the full transcript that
        result.output / stdout / logs.json continue to carry."""
        sink = RecordingSink(replies=["my reply"])
        executor = AcpHarnessExecutor(
            launch_cmd=_fake_cmd(
                {
                    "turns": [
                        {"chunks": ["first turn says hi"], "stop": "end_turn"},
                        {
                            "chunks": ["final answer here [ATELIER_DONE]"],
                            "stop": "end_turn",
                        },
                    ]
                }
            ),
            sink=sink,
        )
        result = await executor.execute(
            _task("go", interactive=True), "go", _ctx()
        )
        assert result.exit_code == 0
        # Full transcript stays in output/stdout for logs.json.
        assert "first turn says hi" in result.output
        assert "final answer here" in result.output
        # last_turn_output is final-turn-only.
        assert result.last_turn_output == "final answer here"
        assert "first turn" not in (result.last_turn_output or "")

    async def test_non_interactive_last_turn_output_is_none(self) -> None:
        """Non-interactive (single-turn) execution leaves last_turn_output
        as None so the engine falls back to result.output."""
        sink = RecordingSink()
        executor = AcpHarnessExecutor(
            launch_cmd=_fake_cmd(
                {"turns": [{"chunks": ["hi"], "stop": "end_turn"}]}
            ),
            sink=sink,
        )
        result = await executor.execute(
            _task("x", interactive=False), "x", _ctx()
        )
        assert result.exit_code == 0
        assert result.last_turn_output is None
        assert "hi" in result.output


class TestAwaitPendingUpdates:
    """The turn boundary must not read the buffer before handlers finish.

    Built on the ACP stream-observer hook rather than the library's private
    queue/supervisor: the receive loop notifies observers synchronously and
    in order, so counting `session/update` there and again in the handler is
    enough to know when a turn's notifications have all landed.
    """

    def _client(self) -> Any:
        """Build a buffering client with a no-op sink."""
        from flow_atelier.services.executor.harness import _BufferingClient

        return _BufferingClient(RecordingSink())

    @staticmethod
    def _update_event(direction=StreamDirection.INCOMING, method="session/update"):
        """Build a raw stream event as the ACP connection would emit one.

        :param direction: message direction.
        :param method: JSON-RPC method name.
        """
        return StreamEvent(direction, {"method": method, "params": {}})

    async def test_waits_for_a_handler_that_lands_after_a_scheduling_gap(
        self,
    ) -> None:
        """A slow handler must complete before the wait returns.

        This is the regression the drain exists for: a buffer-stability poll
        samples the empty buffer and returns early, dropping the last chunk.
        """
        client = self._client()
        chunk = AgentMessageChunk(
            session_update="agent_message_chunk",
            content=TextContentBlock(type="text", text="late chunk"),
        )

        async def slow_handler() -> None:
            """Handle an update only after yielding to the loop."""
            await asyncio.sleep(0.05)
            await client.session_update("s", chunk)

        client.observe(self._update_event())
        task = asyncio.create_task(slow_handler())
        await client.await_pending_updates()
        assert client.buffer == ["late chunk"]
        await task

    async def test_observer_is_wired_into_a_real_run(self) -> None:
        """The observer must actually fire against the real ACP connection.

        If `observers=` were ever ignored, the received count would stay at
        zero and the wait would silently degrade to a no-op — reintroducing
        the dropped-chunk bug it exists to prevent. Assert on the counters
        of the client a real run used.
        """
        from flow_atelier.services.executor import harness as harness_mod

        seen: list[Any] = []
        original = harness_mod._BufferingClient

        def capture(*args: Any, **kwargs: Any) -> Any:
            """Record each client the executor builds."""
            client = original(*args, **kwargs)
            seen.append(client)
            return client

        harness_mod._BufferingClient = capture
        try:
            executor = AcpHarnessExecutor(
                launch_cmd=_fake_cmd(
                    {"turns": [{"chunks": ["a", "b", "c"], "stop": "end_turn"}]}
                ),
                sink=RecordingSink(),
            )
            result = await executor.execute(_task("x"), "x", _ctx())
        finally:
            harness_mod._BufferingClient = original

        assert result.exit_code == 0
        client = seen[0]
        assert client._updates_received >= 3, "stream observer never fired"
        assert client._updates_handled == client._updates_received
        assert result.output == "abc"

    async def test_handlers_are_serialized_in_receive_order(self) -> None:
        """Concurrent notification tasks must not interleave their output.

        The ACP dispatcher spawns one task per notification, so handlers that
        await mid-flight could otherwise emit out of order. Handlers started
        in order must finish in order.
        """
        from flow_atelier.services.executor.harness import _BufferingClient

        order: list[str] = []

        class SlowSink(RecordingSink):
            """A sink that yields inside display, inviting interleaving."""

            async def display(self, text: str) -> None:
                """Record entry, yield, then record exit.

                :param text: the chunk being displayed.
                """
                order.append(f"enter:{text}")
                await asyncio.sleep(0.01)
                order.append(f"exit:{text}")

        client = _BufferingClient(SlowSink(), stream_messages=True)
        chunks = ["a", "b", "c"]
        for _ in chunks:
            client.observe(self._update_event())
        await asyncio.gather(
            *(
                client.session_update(
                    "s",
                    AgentMessageChunk(
                        session_update="agent_message_chunk",
                        content=TextContentBlock(type="text", text=text),
                    ),
                )
                for text in chunks
            )
        )
        assert order == [
            "enter:a", "exit:a", "enter:b", "exit:b", "enter:c", "exit:c",
        ]
        assert client.buffer == chunks

    async def test_returns_immediately_when_nothing_is_outstanding(self) -> None:
        """With no observed updates the wait is a no-op."""
        client = self._client()
        await client.await_pending_updates()
        assert client.buffer == []

    async def test_ignores_outgoing_and_non_update_messages(self) -> None:
        """Only inbound session/update messages count toward the wait."""
        client = self._client()
        client.observe(self._update_event(direction=StreamDirection.OUTGOING))
        client.observe(self._update_event(method="session/prompt"))
        # Neither was counted, so this must not block on a handler.
        await client.await_pending_updates(timeout=0.2)

    async def test_a_failing_handler_still_advances_the_count(self) -> None:
        """A raising handler must not strand the wait until its timeout.

        The ACP dispatcher swallows notification-handler exceptions, so a
        counter that only advanced on success would wait out the bound on
        every failure.
        """
        class BoomSink(RecordingSink):
            """A sink whose display blows up mid-handler."""

            async def display(self, text: str) -> None:
                """Fail the way a broken renderer would.

                :param text: text chunk (unused).
                """
                raise RuntimeError("renderer blew up")

        from flow_atelier.services.executor.harness import _BufferingClient

        client = _BufferingClient(BoomSink(), stream_messages=True)
        client.observe(self._update_event())
        chunk = AgentMessageChunk(
            session_update="agent_message_chunk",
            content=TextContentBlock(type="text", text="x"),
        )
        with contextlib.suppress(RuntimeError):
            await client.session_update("s", chunk)
        await client.await_pending_updates(timeout=0.2)

    async def test_wait_is_bounded_when_a_notification_never_arrives(self) -> None:
        """A lost notification costs the bound, not the whole task timeout."""
        client = self._client()
        client.observe(self._update_event())
        started = asyncio.get_running_loop().time()
        await client.await_pending_updates(timeout=0.1)
        assert asyncio.get_running_loop().time() - started < 1.0


async def test_relative_working_dir_is_resolved_before_session_new(tmp_path) -> None:
    """A relative working_dir must reach the agent as an absolute path.

    ACP agents reject a relative `cwd` at session/new, so passing one through
    fails the task before it starts — which is what `atelier run --path .` or
    a schedule with a relative run_path would do. The fake agent enforces the
    same rule as the real ones.

    :param tmp_path: pytest temp directory fixture.
    """
    import os

    executor = AcpHarnessExecutor(
        launch_cmd=_fake_cmd({"turns": [{"chunks": ["ok"], "stop": "end_turn"}]}),
        sink=RecordingSink(),
    )
    context = FlowContext(
        flow_id="f", store=None, inputs={}, timeout=30, working_dir=Path("."),
    )
    cwd_before = os.getcwd()
    try:
        os.chdir(tmp_path)
        result = await executor.execute(_task("x"), "x", context)
    finally:
        os.chdir(cwd_before)
    assert result.exit_code == 0, result.stderr
    assert "ok" in result.output


def test_is_available_true_when_binary_on_path(monkeypatch) -> None:
    """is_available returns (True, "") when the launch binary resolves."""
    monkeypatch.setattr(
        "flow_atelier.services.executor.harness.shutil.which",
        lambda _binary: "/usr/bin/npx",
    )
    executor = AcpHarnessExecutor(launch_cmd=["npx", "-y", "pkg"])
    assert executor.is_available() == (True, "")


def test_is_available_false_when_binary_missing(monkeypatch) -> None:
    """is_available returns (False, reason) naming the missing binary."""
    monkeypatch.setattr(
        "flow_atelier.services.executor.harness.shutil.which",
        lambda _binary: None,
    )
    executor = AcpHarnessExecutor(launch_cmd=["npx", "-y", "pkg"])
    ok, reason = executor.is_available()
    assert ok is False
    assert "npx" in reason


async def test_nontext_only_turn_notes_empty_success() -> None:
    """A turn whose only content is non-text yields an empty-but-noted success.

    Without the note, a successful turn with no captured text is
    indistinguishable from a genuinely empty one, so downstream steps reading
    its output silently get "".
    """
    from acp.schema import AgentMessageChunk, ImageContentBlock

    from flow_atelier.services.executor.harness import (
        AcpHarnessExecutor,
        _BufferingClient,
    )

    client = _BufferingClient(sink=RecordingSink())
    image = ImageContentBlock(type="image", data="abc", mimeType="image/png")
    await client.session_update(
        "s", AgentMessageChunk(sessionUpdate="agent_message_chunk", content=image)
    )

    result = AcpHarnessExecutor._result_for_turn(client, "end_turn")
    assert result.exit_code == 0
    assert result.output == ""
    assert result.stderr == "agent produced only non-text content"


async def test_text_turn_has_no_nontext_note() -> None:
    """A normal text turn stays clean: success with empty stderr."""
    from acp.schema import AgentMessageChunk, TextContentBlock

    from flow_atelier.services.executor.harness import (
        AcpHarnessExecutor,
        _BufferingClient,
    )

    client = _BufferingClient(sink=RecordingSink())
    text = TextContentBlock(type="text", text="hello")
    await client.session_update(
        "s", AgentMessageChunk(sessionUpdate="agent_message_chunk", content=text)
    )

    result = AcpHarnessExecutor._result_for_turn(client, "end_turn")
    assert result.exit_code == 0
    assert result.output == "hello"
    assert result.stderr == ""


async def test_agent_stderr_is_surfaced_on_launch_failure() -> None:
    """A harness that dies before the handshake reports *why* it died.

    The ACP transport pipes the agent's stderr; without a drain the only
    explanation the user gets is a bare exception type.
    """
    executor = AcpHarnessExecutor(
        launch_cmd=[
            sys.executable,
            "-c",
            "import sys; sys.stderr.write('not authenticated: run `claude login`\\n'); "
            "sys.exit(1)",
        ],
        sink=RecordingSink(),
    )
    result = await executor.execute(_task("hi"), "hi", _ctx(timeout=10))

    assert not result.success
    assert "not authenticated" in result.stderr
    assert "claude login" in result.stderr


async def test_chatty_agent_stderr_is_ring_buffered() -> None:
    """A high-volume agent surfaces its stderr tail without unbounded growth.

    Draining is what keeps the pipe from filling (undrained, an agent past
    the ~64KB buffer blocks on write), and AGENT_STDERR_LINES is what keeps
    the drained output from becoming the log. Here: 40k lines in, a bounded
    tail out.
    """
    executor = AcpHarnessExecutor(
        launch_cmd=[
            sys.executable,
            "-c",
            "import sys; sys.stderr.write('noise\\n' * 40000); sys.exit(1)",
        ],
        sink=RecordingSink(),
    )
    result = await asyncio.wait_for(
        executor.execute(_task("hi"), "hi", _ctx(timeout=15)), timeout=30
    )

    assert "noise" in result.stderr
    # The bare failure line plus at most AGENT_STDERR_LINES of agent output.
    assert len(result.stderr.splitlines()) <= AGENT_STDERR_LINES + 1
