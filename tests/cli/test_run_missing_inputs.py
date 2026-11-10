"""CLI tests: `atelier run` without a terminal and with required inputs left out.

The rejection must let the caller append the missing flags to the command
they already ran, so nothing they supplied is lost or echoed back.
"""
from __future__ import annotations

import json
import os
import shlex
import sys
from pathlib import Path

import pytest
from typer.testing import CliRunner

import flow_atelier.cli.commands.run as run_mod
from flow_atelier.cli import app
from flow_atelier.core.atelier import Atelier

FAKE_AGENT = Path(__file__).resolve().parents[1] / "fixtures" / "fake_acp_agent.py"

NEEDS = (
    "name: needs\ndescription: d\n"
    "inputs:\n"
    "  document: The document to process\n"
    "  destination:\n"
    '    description: "Where to send [bold]it[/bold] <dest>"\n'
    "  notes: Extra notes\n"
    "  mode:\n    description: Processing mode\n    default: fast\n"
    "tasks:\n  - name: report\n    description: r\n"
    "    task: \"printf '%s|%s|%s|%s' '{{inputs.document}}' '{{inputs.destination}}'"
    " '{{inputs.notes}}' '{{inputs.mode}}'\"\n"
    "    tool: tool:bash\n"
)

# A bash step that can take a worktree and an agent step that can be rebound.
CHOICES = (
    "name: choices\ndescription: d\n"
    "inputs:\n  document: The document\n  destination: Where it goes\n"
    "tasks:\n"
    "  - name: prepare\n    description: p\n"
    '    task: "echo {{inputs.document}} {{inputs.destination}}"\n    tool: tool:bash\n'
    "  - name: review\n    description: r\n"
    '    task: "Review {{prepare.output}}"\n    tool: harness:beta\n'
    "    depends_on: [prepare]\n"
)

SECRET = "SECRET-sk_live_123  two  spaces"


@pytest.fixture
def workdir(tmp_path, monkeypatch):
    """Isolated project with both recipes, fake agents and a non-TTY stdin.

    :param tmp_path: pytest temp directory fixture.
    :param monkeypatch: pytest monkeypatch fixture.
    :returns: the project path.
    """
    for name, body in (("needs", NEEDS), ("choices", CHOICES)):
        cdir = tmp_path / ".atelier" / "conduits" / name
        cdir.mkdir(parents=True)
        (cdir / "conduit.yaml").write_text(body, encoding="utf-8")
    (tmp_path / "global" / "conduits").mkdir(parents=True)
    monkeypatch.chdir(tmp_path)
    for k in list(os.environ):
        if k.startswith("ATELIER_"):
            monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("ATELIER_GLOBAL_ATELIER_DIR", str(tmp_path / "global"))
    monkeypatch.setenv("ATELIER_NO_UPDATE_CHECK", "1")
    monkeypatch.setenv(
        "ATELIER_HARNESSES",
        json.dumps(
            {n: [sys.executable, str(FAKE_AGENT), "--script", "{}"] for n in ("alpha", "beta")}
        ),
    )

    class _Stdin:
        def isatty(self):
            """Report a non-interactive stdin."""
            return False

    class _Sys:
        stdin = _Stdin()

    monkeypatch.setattr(run_mod, "sys", _Sys())
    return tmp_path


def _flows(workdir: Path) -> list[Path]:
    """Return every flow directory the project holds.

    :param workdir: the project path.
    :returns: the flow dirs.
    """
    return sorted((workdir / ".atelier" / "flows").glob("*"))


def _flags(output: str) -> list[str]:
    """Return the printed flags line, split as a shell would.

    :param output: the CLI output.
    :returns: the argv tokens of the flags line.
    """
    line = next(x for x in output.splitlines() if x.strip().startswith("--input"))
    return shlex.split(line)


def test_all_missing_inputs_are_listed_with_descriptions(workdir):
    """Every required key and its description, defaults excluded, no flow."""
    result = CliRunner().invoke(app, ["run", "needs"])
    assert result.exit_code == 2, result.output
    out = result.output
    assert "missing required inputs: document, destination, notes" in out
    assert "document — The document to process" in out
    assert "notes — Extra notes" in out
    # Rich-looking metadata prints literally.
    assert "Where to send [bold]it[/bold] <dest>" in out
    assert "mode" not in out
    assert _flags(out) == [
        "--input", "document=<value>",
        "--input", "destination=<value>",
        "--input", "notes=<value>",
    ]
    assert "loading conduit" not in out
    assert _flows(workdir) == []


