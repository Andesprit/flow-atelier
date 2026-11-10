"""CLI tests for `atelier check` readiness gating + required-input listing."""
from __future__ import annotations

import os

import pytest
from typer.testing import CliRunner

from flow_atelier.cli import app


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


def _write_conduit(workdir, name: str, body: str) -> None:
    """Write a conduit.yaml under the project `.atelier/conduits/<name>/`.

    :param workdir: working directory path.
    :param name: conduit name (also the folder name).
    :param body: full YAML document for the conduit.
    """
    cdir = workdir / ".atelier" / "conduits" / name
    cdir.mkdir(parents=True)
    (cdir / "conduit.yaml").write_text(body)


def test_check_ok_for_bash_only(workdir):
    """A bash-only conduit checks OK and lists no required inputs."""
    _write_conduit(
        workdir,
        "bashy",
        "name: bashy\ndescription: d\n"
        "tasks:\n  - name: a\n    description: a\n    task: echo hi\n    tool: tool:bash\n",
    )
    result = CliRunner().invoke(app, ["check", "bashy"])
    assert result.exit_code == 0
    assert "OK" in result.stdout
    assert "requires --input" not in result.stdout


def test_check_lists_required_inputs(workdir):
    """An OK conduit with a no-default input shows the requires line."""
    _write_conduit(
        workdir,
        "needy",
        "name: needy\ndescription: d\n"
        "inputs:\n  topic:\n    description: the topic\n"
        "tasks:\n  - name: a\n    description: a\n    task: echo {{inputs.topic}}\n    tool: tool:bash\n",
    )
    result = CliRunner().invoke(app, ["check", "needy"])
    assert result.exit_code == 0
    assert "requires --input" in result.stdout
    assert "topic" in result.stdout


def test_check_fails_when_harness_binary_missing(workdir, monkeypatch):
    """A harness whose CLI is absent from PATH makes check FAIL with exit 1."""
    monkeypatch.setattr(
        "flow_atelier.services.executor.harness.shutil.which",
        lambda _binary: None,
    )
    _write_conduit(
        workdir,
        "agentic",
        "name: agentic\ndescription: d\n"
        "tasks:\n  - name: build\n    description: b\n    task: do it\n    tool: harness:claude-code\n",
    )
    result = CliRunner().invoke(app, ["check", "agentic"])
    assert result.exit_code == 1
    assert "FAIL" in result.stdout
    assert "build" in result.stdout


def test_check_json_reports_each_conduit(workdir):
    """`--json` emits one record per conduit and still exits 1 on any failure."""
    import json

    _write_conduit(
        workdir,
        "good",
        "name: good\ndescription: d\ninputs:\n  who: w\n"
        "tasks:\n  - name: a\n    description: a\n    task: echo {{inputs.who}}\n    tool: tool:bash\n",
    )
    _write_conduit(
        workdir,
        "broken",
        "name: broken\ndescription: d\ntasks:\n"
        "  - name: a\n    description: a\n    task: x\n    tool: tool:bash\n    depends_on: [a]\n",
    )
    result = CliRunner().invoke(app, ["check", "--json"])
    assert result.exit_code == 1
    rows = {row["name"]: row for row in json.loads(result.stdout)}
    assert rows["good"]["ok"] is True
    assert rows["good"]["required_inputs"] == ["who"]
    assert rows["broken"]["ok"] is False
    assert rows["broken"]["error"]


