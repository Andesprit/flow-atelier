"""`atelier init` command."""
from __future__ import annotations

import shutil
from pathlib import Path

import yaml

from flow_atelier.cli._shared import console
from flow_atelier.cli.commands.create import starter_conduit_yaml
from flow_atelier.cli.main import app

# Each agent CLI a user may already be logged in to, and the harness that
# drives it. The order is the preference when picking the starter's agents.
AGENT_CLIS = (
    ("claude", "claude-code"),
    ("codex", "codex"),
    ("gemini", "gemini"),
    ("cursor-agent", "cursor"),
    ("opencode", "opencode"),
    ("copilot", "copilot"),
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

MERGE_PROMPT = """\
Two agents looked at the same project. Merge their notes into one short brief:
where they agree, where they disagree, and the single most useful next step.

--- {first} ---
{{{{look_{first_key}.output}}}}

--- {second} ---
{{{{look_{second_key}.output}}}}
"""


class _PromptDumper(yaml.SafeDumper):
    """Write multi-line prompts as block scalars so they stay easy to edit."""


def _block_str(dumper, value):
    style = "|" if "\n" in value else None
    return dumper.represent_scalar("tag:yaml.org,2002:str", value, style=style)


_PromptDumper.add_representer(str, _block_str)


def installed_harnesses() -> list[str]:
    """Return the harnesses whose agent CLI is on PATH, most preferred first.

    :returns: harness names such as ``claude-code``.
    """
    return [harness for cli, harness in AGENT_CLIS if shutil.which(cli)]


def explain_conduit_yaml(harnesses: list[str]) -> str:
    """Render the starter that has agents describe the current project.

    One agent answers alone. Two agents look in parallel and the first
    merges both answers.

    :param harnesses: one or more harness names, most preferred first.
    :returns: YAML text for ``conduit.yaml``.
    """
    first = harnesses[0]
    if len(harnesses) == 1:
        tasks = [{"look": {
            "description": f"{first} describes this project",
            "task": LOOK_PROMPT,
            "tool": f"harness:{first}",
            "depends_on": [],
        }}]
    else:
        second = harnesses[1]
        keys = {h: h.replace("-", "_") for h in (first, second)}
        tasks = [
            {f"look_{keys[h]}": {
                "description": f"{h} describes this project",
                "task": LOOK_PROMPT,
                "tool": f"harness:{h}",
                "depends_on": [],
            }}
            for h in (first, second)
        ] + [{"merge": {
            "description": f"{first} merges both answers",
            "task": MERGE_PROMPT.format(
                first=first, first_key=keys[first],
                second=second, second_key=keys[second],
            ),
            "tool": f"harness:{first}",
            "depends_on": [f"look_{keys[first]}", f"look_{keys[second]}"],
        }}]
    doc = {
        "name": "explain",
        "description": "Ask your AI agents what this project is",
        "tasks": tasks,
    }
    return yaml.dump(doc, Dumper=_PromptDumper, sort_keys=False, width=100)


@app.command(
    "init",
    help=(
        "Scaffold a local .atelier/ directory with a hello-world conduit, plus "
        "an AI starter when an agent CLI such as claude or codex is installed."
    ),
)
def init_cmd() -> None:
    """Scaffold ``.atelier/`` with starter conduits; idempotent."""
    conduits = Path.cwd() / ".atelier" / "conduits"
    hello = conduits / "hello" / "conduit.yaml"
    explain = conduits / "explain" / "conduit.yaml"
    harnesses = installed_harnesses()

    written = []
    if not hello.exists():
        hello.parent.mkdir(parents=True, exist_ok=True)
        hello.write_text(starter_conduit_yaml("hello", "Say hello"))
        written.append(hello)
    if harnesses and not explain.exists():
        explain.parent.mkdir(parents=True, exist_ok=True)
        explain.write_text(explain_conduit_yaml(harnesses[:2]))
        written.append(explain)

    if not written:
        console.print("[yellow]atelier is already set up in this project[/yellow]")
        return
    console.print(f"[green]initialized[/green] {conduits.parent}")
    if explain.exists():
        agents = " and ".join(harnesses[:2])
        console.print(
            f"found {agents}. Have {'them' if len(harnesses) > 1 else 'it'} "
            "explain this project:\n"
            "  [bold]atelier serve[/bold]        # in a second terminal, to watch live\n"
            "  [bold]atelier run explain[/bold]"
        )
    else:
        console.print(
            "try: [bold]atelier run hello --input name=world[/bold]\n"
            "[dim]install Claude Code or Codex, then run atelier init again "
            "for an AI starter[/dim]"
        )