def test_partial_input_rejection_prints_flags_only_and_hides_values(workdir, tmp_path):
    """No replacement command, no supplied literal or file content echoed."""
    doc = tmp_path / "my docs" / "input file.txt"
    doc.parent.mkdir()
    doc.write_text("SECRET-FILE-sk_live_999\n", encoding="utf-8")
    result = CliRunner().invoke(
        app,
        ["run", "needs", "--input-file", f"document={doc}", "--input", f"notes={SECRET}"],
    )
    assert result.exit_code == 2, result.output
    out = result.output
    assert "SECRET" not in out
    assert "atelier run" not in out
    assert "add these flags to the command you just ran" in out
    assert _flags(out) == ["--input", "destination=<value>"]
    assert _flows(workdir) == []


def test_unusual_key_is_quoted_as_one_shell_word(workdir):
    """A key with spaces or punctuation stays one token, never shell syntax."""
    cdir = workdir / ".atelier" / "conduits" / "odd"
    cdir.mkdir()
    (cdir / "conduit.yaml").write_text(
        "name: odd\ndescription: d\n"
        "inputs:\n  'a b;$(x)': " + "very long description " * 8 + "\n"
        "tasks:\n  - name: t\n    description: t\n    task: echo hi\n    tool: tool:bash\n",
        encoding="utf-8",
    )
    result = CliRunner().invoke(app, ["run", "odd"], terminal_width=40)
    assert result.exit_code == 2, result.output
    assert _flags(result.output) == ["--input", "a b;$(x)=<value>"]
    # Wrapped across lines at width 40, but nothing is cut off.
    assert " ".join(result.output.split()).count("very long description") == 8


def test_appending_the_filled_flags_completes_the_run(workdir, tmp_path):
    """Follow the hint: original argv plus filled flags runs with every value."""
    content = "línea uno — café\n  second line with  spaces\n"
    doc = tmp_path / "my docs" / "input file.txt"
    doc.parent.mkdir()
    doc.write_text(content, encoding="utf-8")
    original = [
        "run", "needs", "--input-file", f"document={doc}",
        "--input", "mode=slow", "--input", f"notes={SECRET}", "--hide-steps",
    ]
    rejected = CliRunner().invoke(app, original)
    assert rejected.exit_code == 2, rejected.output
    filled = [t.replace("<value>", "local box") for t in _flags(rejected.output)]

    result = CliRunner().invoke(app, original + filled)
    assert result.exit_code == 0, result.output
    (flow,) = _flows(workdir)
    outputs = Atelier().get_outputs(flow.name)
    assert outputs["report"].rstrip("\n") == f"{content}|local box|{SECRET}|slow"


def test_retry_keeps_agent_worktree_and_hide_steps_choices(workdir, monkeypatch):
    """The caller's --agent, --worktree and --hide-steps reach the engine unchanged."""
    original = [
        "run", "choices", "--input", f"document={SECRET}",
        "--agent", "review=alpha", "--worktree", "prepare", "--hide-steps",
    ]
    rejected = CliRunner().invoke(app, original)
    assert rejected.exit_code == 2, rejected.output
    assert "SECRET" not in rejected.output

    calls: list[tuple[dict, dict]] = []

    def fake_run_conduit(self, name, inputs, **kwargs):
        """Record what the CLI hands the engine instead of running it."""
        calls.append((dict(inputs), kwargs))

    monkeypatch.setattr(Atelier, "run_conduit", fake_run_conduit)
    monkeypatch.setattr(run_mod, "drive_flow", lambda run, **_: run())
    filled = [t.replace("<value>", "x") for t in _flags(rejected.output)]
    result = CliRunner().invoke(app, original + filled)
    assert result.exit_code == 0, result.output
    ((inputs, kwargs),) = calls
    assert inputs == {"document": SECRET, "destination": "x"}
    assert kwargs["show_steps"] is False
    assert kwargs["worktrees"] == ["prepare"]
    assert kwargs["agents"] == {"review": "harness:alpha"}