def test_check_warns_when_a_shell_step_puts_a_value_into_its_command(workdir):
    """Each direct value placeholder in a bash task, inline bodies included, warns."""
    import json

    _write_conduit(
        workdir,
        "direct",
        "name: direct\ndescription: d\ninputs:\n  repo-url: r\n"
        "tasks:\n"
        "  - name: prep\n    description: p\n    tool: tool:bash\n"
        "    task: git clone {{inputs.repo-url}} && cd {{conduit_dir}}\n"
        "  - name: show\n    description: s\n    tool: tool:bash\n"
        "    depends_on: [prep]\n    task: 'echo \"{{prep.output}}\" \"$OUTPUT_PREP\"'\n"
        "  - name: loop\n    description: l\n    tool: tool:conduit\n    repeat: 2\n"
        "    inputs:\n      feedback: '{{loop.previous}}'\n"
        "    tasks:\n"
        "      - name: test\n        description: t\n        tool: tool:bash\n"
        "        task: echo {{inputs.feedback}}\n",
    )
    result = CliRunner().invoke(app, ["check", "direct", "--json"])
    assert result.exit_code == 0, result.stdout
    assert json.loads(result.stdout)[0]["warnings"] == [
        'prep: use "$INPUT_REPO_URL" instead of {{inputs.repo-url}}, '
        "so the value cannot run as a command; a value past the step's "
        '100,000-byte budget comes only as "$INPUT_REPO_URL_FILE"',
        'show: use "$OUTPUT_PREP" instead of {{prep.output}}, '
        "so the value cannot run as a command; it keeps the trailing newline, "
        "and a value past the step's 100,000-byte budget comes only as "
        '"$OUTPUT_PREP_FILE"',
        'loop.test: use "$INPUT_FEEDBACK" instead of {{inputs.feedback}}, '
        "so the value, which can hold a failed try's raw output, cannot run "
        "as a command; a value past the step's 100,000-byte budget comes only "
        'as "$INPUT_FEEDBACK_FILE"',
    ]


def test_check_does_not_warn_for_variables_paths_or_other_tools(workdir):
    """Env vars, {{conduit_dir}} and a value in a non-shell task are fine."""
    import json

    _write_conduit(
        workdir,
        "clean",
        "name: clean\ndescription: d\ninputs:\n  name: n\n"
        "tasks:\n"
        "  - name: prep\n    description: p\n    tool: tool:bash\n    task: 'true'\n"
        "  - name: greet\n    description: g\n    tool: tool:bash\n    depends_on: [prep]\n"
        "    task: 'echo \"hello $INPUT_NAME\" && ls {{conduit_dir}} {{prep.workspace}}'\n"
        "  - name: ask\n    description: a\n    tool: tool:hitl\n"
        "    task: 'Greet {{inputs.name}}?'\n    inputs:\n      ok: yes?\n",
    )
    result = CliRunner().invoke(app, ["check", "clean", "--json"])
    assert result.exit_code == 0, result.stdout
    assert "warnings" not in json.loads(result.stdout)[0]


def test_check_tells_to_declare_an_input_only_a_placeholder_named(workdir):
    """Without {{inputs.name}}, run accepts name only once inputs: declares it."""
    import json

    _write_conduit(
        workdir,
        "undeclared",
        "name: undeclared\ndescription: d\n"
        "tasks:\n"
        "  - name: greet\n    description: g\n    tool: tool:bash\n"
        "    task: echo hello {{inputs.name}}\n",
    )
    result = CliRunner().invoke(app, ["check", "undeclared", "--json"])
    assert result.exit_code == 0, result.stdout
    assert json.loads(result.stdout)[0]["warnings"] == [
        'greet: use "$INPUT_NAME" instead of {{inputs.name}}, '
        "so the value cannot run as a command; a value past the step's "
        '100,000-byte budget comes only as "$INPUT_NAME_FILE"; '
        "declare name under inputs: too",
    ]


def test_check_does_not_tell_to_declare_a_hitl_answer(workdir):
    """A human's answer reaches $INPUT_<NAME>; declaring it would ask for it twice."""
    import json

    _write_conduit(
        workdir,
        "gate",
        "name: gate\ndescription: d\n"
        "tasks:\n"
        "  - name: ask\n    description: a\n    tool: tool:hitl\n"
        "    task: which branch?\n    inputs:\n      branch: branch name\n"
        "  - name: deploy\n    description: d\n    tool: tool:bash\n"
        "    depends_on: [ask]\n    task: echo deploying {{inputs.branch}}\n",
    )
    result = CliRunner().invoke(app, ["check", "gate", "--json"])
    assert result.exit_code == 0, result.stdout
    assert json.loads(result.stdout)[0]["warnings"] == [
        'deploy: use "$INPUT_BRANCH" instead of {{inputs.branch}}, '
        "so the value cannot run as a command; a value past the step's "
        '100,000-byte budget comes only as "$INPUT_BRANCH_FILE"',
    ]


