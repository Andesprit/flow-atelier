"""Tests for PromptSink protocol and TerminalPromptSink."""
from __future__ import annotations

import builtins
import io

import pytest

from flow_atelier.services.executor.prompt_sink import (
    PromptSink,
    TerminalPromptSink,
)


async def _coro(value: str) -> str:
    """Trivial awaitable returning *value* — stands in for async helpers.

    :param value: value to return from the coroutine.
    """
    return value


class TestTerminalPromptSink:
    async def test_display_writes_to_stream(self) -> None:
        """Verify display() writes text verbatim to the configured stream."""
        stream = io.StringIO()
        sink = TerminalPromptSink(out=stream)
        await sink.display("hello world")
        assert stream.getvalue() == "hello world"

    async def test_display_passthrough_no_auto_newline(self) -> None:
        """Streaming chunks must not be line-wrapped.

        Agent output arrives as token-sized fragments; the sink must
        concatenate them verbatim so the terminal renders agent
        formatting exactly as emitted.
        """
        stream = io.StringIO()
        sink = TerminalPromptSink(out=stream)
        for chunk in ("What", " is", " your", " name", "?"):
            await sink.display(chunk)
        assert stream.getvalue() == "What is your name?"

    async def test_display_preserves_agent_newlines(self) -> None:
        """Verify display() preserves newline characters from the agent."""
        stream = io.StringIO()
        sink = TerminalPromptSink(out=stream)
        await sink.display("line one\n")
        await sink.display("line two\n")
        assert stream.getvalue() == "line one\nline two\n"

    async def test_request_input_reads_stdin_line(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Verify request_input reads a line from stdin.

        :param monkeypatch: pytest monkeypatch fixture.
        """
        stream = io.StringIO()
        sink = TerminalPromptSink(out=stream)
        monkeypatch.setattr(builtins, "input", lambda _prompt="": "175 cm")
        answer = await sink.request_input("height?")
        assert answer == "175 cm"
        assert "height?" in stream.getvalue()

    async def test_request_input_inserts_visual_separator(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """User must see a clear break between streamed agent output and
        the turn-over prompt; otherwise the prompt gets glued onto the last
        agent token. The styled "you" rule lives on its own line after a
        compensating newline.

        :param monkeypatch: pytest monkeypatch fixture.
        """
        stream = io.StringIO()
        sink = TerminalPromptSink(out=stream)
        await sink.display("agent finished mid-sentence")
        monkeypatch.setattr(builtins, "input", lambda _prompt="": "ok")
        await sink.request_input("reply?")
        rendered = stream.getvalue()
        # The streamed agent text gets its own newline before the rule.
        assert "agent finished mid-sentence\n" in rendered
        # The styled "you" rule renders, followed by the prompt label.
        assert "you" in rendered
        assert "reply?" in rendered
        # The rule is not glued to the streamed text — its first glyph
        # (the 👤 emoji) is on a new line.
        before_rule = rendered.split("👤")[0]
        assert before_rule.endswith("\n")

    async def test_request_input_echoes_piped_reply(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Piped stdin doesn't echo keystrokes — the sink must echo the
        consumed line back so scripted transcripts read like a real
        terminal session.

        :param monkeypatch: pytest monkeypatch fixture.
        """
        import sys as _sys
        stream = io.StringIO()
        sink = TerminalPromptSink(out=stream)
        monkeypatch.setattr(builtins, "input", lambda _prompt="": "scripted reply")
        monkeypatch.setattr(_sys.stdin, "isatty", lambda: False)
        await sink.request_input("reply?")
        rendered = stream.getvalue()
        assert "scripted reply" in rendered
        assert "you" in rendered  # styled rule label

    async def test_request_input_does_not_double_echo_on_tty(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When the user is actually typing, the terminal already echoes
        keystrokes — we must not write the answer back ourselves.

        :param monkeypatch: pytest monkeypatch fixture.
        """
        import sys as _sys

        import flow_atelier.cli.rendering.multiline_input as _ml

        stream = io.StringIO()
        sink = TerminalPromptSink(out=stream)
        monkeypatch.setattr(
            _ml, "multiline_input", lambda *a, **kw: _coro("typed live")
        )
        monkeypatch.setattr(_sys.stdin, "isatty", lambda: True)
        await sink.request_input("reply?")
        # The TTY branch must NOT print the answer; only the prompt label.
        assert "typed live" not in stream.getvalue()

    async def test_request_input_hint_says_enter_submits(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The hint above an agent's question names the keys that really work.

        Enter submits and Alt+Enter adds a newline; a hint that says
        Alt+Enter submits leaves a person, or an agent driving the PTY, with
        a newline instead of a sent answer.

        :param monkeypatch: pytest monkeypatch fixture.
        """
        import sys as _sys

        import flow_atelier.cli.rendering.multiline_input as _ml

        hints: list[str] = []

        def fake_multiline_input(prompt: str = "", hint: str = ""):
            """Record the hint and answer at once.

            :param prompt: cursor text (ignored).
            :param hint: the hint shown above the prompt.
            """
            hints.append(hint)
            return _coro("ok")

        monkeypatch.setattr(_ml, "multiline_input", fake_multiline_input)
        monkeypatch.setattr(_sys.stdin, "isatty", lambda: True)
        await TerminalPromptSink(out=io.StringIO()).request_input("reply?")
        assert hints == [_ml.SUBMIT_HINT]
        assert _ml.SUBMIT_HINT.startswith("Enter to submit")

    async def test_start_agent_turn_renders_styled_rule(self) -> None:
        """The agent-turn marker must be a horizontal rule with the
        'agent' label so the user can see when the agent starts speaking."""
        stream = io.StringIO()
        sink = TerminalPromptSink(out=stream)
        await sink.start_agent_turn()
        rendered = stream.getvalue()
        assert "agent" in rendered
        # A rule renders box-drawing horizontal characters.
        assert "─" in rendered

    async def test_start_agent_turn_separates_from_previous_stream(self) -> None:
        """A turn rule must start on its own line, not glued to the
        previous chunk of streamed agent text."""
        stream = io.StringIO()
        sink = TerminalPromptSink(out=stream)
        await sink.display("trailing token without newline")
        await sink.start_agent_turn()
        rendered = stream.getvalue()
        assert "trailing token without newline\n" in rendered

    async def test_request_input_renders_markup_bearing_reply_literally(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A piped reply containing markup brackets must echo literally
        and not raise ``MarkupError``.

        :param monkeypatch: pytest monkeypatch fixture.
        """
        import sys as _sys

        stream = io.StringIO()
        sink = TerminalPromptSink(out=stream)
        monkeypatch.setattr(builtins, "input", lambda _prompt="": "answer [x]")
        monkeypatch.setattr(_sys.stdin, "isatty", lambda: False)
        await sink.request_input("pick [a] or [b]?")
        rendered = stream.getvalue()
        assert "answer [x]" in rendered
        assert "pick [a] or [b]?" in rendered


class TestPromptSinkProtocol:
    def test_terminal_sink_satisfies_protocol(self) -> None:
        """Verify TerminalPromptSink exposes the PromptSink protocol surface."""
        sink: PromptSink = TerminalPromptSink()
        assert hasattr(sink, "display")
        assert hasattr(sink, "request_input")
