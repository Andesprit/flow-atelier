"""CLI tests: `atelier run --input-file key=path` loads a document as an input.

Runs through the real CLI, engine and store. The `tool:bash` conduit uses the
real executor with benign content; the document-fidelity checks go through a
harness task whose agent is the scripted ACP fake, which records every prompt
it is handed — so hostile-looking text is captured, never executed.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest
from click import unstyle
from typer.testing import CliRunner

from flow_atelier.cli import app
from flow_atelier.core.atelier import Atelier

FAKE_AGENT = Path(__file__).resolve().parents[1] / "fixtures" / "fake_acp_agent.py"

# Everything a loaded document has to survive on its way to a task: non-ASCII,
# quotes, CRLF, a template that must not be expanded a second time, shell
# metacharacters that must not be executed, and the trailing blank line an
# editor leaves behind.
DOCUMENT = (
    'Héllo "world" — ¿qué tal?\r\n'
    "{{inputs.example}} $(touch pwned) `touch pwned2`\n"
    "\tindented\n\n"
)

BRIEF = (
    "name: brief\ndescription: d\n"
    "inputs:\n"
    "  brief:\n    description: the document to work from\n"
    "  tone:\n    description: how to answer\n    default: warm\n"
    "tasks:\n  - name: think\n    description: t\n"
    '    task: "tone {{inputs.tone}}\\n{{inputs.brief}}"\n'
    "    tool: harness:claude-code\n"
)

HELLO = (
    "name: hello\ndescription: d\n"
    "inputs:\n"
    "  msg:\n    description: what to echo\n    default: hi\n"
    "  tone:\n    description: how to say it\n    default: warm\n"
    "tasks:\n  - name: greet\n    description: g\n"
    '    task: "echo {{inputs.msg}} {{inputs.tone}}"\n'
    "    tool: tool:bash\n"
)


@pytest.fixture
def workdir(tmp_path, monkeypatch):
    """Isolated project/global stores with the scripted agent wired up.

    :param tmp_path: pytest temp directory fixture.
    :param monkeypatch: pytest monkeypatch fixture.
    :returns: the project working directory.
    """
    (tmp_path / ".atelier" / "conduits").mkdir(parents=True)
    (tmp_path / "global" / "conduits").mkdir(parents=True)
    for key in list(os.environ):
        if key.startswith("ATELIER_"):
            monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("ATELIER_GLOBAL_ATELIER_DIR", str(tmp_path / "global"))
    monkeypatch.setenv("ATELIER_NO_UPDATE_CHECK", "1")
    monkeypatch.setenv(
        "ATELIER_CLAUDE_LAUNCH_CMD",
        json.dumps(
            [
                sys.executable,
                str(FAKE_AGENT),
                "--script",
                json.dumps(
                    {
                        "turns": [{"chunks": ["ok"]}],
                        "record_path": str(tmp_path / "prompts.jsonl"),
                    }
                ),
            ]
        ),
    )
    monkeypatch.chdir(tmp_path)
    return tmp_path


def _write(workdir: Path, name: str, body: str) -> None:
    """Write a project conduit.yaml.

    :param workdir: the project working directory.
    :param name: conduit name (also the folder name).
    :param body: full YAML document for the conduit.
    """
    cdir = workdir / ".atelier" / "conduits" / name
    cdir.mkdir(parents=True)
    (cdir / "conduit.yaml").write_text(body)


def _prompts(workdir: Path) -> str:
    """Return every prompt the scripted agent was handed, as one string.

    :param workdir: the project working directory.
    :returns: concatenated prompt text; empty when the agent never ran.
    """
    record = workdir / "prompts.jsonl"
    if not record.exists():
        return ""
    return "\n".join(
        block.get("text", "")
        for line in record.read_text().splitlines()
        for block in json.loads(line)
    )


def _flow_id(output: str) -> str:
    """Extract the flow id `atelier run` printed.

    :param output: captured CLI output.
    :returns: the flow id.
    """
    for line in output.splitlines():
        if "flow_id:" in line:
            return line.split("flow_id:", 1)[1].strip()
    raise AssertionError(f"no flow_id in output:\n{output}")


def _inputs(flow_id: str) -> dict:
    """Return the input map persisted for ``flow_id``.

    :param flow_id: the flow to read.
    :returns: the saved input mapping.
    """
    return Atelier().store.read_input(flow_id)


def test_document_reaches_the_task_and_the_record_unchanged(workdir):
    """The file's exact text is what the task sees and what the run saves."""
    _write(workdir, "brief", BRIEF)
    (workdir / "brief.md").write_bytes(DOCUMENT.encode("utf-8"))

    result = CliRunner().invoke(app, ["run", "brief", "--input-file", "brief=brief.md"])
    assert result.exit_code == 0, result.output

    assert _inputs(_flow_id(result.output))["brief"] == DOCUMENT
    assert DOCUMENT in _prompts(workdir)
    # Neither the `{{inputs.example}}` template nor the command substitutions
    # were acted on: the document is data, all the way through.
    assert "{{inputs.example}}" in _prompts(workdir)
    assert not (workdir / "pwned").exists()
    assert not (workdir / "pwned2").exists()


