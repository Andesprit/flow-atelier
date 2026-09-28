"""Errors from the loop/agent/graph starter must identify a repair in one check."""

from __future__ import annotations

import json

import pytest
import yaml

from tests.cli.test_agent_binding_workflow import Project


@pytest.mark.parametrize(
    ("mistake", "task", "fix"),
    [
        ("conduit_description", "ship", "description:"),
        ("body_description", "ship.fix_until_green.fix", "description:"),
        ("depends_on_spelling", "ship.fix_until_green", "depends_on"),
        ("until_plain", "ship.fix_until_green", "until: output.match(TESTS PASSED)"),
        ("until_contains", "ship.fix_until_green", "until: output.match(TESTS PASSED)"),
        ("harness_typo", "ship.plan", "harness:claude-code"),
        ("tool_bash", "ship.plan", "tool:bash"),
        ("on_exhaust_value", "ship.fix_until_green", "on_exhaust: fail"),
        ("body_parent_ref", "ship.fix_until_green.fix", "{{inputs.plan}}"),
        ("unforwarded_input", "ship.fix_until_green", "test_command:"),
        ("loop_previous_outside", "ship.plan", "repeat"),
        ("on_exhaust_without_until", "ship.fix_until_green", "until: output.match"),
        ("depends_on_task_typo", "ship.review_correctness", "review_maintainability"),
    ],
)
def test_starter_mistakes_point_to_line_task_and_fix(tmp_path, mistake, task, fix):
    project = Project(tmp_path)
    assert project.cli("create", "ship", "--template", "fix-loop").returncode == 0
    path = project.work / ".atelier/conduits/ship/conduit.yaml"
    recipe = yaml.safe_load(path.read_text())
    tasks = {next(iter(item)): next(iter(item.values())) for item in recipe["tasks"]}
    loop = tasks["fix_until_green"]
    body = {next(iter(item)): next(iter(item.values())) for item in loop["tasks"]}
    if mistake == "conduit_description":
        del recipe["description"]
    elif mistake == "body_description":
        del body["fix"]["description"]
    elif mistake == "depends_on_spelling":
        loop["depends-on"] = loop.pop("depends_on")
    elif mistake == "until_plain":
        loop["until"] = "TESTS PASSED"
    elif mistake == "until_contains":
        loop["until"] = "output.contains(TESTS PASSED)"
    elif mistake == "harness_typo":
        tasks["plan"]["tool"] = "harness:claude-cod"
    elif mistake == "tool_bash":
        tasks["plan"]["tool"] = "bash"
    elif mistake == "on_exhaust_value":
        loop["on_exhaust"] = "error"
    elif mistake == "body_parent_ref":
        body["fix"]["task"] = "{{plan.output}}"
    elif mistake == "unforwarded_input":
        del loop["inputs"]["test_command"]
    elif mistake == "loop_previous_outside":
        tasks["plan"]["task"] = "{{loop.previous}}"
    elif mistake == "on_exhaust_without_until":
        del loop["until"]
    elif mistake == "depends_on_task_typo":
        tasks["review_correctness"]["depends_on"] = ["review_maintainabilty"]
    path.write_text(yaml.safe_dump(recipe, sort_keys=False))

    checked = project.cli("check", "ship", "--recursive", "--json")
    assert checked.returncode == 1, checked.stdout + checked.stderr
    row = json.loads(checked.stdout)[0]
    message = row["error"]
    assert "conduit.yaml:" in message
    assert task in message
    assert fix in message
    assert "Value error," not in message
    assert "Extra inputs are not permitted" not in message
    if mistake == "harness_typo":
        assert "atelier harness list" in message


def test_multiple_schema_errors_are_all_reported_in_original_json_shape(tmp_path):
    project = Project(tmp_path)
    assert project.cli("create", "ship", "--template", "fix-loop").returncode == 0
    path = project.work / ".atelier/conduits/ship/conduit.yaml"
    recipe = yaml.safe_load(path.read_text())
    del recipe["description"]
    loop = recipe["tasks"][1]["fix_until_green"]
    loop["depends-on"] = loop.pop("depends_on")
    path.write_text(yaml.safe_dump(recipe, sort_keys=False))
    checked = project.cli("check", "ship", "--recursive", "--json")
    assert checked.returncode == 1
    row = json.loads(checked.stdout)[0]
    assert set(row) == {"name", "source", "path", "ok", "error", "required_inputs"}
    assert "ship: missing description" in row["error"]
    assert "ship.fix_until_green: unknown key 'depends-on'" in row["error"]
    assert row["error"].count("conduit.yaml:") == 2


def test_defaulted_child_input_need_not_be_forwarded(tmp_path):
    project = Project(tmp_path)
    assert project.cli("create", "ship", "--template", "fix-loop").returncode == 0
    path = project.work / ".atelier/conduits/ship/conduit.yaml"
    recipe = yaml.safe_load(path.read_text())
    loop = recipe["tasks"][1]["fix_until_green"]
    del loop["inputs"]["test_command"]
    loop["inputs"].clear()
    # A named child can provide its own default even if the call omits it.
    loop.pop("tasks")
    loop["task"] = "child"
    child = project.work / ".atelier/conduits/child/conduit.yaml"
    child.parent.mkdir(parents=True)
    child_recipe = {
        "name": "child",
        "description": "d",
        "inputs": {"test_command": {"description": "d"}},
        "tasks": [
            {"test": {"description": "d", "tool": "tool:bash", "task": "{{inputs.test_command}}"}}
        ],
    }
    child.write_text(yaml.safe_dump(child_recipe, sort_keys=False))
    path.write_text(yaml.safe_dump(recipe, sort_keys=False))
    missing = project.cli("check", "ship", "--recursive", "--json")
    assert missing.returncode == 1
    error = json.loads(missing.stdout)[0]["error"]
    assert "conduit.yaml:" in error
    assert "ship.fix_until_green" in error
    assert "test_command: '{{inputs.test_command}}'" in error
    child_recipe["inputs"]["test_command"]["default"] = "echo OK"
    child.write_text(yaml.safe_dump(child_recipe, sort_keys=False))
    checked = project.cli("check", "ship", "--recursive")
    assert checked.returncode == 0, checked.stdout + checked.stderr


def test_malformed_yaml_still_names_its_line(tmp_path):
    project = Project(tmp_path)
    assert project.cli("create", "ship", "--template", "fix-loop").returncode == 0
    path = project.work / ".atelier/conduits/ship/conduit.yaml"
    path.write_text(path.read_text() + "\nbad: [unclosed\n")
    checked = project.cli("check", "ship", "--recursive")
    assert checked.returncode == 1
    assert "invalid YAML" in checked.stdout
    assert "line" in checked.stdout
