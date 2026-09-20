"""CLI tests for `atelier schema` — export the conduit authoring schema."""
from __future__ import annotations

import json
import os

import pytest
import yaml
from jsonschema import Draft202012Validator
from jsonschema.exceptions import best_match
from pydantic import ValidationError
from typer.testing import CliRunner

from flow_atelier.cli import app
from flow_atelier.core.atelier import Atelier
from flow_atelier.schemas.authoring import DIALECT, conduit_json_schema
from flow_atelier.schemas.conduit import Conduit, TaskDefinition

# Every authoring shape at once: a shorthand input, a full InputSpec, an
# empty-string default, a wrapped task, a wrapped task that names itself, a
# fully normalized task object, a loop, a custom harness, forwarded inputs
# and a nested interaction policy.
EVERY_SHAPE = """\
name: every-shape
description: one of each authoring form
max_concurrency: 2
inputs:
  shorthand: Who to greet
  spelled_out:
    description: has a default
    default: fallback
  empty_default:
    description: an empty string is still a default
    default: ""
tasks:
  - wrapped:
      description: named by the mapping key
      task: "echo {{inputs.shorthand}}"
      tool: tool:bash
      repeat: 3
      while: output.match(again)
  - ignored_key:
      name: names_itself
      description: an explicit name inside the body wins over the key
      task: echo two
      tool: harness:my-own-agent
      depends_on: [wrapped]
  - name: normalized
    description: a plain task object
    task: hello
    tool: tool:conduit
    depends_on: [names_itself]
    inputs:
      name: "{{inputs.forwarded}}"
interaction:
  questions: supervisor
  permissions: hybrid
  supervisor:
    tool: harness:my-own-agent
    instructions: answer briefly
    max_replies: 2
"""


@pytest.fixture
def workdir(tmp_path, monkeypatch):
    """Isolated empty cwd with no project or global store at all.

    :param tmp_path: pytest temp directory fixture.
    :param monkeypatch: pytest monkeypatch fixture.
    :returns: the working directory path.
    """
    global_dir = tmp_path / "global"
    global_dir.mkdir()
    monkeypatch.chdir(tmp_path)
    for k in list(os.environ):
        if k.startswith("ATELIER_"):
            monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("ATELIER_GLOBAL_ATELIER_DIR", str(global_dir))
    monkeypatch.setenv("ATELIER_NO_UPDATE_CHECK", "1")
    return tmp_path


def _export(args=("schema",)) -> dict:
    """Invoke the CLI and return the exported schema.

    :param args: argv to pass to the app.
    :returns: the parsed JSON object written to stdout.
    """
    result = CliRunner().invoke(app, list(args))
    assert result.exit_code == 0, result.output
    assert result.stderr == ""
    assert result.stdout.endswith("}\n")
    return json.loads(result.stdout)


def _errors(schema: dict, document) -> list:
    """Validate ``document`` against ``schema`` with a real validator.

    :param schema: the exported authoring schema.
    :param document: the parsed YAML document to check.
    :returns: every validation error, in document order.
    """
    return sorted(Draft202012Validator(schema).iter_errors(document), key=str)


def test_stdout_is_one_json_object_and_stderr_is_empty(workdir):
    """The export is pipeable: JSON on stdout, nothing else anywhere."""
    schema = _export()
    assert schema["$schema"] == DIALECT
    assert schema["type"] == "object"
    assert "flow-atelier" in schema["description"]
    # It says what it does not check, so nobody reads it as `atelier check`.
    assert "atelier check" in schema["description"]


def test_the_export_is_a_valid_schema_and_repeatable(workdir):
    """It satisfies its own declared dialect and never drifts between runs."""
    schema = _export()
    Draft202012Validator.check_schema(schema)
    assert _export() == schema


def test_every_reference_resolves_inside_the_document(workdir):
    """No `$ref` points at a URL or a file the user does not have."""
    schema = _export()
    refs: list[str] = []

    def walk(node) -> None:
        """Collect every `$ref` string under ``node``.

        :param node: any JSON value in the schema.
        """
        if isinstance(node, dict):
            if isinstance(node.get("$ref"), str):
                refs.append(node["$ref"])
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    walk(schema)
    assert refs
    for ref in refs:
        assert ref.startswith("#/$defs/"), ref
        assert ref.removeprefix("#/$defs/") in schema["$defs"], ref