def test_file_and_literal_inputs_combine(workdir):
    """A document from a file and a short literal setting travel together."""
    _write(workdir, "brief", BRIEF)
    # newline="": `--input-file` hands the file's bytes through untouched, so
    # letting Windows turn the \n into \r\n would assert on the platform, not
    # on the feature.
    (workdir / "spec.md").write_text("ship it\n", newline="")

    result = CliRunner().invoke(
        app,
        ["run", "brief", "--input-file", "brief=spec.md", "--input", "tone=blunt"],
    )
    assert result.exit_code == 0, result.output
    assert _inputs(_flow_id(result.output)) == {"brief": "ship it\n", "tone": "blunt"}
    assert "tone blunt\nship it\n" in _prompts(workdir)


def test_absolute_path_and_awkward_filename(workdir):
    """An absolute path works, and `=` or spaces in the name are not split."""
    _write(workdir, "hello", HELLO)
    odd = workdir / "my brief = draft.txt"
    odd.write_text("Ada")

    result = CliRunner().invoke(app, ["run", "hello", "--input-file", f"msg={odd}"])
    assert result.exit_code == 0, result.output
    assert _inputs(_flow_id(result.output))["msg"] == "Ada"


def test_symlink_to_a_file_is_followed(workdir):
    """An ordinary symlink reads through to its target."""
    _write(workdir, "hello", HELLO)
    (workdir / "real.txt").write_text("Ada")
    try:
        (workdir / "link.txt").symlink_to(workdir / "real.txt")
    except (OSError, NotImplementedError):  # pragma: no cover - platform bound
        pytest.skip("symlinks unavailable here")

    result = CliRunner().invoke(app, ["run", "hello", "--input-file", "msg=link.txt"])
    assert result.exit_code == 0, result.output
    assert _inputs(_flow_id(result.output))["msg"] == "Ada"


def test_empty_file_is_an_explicit_empty_value(workdir):
    """Empty content overrides a default and satisfies a required input."""
    _write(workdir, "hello", HELLO)
    _write(workdir, "brief", BRIEF)
    (workdir / "empty.txt").write_text("")
    runner = CliRunner()

    defaulted = runner.invoke(app, ["run", "hello", "--input-file", "msg=empty.txt"])
    assert defaulted.exit_code == 0, defaulted.output
    assert _inputs(_flow_id(defaulted.output))["msg"] == ""

    # `brief` has no default, so this also proves the CLI saw the key as
    # supplied rather than prompting or failing for a missing input.
    required = runner.invoke(app, ["run", "brief", "--input-file", "brief=empty.txt"])
    assert required.exit_code == 0, required.output
    assert _inputs(_flow_id(required.output))["brief"] == ""


def test_at_prefixed_literal_is_still_a_literal(workdir):
    """`--input k=@file` keeps its old meaning: the literal string `@file`."""
    _write(workdir, "hello", HELLO)
    (workdir / "brief.txt").write_text("Ada")

    result = CliRunner().invoke(app, ["run", "hello", "--input", "msg=@brief.txt"])
    assert result.exit_code == 0, result.output
    assert _inputs(_flow_id(result.output))["msg"] == "@brief.txt"


@pytest.mark.parametrize(
    ("args", "expected"),
    [
        (["--input-file", "noequals"], "expects key=path"),
        (["--input-file", "=brief.txt"], "empty key"),
        (["--input-file", "msg="], "empty path"),
        (["--input-file", "msg=-"], "not '-'"),
        (["--input-file", "msg=missing.txt"], "no such file"),
        (["--input-file", "msg=adir"], "not a regular file"),
        (["--input-file", "msg=binary.bin"], "not valid UTF-8"),
        (["--input-file", "msg=ok.txt", "--input-file", "msg=ok.txt"], "duplicate key"),
        (["--input-file", "msg=ok.txt", "--input", "msg=x"], "already set by --input"),
        (["--input", "msg=x", "--input-file", "msg=ok.txt"], "already set by --input"),
    ],
)
def test_bad_usage_fails_before_anything_runs(workdir, args, expected):
    """Every malformed or unreadable case exits 2 and records no flow."""
    _write(workdir, "hello", HELLO)
    (workdir / "ok.txt").write_text("Ada")
    (workdir / "adir").mkdir()
    (workdir / "binary.bin").write_bytes(b"\xff\xfe\x00nope")

    result = CliRunner().invoke(app, ["run", "hello", *args])
    assert result.exit_code == 2, result.output
    assert expected in unstyle(result.output)
    assert "Ada" not in result.output
    assert "loading conduit" not in result.output
    assert Atelier().list_flows() == []


