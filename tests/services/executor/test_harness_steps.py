"""Tests for intermediate step capture in _BufferingClient."""
from __future__ import annotations

from acp.schema import (
    AgentMessageChunk,
    AgentThoughtChunk,
    Cost,
    TextContentBlock,
    ToolCallLocation,
    ToolCallProgress,
    ToolCallStart,
    UsageUpdate,
)

from flow_atelier.schemas.log import IntermediateStep, StepKind
from flow_atelier.services.executor.harness import _BufferingClient


class RecordingSink:
    """Minimal sink that records display_step calls."""

    def __init__(self) -> None:
        """Initialize the recording sink with empty logs."""
        self.display_log: list[str] = []
        self.step_log: list[IntermediateStep] = []

    async def display(self, text: str) -> None:
        """Record displayed text.

        :param text: chunk of agent output forwarded to the sink.
        """
        self.display_log.append(text)

    async def start_agent_turn(self, label: str = "agent") -> None:
        """No-op stub for the agent-turn marker.

        :param label: turn label (ignored).
        """
        del label

    async def request_input(self, prompt: str) -> str:
        """Refuse input by raising EOFError.

        :param prompt: prompt text (ignored).
        """
        del prompt
        raise EOFError

    async def display_step(self, step: IntermediateStep) -> None:
        """Record an intermediate step.

        :param step: the intermediate step to capture.
        """
        self.step_log.append(step)


