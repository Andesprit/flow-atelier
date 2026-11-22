"""`atelier init` command."""
from __future__ import annotations

import hashlib
import re
import shutil
from pathlib import Path
from tempfile import NamedTemporaryFile

import yaml
from pydantic import ValidationError

from flow_atelier.cli._shared import console
from flow_atelier.cli.commands._legacy_starters import STOCK_STARTER_SHA256
from flow_atelier.cli.commands.create import starter_conduit_yaml
from flow_atelier.cli.main import app
from flow_atelier.schemas.conduit import Conduit

# Both AI starters need a harness that can enforce read-only permissions.
READ_ONLY_CLIS = (
    ("claude", "claude-code"),
    ("codex", "codex"),
)

LOOK_PROMPT = """\
Look around the current directory and work out what this project is.

Reply in plain language a newcomer can follow:
- what it does, in one sentence
- who it is for
- three things it does well
- the three spots most likely to confuse or break, with file paths

Read only: do not edit files, install anything, or run commands that change state.
"""

DRAFT_PROMPT = """\
Goal: {{inputs.goal}}

Read the code this goal touches, then reply with a short plan and the exact
diff you would apply. If something is unclear, ask before you answer.

Read only: do not edit files, install anything, or run commands that change state.
"""

REVIEW_PROMPT = """\
Another agent proposed this change for the goal "{{inputs.goal}}":

{{draft.output}}

Check it against the code. List what must change, most important first,
or reply LGTM if nothing must.

Read only: do not edit files, install anything, or run commands that change state.
"""

REVISE_PROMPT = """\
The reviewer replied:

{{review.output}}

Revise your proposal. Reply with the final plan and diff.

Read only: do not edit files, install anything, or run commands that change state.
"""

SUPERVISOR_INSTRUCTIONS = """\
Answer the worker's questions from the code and the project's conventions.
Escalate to the human anything about product direction or scope.
"""


class _PromptDumper(yaml.SafeDumper):
    """Write multi-line prompts as block scalars so they stay easy to edit."""


def _block_str(dumper, value):
    style = "|" if "\n" in value else None
    return dumper.represent_scalar("tag:yaml.org,2002:str", value, style=style)


_PromptDumper.add_representer(str, _block_str)


def installed_harnesses() -> list[str]:
    """Return read-only starter harnesses whose CLI is on PATH, most preferred first.

    :returns: harness names such as ``claude-code``.
    """
    return [harness for cli, harness in READ_ONLY_CLIS if shutil.which(cli)]


def explain_conduit_yaml(harnesses: list[str]) -> str:
    """Render the starter that asks the preferred agent to describe the project.

    :param harnesses: one or more harness names, most preferred first.
    :returns: YAML text for ``conduit.yaml``.
    """
    reader = harnesses[0]
    doc = {
        "name": "explain",
        "description": "Ask an agent what this project is",
        "tasks": [{"look": {
            "description": f"{reader} describes this project",
            "task": LOOK_PROMPT,
            "tool": f"harness:{reader}",
            "permissions": "read-only",
            "depends_on": [],
        }}],
    }
    return yaml.dump(doc, Dumper=_PromptDumper, sort_keys=False, width=100)


def pair_conduit_yaml(harnesses: list[str]) -> str:
    """Render the starter where agents answer, review and continue each other.

    The first agent drafts and revises in one ``session``. The second agent
    answers the first agent's questions as supervisor and reviews the draft.
    One agent plays both parts.

    :param harnesses: one or more harness names, most preferred first.
    :returns: YAML text for ``conduit.yaml``.
    """
    worker = f"harness:{harnesses[0]}"
    partner = f"harness:{harnesses[-1]}"
    doc = {
        "name": "pair",
        "description": "One agent drafts a change, another answers its questions and reviews it",
        "inputs": {"goal": {
            "description": "What to change",
            "default": "the smallest change that would most help a new contributor",
        }},
        "interaction": {
            "questions": "hybrid",
            "supervisor": {"tool": partner, "instructions": SUPERVISOR_INSTRUCTIONS},
        },
        "tasks": [
            {"draft": {
                "description": f"{harnesses[0]} proposes a change",
                "task": DRAFT_PROMPT,
                "tool": worker,
                "permissions": "read-only",
                "session": "worker",
                "depends_on": [],
            }},
            {"review": {
                "description": f"{harnesses[-1]} reviews the proposal",
                "task": REVIEW_PROMPT,
                "tool": partner,
                "permissions": "read-only",
                "depends_on": ["draft"],
            }},
            {"revise": {
                "description": f"{harnesses[0]} revises, remembering its draft",
                "task": REVISE_PROMPT,
                "tool": worker,
                "permissions": "read-only",
                "session": "worker",
                "depends_on": ["review"],
            }},
        ],
    }
    return yaml.dump(doc, Dumper=_PromptDumper, sort_keys=False, width=100)


