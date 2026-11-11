"""Multi-line terminal input using prompt_toolkit.

Enter submits; Alt+Enter (Escape then Enter) inserts a newline.
Falls back to ``builtins.input()`` when stdin is not a TTY.
"""
from __future__ import annotations

import asyncio
import builtins
import sys

from prompt_toolkit import PromptSession
from prompt_toolkit.key_binding import KeyBindings

# The hint every caller shows above the prompt. It names the bindings below;
# keep the two in step.
SUBMIT_HINT = "Enter to submit · Alt+Enter for newline"


def _make_key_bindings() -> KeyBindings:
    """Enter submits; Alt+Enter inserts a newline."""
    kb = KeyBindings()

    @kb.add("enter")
    def _submit(event):
        """Submit the current buffer when Enter is pressed.

        :param event: prompt_toolkit key event whose buffer is submitted.
        """
        event.current_buffer.validate_and_handle()

    @kb.add("escape", "enter")
    def _newline(event):
        """Insert a newline when Alt+Enter (Escape then Enter) is pressed.

        :param event: prompt_toolkit key event whose buffer gets the newline.
        """
        event.current_buffer.insert_text("\n")

    return kb


async def multiline_input(prompt: str = "› ", hint: str = "") -> str:
    """Read user input — multi-line on a TTY, single-line otherwise.

    :param prompt: text shown before the cursor.
    :param hint: optional dim hint printed above the prompt on a TTY.
    """
    if not sys.stdin.isatty():
        return await asyncio.to_thread(builtins.input, prompt)

    if hint:
        print(f"\033[2m({hint})\033[0m", flush=True)

    session = PromptSession(multiline=True, key_bindings=_make_key_bindings())
    return await asyncio.to_thread(session.prompt, prompt)


def multiline_input_sync(prompt: str = "› ", hint: str = "") -> str:
    """Synchronous variant for use before ``asyncio.run()``.

    :param prompt: text shown before the cursor.
    :param hint: optional dim hint printed above the prompt on a TTY.
    """
    if not sys.stdin.isatty():
        return builtins.input(prompt)

    if hint:
        print(f"\033[2m({hint})\033[0m", flush=True)

    session = PromptSession(multiline=True, key_bindings=_make_key_bindings())
    return session.prompt(prompt)
