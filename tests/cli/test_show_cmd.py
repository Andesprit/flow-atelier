"""CLI tests for `atelier show` — read a conduit without running it."""
from __future__ import annotations

import json
import os

import pytest
from typer.testing import CliRunner

from flow_atelier.cli import app
from flow_atelier.core.atelier import Atelier
from flow_atelier.schemas.conduit import Conduit

# Everything a raw source view must survive: a comment, a non-ASCII word, text
# Rich would read as markup, a literal template, and a multiline prompt.
RICH_SOURCE = """\
# authored by hand — keep this comment
name: fancy
description: "Résumé [bold]review[/bold]"
inputs:
  name: Who to greet
tasks:
  - greet:
      description: greet someone
      task: |
        Greet {{inputs.name}} warmly.

        Mention [link] and {{ outputs.nothing }}.
      tool: tool:bash
"""

# Declared required, declared defaulted, declared empty-string default, a key
# only referenced in a task body, and one forwarded to a nested conduit.
INPUTS_SOURCE = """\
name: inputs
description: every input shape
inputs:
  required_key:
    description: no default, so it must be supplied
  defaulted_key:
    description: has a default
    default: fallback
  empty_default_key:
    description: an empty string is still a default
    default: ""
tasks:
  - first:
      description: references an undeclared key
      task: "echo {{inputs.referenced_only}}"
      tool: tool:bash
      repeat: 3
      while: output.match(again)
  - second:
      description: forwards an input to a nested conduit
      task: hello
      tool: tool:conduit
      depends_on: [first]
      inputs:
        name: "{{inputs.forwarded_key}}"
"""


@pytest.fixture
def workdir(tmp_path, monkeypatch):
    """Isolated cwd with empty project and global conduit stores.

    :param tmp_path: pytest temp directory fixture.
    :param monkeypatch: pytest monkeypatch fixture.
    """
    (tmp_path / ".atelier" / "conduits").mkdir(parents=True)
    global_dir = tmp_path / "global"
    (global_dir / "conduits").mkdir(parents=True)
    monkeypatch.chdir(tmp_path)
    for k in list(os.environ):
        if k.startswith("ATELIER_"):
            monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("ATELIER_GLOBAL_ATELIER_DIR", str(global_dir))
    monkeypatch.setenv("ATELIER_NO_UPDATE_CHECK", "1")
    return tmp_path


def _write(workdir, name: str, body: str, *, source: str = "project"):
    """Write ``body`` as conduit ``name`` in the project or global store.

    :param workdir: working directory path.
    :param name: conduit name (also the folder name).
    :param body: full YAML document to write.
    :param source: ``project`` or ``global``.
    :returns: the path written.
    """
    root = workdir / ".atelier" if source == "project" else workdir / "global"
    cdir = root / "conduits" / name
    cdir.mkdir(parents=True, exist_ok=True)
    path = cdir / "conduit.yaml"
    path.write_text(body, encoding="utf-8")
    return path


def _flow_count(workdir) -> int:
    """Count flow folders recorded under the project store.

    :param workdir: working directory path.
    :returns: number of top-level flow directories.
    """
    flows = workdir / ".atelier" / "flows"
    return len(list(flows.iterdir())) if flows.is_dir() else 0


def test_default_mode_reproduces_the_source_verbatim(workdir):
    """Comments, Unicode, markup-looking text and templates survive intact."""
    path = _write(workdir, "fancy", RICH_SOURCE)
    result = CliRunner().invoke(app, ["show", "fancy"])

    assert result.exit_code == 0, result.output
    assert result.stdout == RICH_SOURCE
    assert result.stderr == f"project: {path.absolute()}\n"
    # Read-only: the file is untouched and nothing was recorded.
    assert path.read_text(encoding="utf-8") == RICH_SOURCE
    assert _flow_count(workdir) == 0


def test_default_mode_adds_a_newline_only_when_the_file_lacks_one(workdir):
    """A file with no trailing newline still ends the terminal line."""
    _write(workdir, "fancy", RICH_SOURCE.rstrip("\n"))
    result = CliRunner().invoke(app, ["show", "fancy"])
    assert result.exit_code == 0, result.output
    assert result.stdout == RICH_SOURCE