@pytest.mark.skipif(
    os.name == "nt" or (hasattr(os, "geteuid") and os.geteuid() == 0),
    reason="needs POSIX file modes; chmod(0) does not deny reads on Windows",
)
def test_unreadable_file_reports_why(workdir):
    """A file the user cannot read fails with the OS reason, not a traceback."""
    _write(workdir, "hello", HELLO)
    secret = workdir / "secret.txt"
    secret.write_text("Ada")
    secret.chmod(0o000)

    result = CliRunner().invoke(app, ["run", "hello", "--input-file", "msg=secret.txt"])
    secret.chmod(0o600)
    assert result.exit_code == 2, result.output
    assert "cannot read secret.txt" in result.output
    assert Atelier().list_flows() == []


def test_unknown_file_input_key_is_rejected_with_a_suggestion(workdir):
    """A mistyped `--input-file` key gets the same typo help as `--input`."""
    _write(workdir, "hello", HELLO)
    (workdir / "ok.txt").write_text("Ada")

    result = CliRunner().invoke(app, ["run", "hello", "--input-file", "mgs=ok.txt"])
    assert result.exit_code == 1, result.output
    assert "unknown input: mgs" in result.output
    assert "did you mean msg?" in result.output
    assert Atelier().list_flows() == []


def test_resume_refuses_the_flag_before_touching_anything(workdir):
    """`--resume` plus `--input-file` fails without resolving or reading."""
    result = CliRunner().invoke(
        app, ["run", "--resume", "nosuchflow", "--input-file", "msg=missing.txt"]
    )
    assert result.exit_code == 1, result.output
    assert "--resume and --input-file are mutually exclusive" in result.output
    # Neither the unknown flow nor the unreadable path was reached.
    assert "unknown flow" not in result.output
    assert "no such file" not in result.output


def test_again_replays_the_saved_text_after_the_file_is_gone(workdir):
    """A rerun uses the text the run recorded, not the file it came from."""
    _write(workdir, "hello", HELLO)
    (workdir / "msg.txt").write_text("Ada")
    runner = CliRunner()

    first = runner.invoke(
        app, ["run", "hello", "--input-file", "msg=msg.txt", "--input", "tone=blunt"]
    )
    assert first.exit_code == 0, first.output
    original = _flow_id(first.output)
    (workdir / "msg.txt").unlink()

    again = runner.invoke(app, ["run", "--again", original])
    assert again.exit_code == 0, again.output
    assert _inputs(_flow_id(again.output)) == {"msg": "Ada", "tone": "blunt"}


def test_again_with_a_new_file_changes_only_that_key(workdir):
    """A fresh `--input-file` override leaves the other saved inputs alone."""
    _write(workdir, "hello", HELLO)
    (workdir / "msg.txt").write_text("Ada")
    (workdir / "next.txt").write_text("Grace")
    runner = CliRunner()

    first = runner.invoke(
        app, ["run", "hello", "--input-file", "msg=msg.txt", "--input", "tone=blunt"]
    )
    original = _flow_id(first.output)

    again = runner.invoke(
        app, ["run", "--again", original, "--input-file", "msg=next.txt"]
    )
    assert again.exit_code == 0, again.output
    assert _inputs(_flow_id(again.output)) == {"msg": "Grace", "tone": "blunt"}
    assert _inputs(original) == {"msg": "Ada", "tone": "blunt"}


def test_again_resolves_the_path_from_the_invocation_directory(workdir, tmp_path):
    """The file comes from where the command was typed, not the run directory."""
    _write(workdir, "hello", HELLO)
    (workdir / "msg.txt").write_text("Ada")
    runner = CliRunner()

    first = runner.invoke(app, ["run", "hello", "--input-file", "msg=msg.txt"])
    original = _flow_id(first.output)

    # Pin the run directory to the project while the user stands elsewhere,
    # the way a flow started by the API or a schedule records one.
    store = Atelier().store
    progress = store.read_progress(original)
    progress.run_path = str(workdir)
    store.write_progress(original, progress)

    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "msg.txt").write_text("Grace")
    os.chdir(elsewhere)
    try:
        again = runner.invoke(
            app,
            ["run", "--again", original, "--input-file", "msg=msg.txt"],
            env={"ATELIER_ATELIER_DIR": str(workdir / ".atelier")},
        )
    finally:
        os.chdir(workdir)
    assert again.exit_code == 0, again.output
    assert _inputs(_flow_id(again.output))["msg"] == "Grace"


def test_readme_walkthrough(workdir):
    """The documented four commands work end to end with the real executor."""
    runner = CliRunner()
    assert runner.invoke(app, ["init"]).exit_code == 0
    (workdir / "name.txt").write_text("Ada")

    run = runner.invoke(app, ["run", "hello", "--input-file", "name=name.txt"])
    assert run.exit_code == 0, run.output

    outputs = runner.invoke(app, ["outputs", "latest"])
    assert outputs.exit_code == 0, outputs.output
    assert "hello Ada" in outputs.output