class TestBufferingClientSteps:
    async def test_thinking_chunk_captured(self) -> None:
        """Verify a single thought chunk yields one merged thinking step
        once the driver flushes at the turn boundary."""
        sink = RecordingSink()
        client = _BufferingClient(sink, stream_steps=False)
        update = AgentThoughtChunk(
            sessionUpdate="agent_thought_chunk",
            content=TextContentBlock(type="text", text="Let me think about this"),
        )
        await client.session_update("s1", update)
        # Short chunks sit pending until flush — emulate end-of-turn.
        await client.flush_pending()
        assert len(client.steps) == 1
        assert client.steps[0].kind == StepKind.thinking
        assert client.steps[0].text == "Let me think about this"

    async def test_thinking_chunks_grouped_until_flush(self) -> None:
        """Word-per-chunk streams (e.g. opencode) coalesce into one step
        per turn boundary instead of one step per token."""
        sink = RecordingSink()
        client = _BufferingClient(sink, stream_steps=True)
        tokens = ["Let", " me", " think", " about", " this", "."]
        for tok in tokens:
            await client.session_update(
                "s1",
                AgentThoughtChunk(
                    sessionUpdate="agent_thought_chunk",
                    content=TextContentBlock(type="text", text=tok),
                ),
            )
        # Below the length threshold and no newlines — nothing emitted yet.
        assert client.steps == []
        assert sink.step_log == []
        await client.flush_pending()
        assert len(client.steps) == 1
        assert client.steps[0].text == "Let me think about this."
        assert len(sink.step_log) == 1

    async def test_usage_update_captures_cost(self) -> None:
        """A UsageUpdate notification stores the reported cumulative cost."""
        sink = RecordingSink()
        client = _BufferingClient(sink)
        assert client.cost is None
        await client.session_update(
            "s1",
            UsageUpdate(
                sessionUpdate="usage_update",
                cost=Cost(amount=0.42, currency="USD"),
                size=0,
                used=0,
            ),
        )
        assert client.cost == 0.42

    async def test_usage_update_without_cost_is_ignored(self) -> None:
        """A UsageUpdate carrying no cost leaves client.cost untouched."""
        sink = RecordingSink()
        client = _BufferingClient(sink)
        await client.session_update(
            "s1",
            UsageUpdate(sessionUpdate="usage_update", cost=None, size=0, used=0),
        )
        assert client.cost is None

    async def test_thinking_flushes_on_length(self) -> None:
        """Once the buffered thought text reaches the threshold the client
        emits a step without waiting for a turn boundary; remaining text
        continues to accumulate in a fresh group."""
        sink = RecordingSink()
        client = _BufferingClient(sink, stream_steps=False)
        chunk = "x" * 50
        for _ in range(5):  # 250 chars total > 200 threshold
            await client.session_update(
                "s1",
                AgentThoughtChunk(
                    sessionUpdate="agent_thought_chunk",
                    content=TextContentBlock(type="text", text=chunk),
                ),
            )
        # First flush fires when the buffer reaches 200; the trailing 50
        # chars stay pending until the next flush.
        assert len(client.steps) == 1
        assert client.steps[0].text == "x" * 200
        await client.flush_pending()
        assert len(client.steps) == 2
        assert client.steps[1].text == "x" * 50

    async def test_thinking_flushes_on_newline(self) -> None:
        """A chunk containing a newline closes the current thought group."""
        sink = RecordingSink()
        client = _BufferingClient(sink, stream_steps=False)
        await client.session_update(
            "s1",
            AgentThoughtChunk(
                sessionUpdate="agent_thought_chunk",
                content=TextContentBlock(type="text", text="first thought"),
            ),
        )
        await client.session_update(
            "s1",
            AgentThoughtChunk(
                sessionUpdate="agent_thought_chunk",
                content=TextContentBlock(type="text", text="\n"),
            ),
        )
        await client.session_update(
            "s1",
            AgentThoughtChunk(
                sessionUpdate="agent_thought_chunk",
                content=TextContentBlock(type="text", text="second thought"),
            ),
        )
        await client.flush_pending()
        assert [s.text for s in client.steps] == [
            "first thought",
            "second thought",
        ]

    async def test_message_chunk_flushes_pending_thinking(self) -> None:
        """When prose arrives, any partial thought group is emitted first
        so the recorded order matches what the agent actually said."""
        sink = RecordingSink()
        client = _BufferingClient(
            sink, stream_messages=False, stream_steps=False
        )
        await client.session_update(
            "s1",
            AgentThoughtChunk(
                sessionUpdate="agent_thought_chunk",
                content=TextContentBlock(type="text", text="brief plan"),
            ),
        )
        await client.session_update(
            "s1",
            AgentMessageChunk(
                sessionUpdate="agent_message_chunk",
                content=TextContentBlock(type="text", text="answer"),
            ),
        )
        assert len(client.steps) == 1
        assert client.steps[0].kind == StepKind.thinking
        assert client.steps[0].text == "brief plan"
        assert "".join(client.buffer) == "answer"

    async def test_tool_call_start_captured(self) -> None:
        """Verify ToolCallStart updates become tool_call steps with metadata."""
        sink = RecordingSink()
        client = _BufferingClient(sink, stream_steps=False)
        update = ToolCallStart(
            sessionUpdate="tool_call",
            tool_call_id="tc-1",
            title="Read file",
            kind="read",
            status="pending",
            locations=[ToolCallLocation(path="/src/main.py")],
            raw_input={"path": "/src/main.py"},
        )
        await client.session_update("s1", update)
        assert len(client.steps) == 1
        step = client.steps[0]
        assert step.kind == StepKind.tool_call
        assert step.tool_call_id == "tc-1"
        assert step.tool_name == "Read file"
        assert step.tool_kind == "read"
        assert step.tool_status == "pending"
        assert step.locations == ["/src/main.py"]

    async def test_tool_call_progress_completed_captured(self) -> None:
        """Verify completed ToolCallProgress updates become tool_result steps."""
        sink = RecordingSink()
        client = _BufferingClient(sink, stream_steps=False)
        update = ToolCallProgress(
            sessionUpdate="tool_call_update",
            tool_call_id="tc-1",
            status="completed",
            raw_output={"content": "file contents"},
        )
        await client.session_update("s1", update)
        assert len(client.steps) == 1
        step = client.steps[0]
        assert step.kind == StepKind.tool_result
        assert step.tool_call_id == "tc-1"
        assert step.tool_status == "completed"

    async def test_tool_call_progress_failed_captured(self) -> None:
        """Verify failed ToolCallProgress updates become tool_result steps."""
        sink = RecordingSink()
        client = _BufferingClient(sink, stream_steps=False)
        update = ToolCallProgress(
            sessionUpdate="tool_call_update",
            tool_call_id="tc-2",
            status="failed",
        )
        await client.session_update("s1", update)
        assert len(client.steps) == 1
        assert client.steps[0].tool_status == "failed"

    async def test_tool_call_progress_pending_ignored(self) -> None:
        """Only completed/failed ToolCallProgress should create a step."""
        sink = RecordingSink()
        client = _BufferingClient(sink, stream_steps=False)
        update = ToolCallProgress(
            sessionUpdate="tool_call_update",
            tool_call_id="tc-1",
            status="in_progress",
        )
        await client.session_update("s1", update)
        assert len(client.steps) == 0

    async def test_stream_steps_calls_display_step(self) -> None:
        """Verify stream_steps=True forwards captured steps to the sink."""
        sink = RecordingSink()
        client = _BufferingClient(sink, stream_steps=True)
        update = AgentThoughtChunk(
            sessionUpdate="agent_thought_chunk",
            content=TextContentBlock(type="text", text="thinking"),
        )
        await client.session_update("s1", update)
        await client.flush_pending()
        assert len(sink.step_log) == 1
        assert sink.step_log[0].kind == StepKind.thinking

    async def test_no_stream_steps_skips_display_step(self) -> None:
        """Verify stream_steps=False captures steps without sending them live."""
        sink = RecordingSink()
        client = _BufferingClient(sink, stream_steps=False)
        update = AgentThoughtChunk(
            sessionUpdate="agent_thought_chunk",
            content=TextContentBlock(type="text", text="thinking"),
        )
        await client.session_update("s1", update)
        await client.flush_pending()
        # Steps captured, but not sent to sink
        assert len(client.steps) == 1
        assert len(sink.step_log) == 0

    async def test_stream_steps_without_messages_streams_all_step_kinds(
        self,
    ) -> None:
        """``stream_steps=True, stream_messages=False`` is the non-interactive
        default: tool/thinking activity surfaces live but the agent's prose
        is still buffered for the post-task panel."""
        sink = RecordingSink()
        client = _BufferingClient(
            sink, stream_messages=False, stream_steps=True
        )
        await client.session_update(
            "s1",
            AgentThoughtChunk(
                sessionUpdate="agent_thought_chunk",
                content=TextContentBlock(type="text", text="planning"),
            ),
        )
        await client.session_update(
            "s1",
            ToolCallStart(
                sessionUpdate="tool_call",
                tool_call_id="tc-1",
                title="Read",
                kind="read",
                status="pending",
            ),
        )
        await client.session_update(
            "s1",
            ToolCallProgress(
                sessionUpdate="tool_call_update",
                tool_call_id="tc-1",
                status="completed",
            ),
        )
        kinds = [s.kind for s in sink.step_log]
        assert kinds == [
            StepKind.thinking,
            StepKind.tool_call,
            StepKind.tool_result,
        ]
        assert sink.display_log == []

    async def test_stream_steps_does_not_stream_messages(self) -> None:
        """Steps streaming must not pull message chunks along with it —
        they are independent gates."""
        sink = RecordingSink()
        client = _BufferingClient(
            sink, stream_messages=False, stream_steps=True
        )
        await client.session_update(
            "s1",
            AgentMessageChunk(
                sessionUpdate="agent_message_chunk",
                content=TextContentBlock(type="text", text="hello world"),
            ),
        )
        # Buffered for the post-task panel...
        assert "".join(client.buffer) == "hello world"
        # ...but never streamed live.
        assert sink.display_log == []

    async def test_tool_input_truncated(self) -> None:
        """Verify oversized tool_input payloads are truncated."""
        sink = RecordingSink()
        client = _BufferingClient(sink, stream_steps=False)
        update = ToolCallStart(
            sessionUpdate="tool_call",
            tool_call_id="tc-1",
            title="Write",
            raw_input="x" * 1000,
        )
        await client.session_update("s1", update)
        assert len(client.steps[0].tool_input) <= 500

    async def test_tool_output_truncated(self) -> None:
        """Verify oversized tool_output payloads are truncated."""
        sink = RecordingSink()
        client = _BufferingClient(sink, stream_steps=False)
        update = ToolCallProgress(
            sessionUpdate="tool_call_update",
            tool_call_id="tc-1",
            status="completed",
            raw_output="y" * 1000,
        )
        await client.session_update("s1", update)
        assert len(client.steps[0].tool_output) <= 500