def test_json_round_trips_through_the_model(workdir):
    """The `conduit` object re-validates and keeps aliases and templates."""
    _write(workdir, "inputs", INPUTS_SOURCE)
    result = CliRunner().invoke(app, ["show", "inputs", "--json"])
    assert result.exit_code == 0, result.output

    payload = json.loads(result.stdout)
    assert payload["source"] == "project"
    assert payload["path"].endswith(os.path.join("inputs", "conduit.yaml"))

    conduit = Conduit.model_validate(payload["conduit"])
    assert conduit.name == "inputs"
    # `while`, not Python's `while_`, so the JSON is re-writable as YAML.
    assert payload["conduit"]["tasks"][0]["while"] == "output.match(again)"
    assert "while_" not in payload["conduit"]["tasks"][0]
    # Templates stay unresolved and defaults stay declared, not applied.
    assert payload["conduit"]["tasks"][0]["task"] == "echo {{inputs.referenced_only}}"
    assert payload["conduit"]["tasks"][1]["inputs"] == {
        "name": "{{inputs.forwarded_key}}"
    }
    assert payload["conduit"]["inputs"]["required_key"]["default"] is None
    assert payload["conduit"]["inputs"]["empty_default_key"]["default"] == ""


def test_json_names_accepted_and_required_inputs(workdir):
    """Accepted covers referenced-only keys; required is declared-without-default."""
    _write(workdir, "inputs", INPUTS_SOURCE)
    result = CliRunner().invoke(app, ["show", "inputs", "--json"])
    assert result.exit_code == 0, result.output

    payload = json.loads(result.stdout)
    assert payload["accepted_inputs"] == [
        "defaulted_key",
        "empty_default_key",
        "forwarded_key",
        "referenced_only",
        "required_key",
    ]
    # An empty-string default is still a default, so it is not required.
    assert payload["required_inputs"] == ["required_key"]


def test_project_copy_shadows_global(workdir):
    """With both copies present, `show` reads the one that would run."""
    _write(workdir, "dup", RICH_SOURCE.replace("name: fancy", "name: dup"))
    _write(
        workdir,
        "dup",
        "name: dup\ndescription: the global one\n"
        "tasks:\n  - name: a\n    description: a\n    task: echo hi\n    tool: tool:bash\n",
        source="global",
    )
    result = CliRunner().invoke(app, ["show", "dup"])
    assert result.exit_code == 0, result.output
    assert "keep this comment" in result.stdout
    assert "the global one" not in result.stdout
    assert "project:" in result.stderr


def test_global_only_conduit_resolves(workdir):
    """A conduit that exists only globally is labelled `global`."""
    path = _write(
        workdir,
        "shared",
        "name: shared\ndescription: from the global store\n"
        "tasks:\n  - name: a\n    description: a\n    task: echo hi\n    tool: tool:bash\n",
        source="global",
    )
    result = CliRunner().invoke(app, ["show", "shared", "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["source"] == "global"
    assert payload["path"] == str(path.absolute())


def test_broken_project_copy_is_never_replaced_by_the_global_one(workdir):
    """Default mode shows the broken source; JSON fails without leaking global."""
    _write(workdir, "dup", "name: dup\ndescription: [unclosed\n")
    _write(
        workdir,
        "dup",
        "name: dup\ndescription: the global one\n"
        "tasks:\n  - name: a\n    description: a\n    task: echo hi\n    tool: tool:bash\n",
        source="global",
    )
    raw = CliRunner().invoke(app, ["show", "dup"])
    assert raw.exit_code == 0, raw.output
    assert raw.stdout == "name: dup\ndescription: [unclosed\n"

    as_json = CliRunner().invoke(app, ["show", "dup", "--json"])
    assert as_json.exit_code == 1
    assert as_json.stdout == ""
    assert "FAIL" in as_json.stderr
    assert "the global one" not in as_json.stderr


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        ("name: bad\ndescription: [unclosed\n", "YAML"),
        ("name: bad\ndescription: d\ntasks: []\ninputs: 7\n", "inputs"),
        ("name: other\ndescription: d\ntasks: []\n", "folder name"),
    ],
    ids=["malformed-yaml", "schema-error", "name-mismatch"],
)
def test_json_mode_fails_cleanly_on_a_bad_definition(workdir, body, expected):
    """A definition JSON cannot represent exits 1 with stderr and no stdout."""
    _write(workdir, "bad", body)
    result = CliRunner().invoke(app, ["show", "bad", "--json"])
    assert result.exit_code == 1
    assert result.stdout == ""
    assert "FAIL" in result.stderr
    assert expected in result.stderr
    assert "Traceback" not in result.stderr


def test_unreadable_source_fails_in_both_modes(workdir):
    """A `conduit.yaml` that cannot be read is an error, not a partial answer."""
    # A directory in the file's place: listed by the store, unreadable by both
    # modes, and no monkeypatching needed to provoke it.
    (workdir / ".atelier" / "conduits" / "hollow" / "conduit.yaml").mkdir(parents=True)
    for args in (["show", "hollow"], ["show", "hollow", "--json"]):
        result = CliRunner().invoke(app, args)
        assert result.exit_code == 1, result.output
        assert result.stdout == ""
        assert "cannot read" in result.stderr
        assert "Traceback" not in result.stderr


