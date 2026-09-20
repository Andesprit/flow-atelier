"""CLI tests for `atelier create` — scaffold a new conduit by name."""
from __future__ import annotations

import os

import pytest
from typer.testing import CliRunner

from flow_atelier.cli import app
from flow_atelier.modules.engine import validate_conduit
from flow_atelier.schemas.conduit import Conduit


@pytest.fixture
def workdir(tmp_path, monkeypatch):
    """Isolated cwd with an empty `.atelier` tree and isolated global dir.

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


def test_create_writes_valid_conduit(workdir):
    """Happy path: writes a parseable conduit.yaml and prints the run hint."""
    result = CliRunner().invoke(app, ["create", "my-flow"])
    assert result.exit_code == 0, result.output
    path = workdir / ".atelier" / "conduits" / "my-flow" / "conduit.yaml"
    assert path.exists()
    Conduit.model_validate_json(_yaml_to_json(path.read_text()))
    assert "atelier run my-flow" in result.output


def test_create_refuses_to_clobber(workdir):
    """Re-creating an existing name exits 1 and leaves the original intact."""
    assert CliRunner().invoke(app, ["create", "dup"]).exit_code == 0
    path = workdir / ".atelier" / "conduits" / "dup" / "conduit.yaml"
    before = path.read_text()
    result = CliRunner().invoke(app, ["create", "dup"])
    assert result.exit_code == 1
    assert "already exists" in result.output
    assert "Traceback" not in result.output
    assert path.read_text() == before


def test_create_rejects_invalid_name(workdir):
    """An invalid name exits 1 with a validation message and no traceback."""
    result = CliRunner().invoke(app, ["create", "bad name!"])
    assert result.exit_code == 1
    assert "invalid" in result.output
    assert "Traceback" not in result.output


def test_create_then_list_and_check(workdir):
    """The scaffold is genuinely valid: it lists and passes `check`."""
    assert CliRunner().invoke(app, ["create", "roundtrip"]).exit_code == 0
    listed = CliRunner().invoke(app, ["list", "conduits"])
    assert listed.exit_code == 0
    assert "roundtrip" in listed.output
    checked = CliRunner().invoke(app, ["check", "roundtrip"])
    assert checked.exit_code == 0


def test_create_writes_no_flows(workdir):
    """Creation is inert: it writes a conduit and runs nothing."""
    assert CliRunner().invoke(app, ["create", "inert"]).exit_code == 0
    assert list((workdir / ".atelier" / "flows").glob("*")) == []


def test_explicit_hello_template_matches_the_default(workdir):
    """`--template hello` is the behaviour `create` already had."""
    assert CliRunner().invoke(app, ["create", "implicit"]).exit_code == 0
    assert CliRunner().invoke(
        app, ["create", "explicit", "--template", "hello"]
    ).exit_code == 0
    implicit = _conduit(workdir, "implicit")
    explicit = _conduit(workdir, "explicit")
    assert [t.model_dump() for t in implicit.tasks] == [
        t.model_dump() for t in explicit.tasks
    ]
    assert implicit.inputs.keys() == {"name"}


def test_code_review_template_builds_a_gated_two_task_dag(workdir):
    """The starter is a valid, input-free DAG: bash diff gates an agent review."""
    result = CliRunner().invoke(app, ["create", "review", "--template", "code-review"])
    assert result.exit_code == 0, result.output
    conduit = _conduit(workdir, "review")
    validate_conduit(conduit)
    assert conduit.inputs == {}
    diff, review = conduit.tasks
    assert diff.name == "diff" and diff.tool == "tool:bash"
    assert diff.task == "git diff --cached --no-color --no-ext-diff --no-textconv --"
    assert diff.depends_on == []
    assert review.name == "review" and review.tool == "harness:claude-code"
    assert review.depends_on == [r"diff.output.match(\S)"]
    assert "{{diff.output}}" in review.task


def test_code_review_hints_name_the_next_commands(workdir):
    """The printed guidance leads to a run and its saved output, not to inputs."""
    result = CliRunner().invoke(app, ["create", "review", "--template", "code-review"])
    assert ".atelier/conduits/review/conduit.yaml" in result.output.replace("\\", "/")
    for expected in (
        "atelier harness check claude-code",
        "atelier check review",
        "atelier plan review",
        "atelier run review",
        "atelier outputs latest --task review",
    ):
        assert expected in result.output
    assert "--input name=world" not in result.output


def test_code_review_template_takes_a_description_override(workdir):
    """`--description` still wins over the template's own default."""
    assert CliRunner().invoke(
        app,
        ["create", "review", "--template", "code-review", "-d", "my house rules"],
    ).exit_code == 0
    assert _conduit(workdir, "review").description == "my house rules"


def test_unknown_template_writes_nothing(workdir):
    """An unrecognized template is refused by the parser, before any write."""
    result = CliRunner().invoke(app, ["create", "review", "--template", "deploy"])
    assert result.exit_code == 2
    assert not (workdir / ".atelier" / "conduits" / "review").exists()


def test_templated_create_refuses_to_clobber(workdir):
    """A name collision leaves the existing conduit byte-identical."""
    assert CliRunner().invoke(app, ["create", "dup"]).exit_code == 0
    path = workdir / ".atelier" / "conduits" / "dup" / "conduit.yaml"
    before = path.read_text()
    result = CliRunner().invoke(app, ["create", "dup", "--template", "code-review"])
    assert result.exit_code == 1
    assert "already exists" in result.output
    assert path.read_text() == before


def test_templated_create_refuses_a_global_collision(workdir):
    """A global conduit of the same name blocks the project scaffold too."""
    global_dir = workdir / "global" / "conduits" / "shadow"
    global_dir.mkdir(parents=True)
    (global_dir / "conduit.yaml").write_text(
        "name: shadow\ndescription: d\ntasks:\n  - name: t\n"
        "    description: d\n    task: 'echo hi'\n    tool: tool:bash\n"
    )
    result = CliRunner().invoke(app, ["create", "shadow", "--template", "code-review"])
    assert result.exit_code == 1
    assert "already exists" in result.output
    assert not (workdir / ".atelier" / "conduits" / "shadow").exists()


def _conduit(workdir, name: str) -> Conduit:
    """Load a scaffolded conduit back off disk.

    :param workdir: the isolated project directory.
    :param name: conduit name to read.
    :returns: the parsed :class:`Conduit`.
    """
    path = workdir / ".atelier" / "conduits" / name / "conduit.yaml"
    return Conduit.model_validate_json(_yaml_to_json(path.read_text()))


def _yaml_to_json(text: str) -> str:
    """Parse YAML text and re-emit as JSON for model validation.

    :param text: YAML document.
    :returns: JSON string of the same data.
    """
    import json

    import yaml

    return json.dumps(yaml.safe_load(text))


def test_create_matches_init_shape(workdir):
    """`create` and `init` write the same compact YAML, differing only in name and description."""
    assert CliRunner().invoke(app, ["create", "hello", "-d", "Say hello"]).exit_code == 0
    created = (workdir / ".atelier" / "conduits" / "hello" / "conduit.yaml").read_text()
    other = workdir / "other"
    other.mkdir()
    import os as _os

    cwd = _os.getcwd()
    _os.chdir(other)
    try:
        assert CliRunner().invoke(app, ["init"]).exit_code == 0
    finally:
        _os.chdir(cwd)
    initialized = (other / ".atelier" / "conduits" / "hello" / "conduit.yaml").read_text()
    assert created == initialized
    assert "- greet:" in created
    assert "retry_backoff" not in created