def _upgrade_stock_starter(path: Path, content: bytes) -> bool:
    """Add task permissions to an exact stock file, preserving its agents and layout."""
    normalized = content.replace(b"\r\n", b"\n")
    if (
        path.is_symlink()
        or content not in (normalized, normalized.replace(b"\n", b"\r\n"))
        or hashlib.sha256(normalized).hexdigest() not in STOCK_STARTER_SHA256[path.parent.name]
    ):
        return False
    # In stock YAML the supervisor's tool appears before the tasks section.
    preamble, marker, tasks = content.partition(b"\ntasks:")
    updated = preamble + marker + re.sub(
        rb"(?m)^(    tool: harness:[^\r\n]+)(\r?\n)",
        rb"\1\2    permissions: read-only\2",
        tasks,
    )
    with NamedTemporaryFile(dir=path.parent, delete=False) as stream:
        pending = Path(stream.name)
        try:
            stream.write(updated)
            stream.close()
            if path.is_symlink() or path.read_bytes() != content:
                return False
            shutil.copymode(path, pending)
            pending.replace(path)
        finally:
            stream.close()
            pending.unlink(missing_ok=True)
    return True


def _has_writable_agent_tasks(content: bytes) -> bool:
    try:
        conduit = Conduit.model_validate(yaml.safe_load(content))
    except (ValidationError, yaml.YAMLError):
        return False
    tasks = list(conduit.tasks)
    for task in tasks:
        if task.tool.startswith("harness:") and task.permissions != "read-only":
            return True
        tasks.extend(task.tasks or [])
    return False


@app.command(
    "init",
    help=(
        "Scaffold a local .atelier/ directory with a hello-world conduit, plus "
        "read-only AI starters when Claude Code or Codex is installed."
    ),
)
def init_cmd() -> None:
    """Scaffold ``.atelier/`` with starter conduits; idempotent."""
    project = Path.cwd()
    conduits = project / ".atelier" / "conduits"
    hello = conduits / "hello" / "conduit.yaml"
    harnesses = installed_harnesses()

    written = []
    upgraded = False
    unsafe_starter = False
    if not hello.exists():
        hello.parent.mkdir(parents=True, exist_ok=True)
        hello.write_text(starter_conduit_yaml("hello", "Say hello"))
        written.append(hello)
    for name, render in (("explain", explain_conduit_yaml), ("pair", pair_conduit_yaml)):
        path = conduits / name / "conduit.yaml"
        if path.is_file():
            content = path.read_bytes()
            label = path.relative_to(project).as_posix()
            if _upgrade_stock_starter(path, content):
                console.print(f"[green]upgraded[/green] {label} to read-only")
                upgraded = True
            elif _has_writable_agent_tasks(content):
                unsafe_starter = True
                console.print(
                    f"[yellow]warning[/yellow] {label} has writable agent tasks.\n"
                    "Add or set 'permissions: read-only' on every agent task in this file."
                )
        elif harnesses and not path.exists() and not path.is_symlink():
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("x", encoding="utf-8") as stream:
                stream.write(render(harnesses))
            written.append(path)

    if not written:
        if not upgraded:
            console.print("[yellow]atelier is already set up in this project[/yellow]")
        if harnesses:
            return
    else:
        console.print(f"[green]initialized[/green] {conduits.parent}")
    if harnesses:
        if unsafe_starter:
            return
        console.print(
            f"Have {harnesses[0]} explain this project:\n"
            "  [bold]atelier serve --no-scheduler[/bold]  # in a second terminal, to watch live\n"
            "  [bold]atelier run explain[/bold]\n"
            "then watch agents answer and review each other:\n"
            "  [bold]atelier run pair[/bold]"
        )
    else:
        console.print(
            "try: [bold]atelier run hello --input name=world[/bold]\n"
            "No installed harness can enforce read-only permissions for the AI starters.\n"
            "[dim]install Claude Code or Codex, then run atelier init again[/dim]"
        )