def test_nothing_is_read_run_or_written(workdir, monkeypatch):
    """No facade, no store scan, no readiness probe, no directory created."""

    def _boom(*args, **kwargs):
        """Fail the test if any runtime entry point is reached.

        :raises AssertionError: always.
        """
        raise AssertionError("atelier schema must not touch the runtime")

    monkeypatch.setattr(Atelier, "__init__", _boom)
    monkeypatch.setattr(Atelier, "tool_readiness", _boom)
    monkeypatch.setattr(Atelier, "run_conduit", _boom)

    assert _export()["title"] == "Flow Atelier conduit"
    assert list(workdir.iterdir()) == [workdir / "global"]


def test_help_explains_the_purpose_and_the_limit(workdir):
    """`--help` tells you what to do with the file and what it cannot catch."""
    result = CliRunner().invoke(app, ["schema", "--help"])
    assert result.exit_code == 0, result.output
    assert "yaml-language-server" in result.output
    assert "atelier check" in result.output


@pytest.mark.parametrize(
    "create_args",
    [
        ["init"],
        ["create", "greeter", "--template", "hello"],
        ["create", "reviewer", "--template", "code-review"],
    ],
)
def test_generated_starters_validate_as_written_on_disk(workdir, create_args):
    """Every starter this version writes passes its own exported schema."""
    schema = _export()
    runner = CliRunner()
    assert runner.invoke(app, create_args).exit_code == 0

    files = sorted((workdir / ".atelier" / "conduits").glob("*/conduit.yaml"))
    assert files
    for path in files:
        # Parsed from the file, before any model normalization can fix it up.
        document = yaml.safe_load(path.read_text(encoding="utf-8"))
        assert _errors(schema, document) == [], path
        # And the loader agrees the same bytes are a real conduit.
        assert Conduit.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))


def test_every_authoring_shape_validates_and_loads(workdir):
    """Shorthands, wrapped tasks, loops, harnesses and nested policy all pass."""
    schema = _export()
    assert _errors(schema, yaml.safe_load(EVERY_SHAPE)) == []

    conduit = Conduit.model_validate(yaml.safe_load(EVERY_SHAPE))
    assert [t.name for t in conduit.tasks] == ["wrapped", "names_itself", "normalized"]
    assert conduit.inputs["shorthand"].description == "Who to greet"
    assert conduit.tasks[0].while_ == "output.match(again)"


def test_the_normalized_form_validates_too(workdir):
    """What `show --json` emits can be written straight back as a conduit."""
    schema = _export()
    cdir = workdir / ".atelier" / "conduits" / "every-shape"
    cdir.mkdir(parents=True)
    (cdir / "conduit.yaml").write_text(EVERY_SHAPE, encoding="utf-8")

    result = CliRunner().invoke(app, ["show", "every-shape", "--json"])
    assert result.exit_code == 0, result.output
    assert _errors(schema, json.loads(result.stdout)["conduit"]) == []


@pytest.mark.parametrize(
    ("break_it", "where", "says"),
    [
        (
            lambda d: d["tasks"][0]["wrapped"].pop("tool"),
            ["tasks", 0, "wrapped"],
            "'tool' is a required property",
        ),
        (
            lambda d: d.__setitem__("tasks", {"wrapped": {}}),
            ["tasks"],
            "is not of type 'array'",
        ),
        (
            lambda d: d["tasks"].__setitem__(0, {"wrapped": "echo hi"}),
            ["tasks", 0, "wrapped"],
            "is not of type 'object'",
        ),
        (
            lambda d: d.__setitem__("max_concurrency", 0),
            ["max_concurrency"],
            "0 is less than the minimum of 1",
        ),
        (
            lambda d: d["inputs"].__setitem__("shorthand", 7),
            ["inputs", "shorthand"],
            "is not valid under any of the given schemas",
        ),
    ],
)
def test_structural_mistakes_are_rejected_where_they_are(workdir, break_it, where, says):
    """A broken file fails, and the error points at the part that is wrong."""
    schema = _export()
    document = yaml.safe_load(EVERY_SHAPE)
    break_it(document)

    errors = _errors(schema, document)
    assert errors
    worst = best_match(errors)
    assert list(worst.absolute_path) == where
    assert says in worst.message
    # The loader refuses the same document, so the schema is not stricter.
    with pytest.raises(Exception):
        Conduit.model_validate(document)