async def test_tool_call_waits_for_the_input_claude_sends_next() -> None:
    """claude-code-acp opens each call empty; the step carries the command that follows."""
    client = _BufferingClient(RecordingSink(), stream_steps=False)
    await client.session_update(
        "s1",
        ToolCallStart(
            sessionUpdate="tool_call", tool_call_id="tc-1", title="Terminal",
            kind="execute", status="pending", raw_input={},
        ),
    )
    assert client.steps == []

    await client.session_update(
        "s1",
        ToolCallProgress(
            sessionUpdate="tool_call_update", tool_call_id="tc-1",
            title="cat request.json", raw_input={"command": "cat request.json"},
        ),
    )
    await client.session_update(
        "s1",
        ToolCallProgress(sessionUpdate="tool_call_update", tool_call_id="tc-1", status="completed"),
    )

    call, result = client.steps
    assert (call.kind, call.tool_name, call.tool_input) == (
        StepKind.tool_call, "Terminal", '{"command": "cat request.json"}'
    )
    assert result.kind == StepKind.tool_result


async def test_tool_call_already_running_is_never_held() -> None:
    """A call that starts in progress shows at once, input or not (codex edits)."""
    client = _BufferingClient(RecordingSink(), stream_steps=False)
    await client.session_update(
        "s1",
        ToolCallStart(
            sessionUpdate="tool_call", tool_call_id="tc-1", title="Editing files",
            kind="edit", status="in_progress",
        ),
    )
    assert [s.tool_name for s in client.steps] == ["Editing files"]


async def test_held_tool_call_is_kept_when_the_turn_ends() -> None:
    """A call whose input never arrives still shows once the turn is flushed."""
    client = _BufferingClient(RecordingSink(), stream_steps=False)
    await client.session_update(
        "s1",
        ToolCallStart(
            sessionUpdate="tool_call", tool_call_id="tc-1", title="Terminal",
            kind="execute", status="pending",
        ),
    )
    await client.flush_pending()
    assert [s.tool_name for s in client.steps] == ["Terminal"]