@pytest.mark.parametrize("json_mode", [False, True], ids=["raw", "json"])
def test_unknown_name_guidance_goes_to_stderr(workdir, json_mode):
    """A typo suggests the close name and never contaminates stdout."""
    _write(workdir, "fancy", RICH_SOURCE)
    args = ["show", "fanc"] + (["--json"] if json_mode else [])
    result = CliRunner().invoke(app, args)
    assert result.exit_code == 1
    assert result.stdout == ""
    assert "unknown conduit" in result.stderr
    assert "did you mean: fancy?" in result.stderr


def test_unsafe_name_is_an_unknown_conduit_not_a_crash(workdir):
    """A path-traversal name never reaches the filesystem."""
    result = CliRunner().invoke(app, ["show", "../../etc/passwd"])
    assert result.exit_code == 1
    assert "unknown conduit" in result.stderr
    assert "Traceback" not in result.stderr


def test_inspection_never_probes_a_harness_or_runs_a_task(workdir, monkeypatch):
    """A conduit whose harness is unusable, and a cyclic one, still inspect."""

    def _boom(*args, **kwargs):
        """Fail loudly if inspection reaches readiness or execution.

        :param args: ignored positional arguments.
        :param kwargs: ignored keyword arguments.
        """
        raise AssertionError("show must not probe or run anything")

    monkeypatch.setattr(Atelier, "tool_readiness", _boom)
    monkeypatch.setattr(Atelier, "run_conduit", _boom)

    _write(
        workdir,
        "agentic",
        "name: agentic\ndescription: needs an agent\n"
        "tasks:\n  - name: build\n    description: b\n    task: do it\n"
        "    tool: harness:claude-code\n",
    )
    # Schema-valid but a cycle: `plan`/`check` reject it, `show` still reads it.
    _write(
        workdir,
        "cyclic",
        "name: cyclic\ndescription: a to b to a\n"
        "tasks:\n  - name: a\n    description: a\n    task: echo a\n"
        "    tool: tool:bash\n    depends_on: [b]\n"
        "  - name: b\n    description: b\n    task: echo b\n"
        "    tool: tool:bash\n    depends_on: [a]\n",
    )
    for name in ("agentic", "cyclic"):
        for args in (["show", name], ["show", name, "--json"]):
            result = CliRunner().invoke(app, args)
            assert result.exit_code == 0, result.output
    assert _flow_count(workdir) == 0


def test_inspect_then_run_with_the_inputs_json_reported(workdir):
    """The JSON envelope is enough to build a correct invocation."""
    _write(
        workdir,
        "greeter",
        "name: greeter\ndescription: greet someone\n"
        "inputs:\n  who:\n    description: who to greet\n"
        "  greeting:\n    description: the word to use\n    default: hello\n"
        'tasks:\n  - say:\n      description: say it\n      task: "echo '
        '{{inputs.greeting}} {{inputs.who}}"\n      tool: tool:bash\n',
    )
    runner = CliRunner()

    listed = runner.invoke(app, ["list", "conduits", "--json"])
    assert listed.exit_code == 0, listed.output
    assert "greeter" in [row["name"] for row in json.loads(listed.stdout)]

    shown = runner.invoke(app, ["show", "greeter", "--json"])
    assert shown.exit_code == 0, shown.output
    payload = json.loads(shown.stdout)
    assert payload["required_inputs"] == ["who"]

    # Exactly what an agent would do with the answer: supply the required
    # keys and nothing else.
    flags = [f"--input={key}=world" for key in payload["required_inputs"]]
    run = runner.invoke(app, ["run", "greeter", *flags])
    assert run.exit_code == 0, run.output

    saved = runner.invoke(app, ["outputs", "latest", "--task", "say"])
    assert saved.exit_code == 0, saved.output
    # The omitted input fell back to its declared default.
    assert saved.stdout.strip() == "hello world"


def test_generated_code_review_template_is_inspectable(workdir, monkeypatch):
    """The starter `create` writes can be read back without an agent."""

    def _boom(*args, **kwargs):
        """Fail loudly if inspection probes the harness.

        :param args: ignored positional arguments.
        :param kwargs: ignored keyword arguments.
        """
        raise AssertionError("show must not probe a harness")

    runner = CliRunner()
    created = runner.invoke(app, ["create", "my-review", "--template", "code-review"])
    assert created.exit_code == 0, created.output

    monkeypatch.setattr(Atelier, "tool_readiness", _boom)
    raw = runner.invoke(app, ["show", "my-review"])
    assert raw.exit_code == 0, raw.output
    assert "BEGIN PATCH" in raw.stdout
    assert "{{diff.output}}" in raw.stdout

    shown = runner.invoke(app, ["show", "my-review", "--json"])
    assert shown.exit_code == 0, shown.output
    payload = json.loads(shown.stdout)
    assert [t["name"] for t in payload["conduit"]["tasks"]] == ["diff", "review"]
    assert payload["required_inputs"] == []