def test_semantic_mistakes_are_left_to_check(workdir):
    """A dependency cycle is well-formed YAML; only the engine can see it."""
    schema = _export()
    document = yaml.safe_load(EVERY_SHAPE)
    document["tasks"][0]["wrapped"]["depends_on"] = ["normalized"]
    assert _errors(schema, document) == []

    cdir = workdir / ".atelier" / "conduits" / "every-shape"
    cdir.mkdir(parents=True)
    (cdir / "conduit.yaml").write_text(yaml.safe_dump(document), encoding="utf-8")
    result = CliRunner().invoke(app, ["check", "every-shape"])
    assert result.exit_code != 0
    assert "circular dependency" in result.output.lower()


def test_the_task_fields_come_from_the_model(workdir):
    """Nothing is hand-listed, so the export cannot drift from the models."""
    schema = _export()
    task_fields = {
        f.alias or name for name, f in TaskDefinition.model_fields.items()
    }
    defs = schema["$defs"]
    assert set(defs["TaskDefinition"]["properties"]) == task_fields
    assert set(defs["WrappedTaskBody"]["properties"]) == task_fields
    assert set(schema["properties"]) == {
        f.alias or name for name, f in Conduit.model_fields.items()
    }
    # The key supplies the name, so only that requirement is relaxed.
    assert defs["TaskDefinition"]["required"] == ["name", "description", "task", "tool"]
    assert defs["WrappedTaskBody"]["required"] == ["description", "task", "tool"]


def test_the_readme_walkthrough_works_end_to_end(workdir):
    """Export, catch a real mistake through the modeline, repair, run, read."""
    runner = CliRunner()
    assert runner.invoke(app, ["init"]).exit_code == 0

    exported = workdir / ".atelier" / "conduit.schema.json"
    exported.write_text(json.dumps(conduit_json_schema(), indent=2), encoding="utf-8")

    conduit = workdir / ".atelier" / "conduits" / "hello" / "conduit.yaml"
    modeline = "# yaml-language-server: $schema=../../conduit.schema.json\n"
    broken = modeline + "max_concurrency: 0\n" + conduit.read_text(encoding="utf-8")
    conduit.write_text(broken, encoding="utf-8")

    # The path in the modeline is what an editor would open, relative to the
    # conduit file itself.
    referenced = (conduit.parent / "../../conduit.schema.json").resolve()
    assert referenced == exported.resolve()
    schema = json.loads(referenced.read_text(encoding="utf-8"))

    errors = _errors(schema, yaml.safe_load(broken))
    assert [e.message for e in errors] == ["0 is less than the minimum of 1"]

    conduit.write_text(broken.replace("max_concurrency: 0\n", ""), encoding="utf-8")
    assert _errors(schema, yaml.safe_load(conduit.read_text(encoding="utf-8"))) == []

    assert runner.invoke(app, ["check", "hello"]).exit_code == 0
    assert runner.invoke(app, ["run", "hello", "--input", "name=world"]).exit_code == 0
    outputs = runner.invoke(app, ["outputs", "latest", "--task", "greet"])
    assert outputs.exit_code == 0, outputs.output
    assert "hello world" in outputs.output


@pytest.mark.parametrize(
    ("field", "break_it"),
    [
        # The conduit root.
        ("max_concurreny", lambda d: d.__setitem__("max_concurreny", 5)),
        # A wrapped task body...
        (
            "depend_on",
            lambda d: d["tasks"][0]["wrapped"].__setitem__("depend_on", []),
        ),
        # ...and a plain task object, the other form the same file may use.
        ("depend_on", lambda d: d["tasks"][2].__setitem__("depend_on", [])),
        # An object-form input specification.
        (
            "defaut",
            lambda d: d["inputs"]["spelled_out"].__setitem__("defaut", "x"),
        ),
        # A typo sitting beside the field it misspells, so the correctly
        # spelled one cannot make the file look complete.
        (
            "timout",
            lambda d: d["tasks"][2].update({"timeout": 30, "timout": 60}),
        ),
    ],
)
def test_a_misspelled_field_fails_the_schema_as_well_as_the_loader(
    workdir, field, break_it
):
    """An editor holding the export catches what the loader now refuses.

    Both sides have to move together: a schema that still accepted the typo
    would green-light a file `atelier check` then rejects, and the author
    would learn about it only after saving.
    """
    schema = _export()
    document = yaml.safe_load(EVERY_SHAPE)
    assert _errors(schema, document) == []
    assert Conduit.model_validate(yaml.safe_load(EVERY_SHAPE))

    break_it(document)

    assert _errors(schema, document), field
    with pytest.raises(ValidationError) as ei:
        Conduit.model_validate(document)
    assert any(field in e["loc"] for e in ei.value.errors()), ei.value.errors()