def test_check_never_points_at_a_variable_another_name_owns(workdir):
    """my-name sorts first and owns $INPUT_MY_NAME; report owns $INPUT_REPORT_FILE."""
    import json

    _write_conduit(
        workdir,
        "coll",
        "name: coll\ndescription: d\n"
        "inputs:\n  my-name: a\n  my_name: b\n  report: c\n  report_file: d\n"
        "tasks:\n"
        "  - name: t\n    description: t\n    tool: tool:bash\n"
        "    task: echo {{inputs.my_name}} {{inputs.report_file}} {{inputs.my-name}}\n",
    )
    result = CliRunner().invoke(app, ["check", "coll", "--json"])
    assert result.exit_code == 0, result.stdout
    warnings = json.loads(result.stdout)[0]["warnings"]
    assert warnings[:2] == [
        "t: {{inputs.my_name}} gets no variable, because $INPUT_MY_NAME holds "
        "another value; rename it to read it safely",
        "t: {{inputs.report_file}} gets no variable, because $INPUT_REPORT_FILE "
        "holds another value; rename it to read it safely",
    ]
    assert warnings[2].startswith('t: use "$INPUT_MY_NAME" instead of {{inputs.my-name}}')


def test_check_warns_when_a_shell_step_reads_a_variable_it_never_gets(workdir):
    """$LOOP_* off a loop and $OUTPUT_<task> off the chain are empty: warn."""
    import json

    _write_conduit(
        workdir,
        "unset",
        "name: unset\ndescription: d\ninputs:\n  token:\n    description: t\n"
        "    default: ''\n"
        "tasks:\n"
        "  - name: plan\n    description: p\n    tool: tool:bash\n    task: echo P\n"
        "  - name: show\n    description: s\n    tool: tool:bash\n"
        "    task: 'echo \"$LOOP_PREVIOUS\" \"$OUTPUT_PLAN\" \"$OUTPUT_DIR\" \"$LOOP_COUNT\"'\n"
        "  - name: fine\n    description: f\n    tool: tool:bash\n    repeat: 2\n"
        "    depends_on: [plan]\n"
        "    task: 'echo \"$OUTPUT_PLAN\" \"${OUTPUT_PLAN_FILE}\" \"$LOOP_ITERATION\"'\n"
        "  - name: loop\n    description: l\n    tool: tool:conduit\n    repeat: 2\n"
        "    depends_on: [plan]\n"
        "    tasks:\n"
        "      - name: test\n        description: t\n        tool: tool:bash\n"
        "        task: 'echo \"$LOOP_PREVIOUS\" \"${OUTPUT_PLAN_FILE}\" \"$OUTPUT_PLAN\" "
        "\"$INPUT_TOKEN\" \"$LOOP_ITERATION\"'\n",
    )
    result = CliRunner().invoke(app, ["check", "unset", "--json"])
    assert result.exit_code == 0, result.stdout
    assert json.loads(result.stdout)[0]["warnings"] == [
        "show: $LOOP_PREVIOUS is empty here: this step does not loop (repeat is 1)",
        "show: $OUTPUT_PLAN is empty here: plan is not in its depends_on chain; "
        "add it as a dependency",
        "loop.test: $LOOP_PREVIOUS is empty here: this step does not loop "
        "(repeat is 1); a loop body gets only what loop forwards, so add "
        "previous: '{{loop.previous}}' under its inputs: and read $INPUT_PREVIOUS",
        "loop.test: $OUTPUT_PLAN_FILE is empty here: plan is a task of the "
        "enclosing conduit; add plan: '{{plan.output}}' under loop's inputs: "
        "and read $INPUT_PLAN",
        "loop.test: $INPUT_TOKEN comes from the environment, not from input "
        "token, because this inline body gets only what its call forwards; to "
        "use the input, add token: '{{inputs.token}}' under loop's inputs:",
        "loop.test: $LOOP_ITERATION is empty here: this step does not loop "
        "(repeat is 1)",
    ]
