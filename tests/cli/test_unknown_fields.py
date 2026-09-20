"""CLI tests: a misspelled conduit field fails before it changes the run.

`depend_on:` used to load as a task with no dependencies, so the checker, the
plan and the run all agreed with a workflow the author never wrote. These go
through the real CLI, engine and store: the only fix applied between the
failing and the passing half is the spelling.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from typer.testing import CliRunner

from flow_atelier.cli import app
from flow_atelier.core.atelier import Atelier

# `consume` can only read the sentinel if it really runs after `prepare`, so
# the ordering claim is checked by the filesystem, not by the plan's wording.
TYPO_DEMO = """\
name: typo_demo
description: two tasks, one misspelled control
tasks:
  - name: prepare
    description: write the sentinel
    task: "printf 'prepared\\n' > sentinel.txt"
    tool: tool:bash
  - name: consume
    description: read the sentinel back
    task: "cat sentinel.txt"
    tool: tool:bash
    {dep}: [prepare]
"""

OK_BASH = (
    "name: {name}\ndescription: d\n"
    "tasks:\n  - name: a\n    description: a\n    task: echo hi\n    tool: tool:bash\n"
)


@pytest.fixture
def workdir(tmp_path, monkeypatch):
    """Isolated cwd with empty project and global conduit stores.

    :param tmp_path: pytest temp directory fixture.
    :param monkeypatch: pytest monkeypatch fixture.
    :returns: the working directory path.
    """
    (tmp_path / ".atelier" / "conduits").mkdir(parents=True)
    (tmp_path / "global" / "conduits").mkdir(parents=True)
    for key in list(os.environ):
        if key.startswith("ATELIER_"):
            monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("ATELIER_GLOBAL_ATELIER_DIR", str(tmp_path / "global"))
    monkeypatch.setenv("ATELIER_NO_UPDATE_CHECK", "1")
    monkeypatch.chdir(tmp_path)
    return tmp_path


def _write(workdir: Path, name: str, body: str, source: str = "project") -> Path:
    """Write `<name>/conduit.yaml` into the project or the global store.

    :param workdir: working directory path.
    :param name: conduit name, which is also the folder name.
    :param body: the YAML document.
    :param source: ``project`` or ``global``.
    :returns: the path written.
    """
    root = workdir / ".atelier" if source == "project" else workdir / "global"
    cdir = root / "conduits" / name
    cdir.mkdir(parents=True, exist_ok=True)
    path = cdir / "conduit.yaml"
    path.write_text(body)
    return path


def _flow_id(output: str) -> str:
    """Extract the flow id `atelier run` printed.

    :param output: captured CLI output.
    :returns: the flow id.
    """
    for line in output.splitlines():
        if "flow_id:" in line:
            return line.split("flow_id:", 1)[1].strip()
    raise AssertionError(f"no flow_id in output:\n{output}")


def test_a_misspelled_control_fails_the_check_and_names_the_field(workdir):
    """Plain checking says FAIL and points at the key to repair."""
    _write(workdir, "typo_demo", TYPO_DEMO.format(dep="depend_on"))

    result = CliRunner().invoke(app, ["check", "typo_demo"])

    assert result.exit_code == 1, result.output
    assert "FAIL" in result.stdout
    assert "depend_on" in result.stdout
    assert "Traceback" not in result.output


def test_the_json_check_carries_the_field_and_the_file_to_repair(workdir):
    """The machine-readable row keeps its shape and locates the typo."""
    path = _write(workdir, "typo_demo", TYPO_DEMO.format(dep="depend_on"))

    result = CliRunner().invoke(app, ["check", "typo_demo", "--json"])
    rows = json.loads(result.stdout)

    assert result.exit_code == 1
    assert set(rows[0]) == {"name", "source", "path", "ok", "error", "required_inputs"}
    assert rows[0]["ok"] is False
    assert "tasks[1].depend_on" in rows[0]["error"]
    assert rows[0]["path"] == str(path.absolute())


def test_run_refuses_the_definition_before_it_executes_anything(workdir):
    """No sentinel, no saved flow: the run stops at the load."""
    _write(workdir, "typo_demo", TYPO_DEMO.format(dep="depend_on"))

    result = CliRunner().invoke(app, ["run", "typo_demo"])

    assert result.exit_code != 0
    assert "depend_on" in result.output
    assert not (workdir / "sentinel.txt").exists()
    assert Atelier().list_flows() == []


def test_repairing_only_the_spelling_restores_the_intended_ordering(workdir):
    """The same file, one key fixed: it checks, plans in waves and runs."""
    path = _write(workdir, "typo_demo", TYPO_DEMO.format(dep="depend_on"))
    broken = path.read_text()
    path.write_text(broken.replace("depend_on:", "depends_on:"))
    runner = CliRunner()

    assert runner.invoke(app, ["check", "typo_demo"]).exit_code == 0

    plan = runner.invoke(app, ["plan", "typo_demo"])
    assert plan.exit_code == 0, plan.output
    assert "Wave 1" in plan.stdout

    run = runner.invoke(app, ["run", "typo_demo"])
    assert run.exit_code == 0, run.output
    assert (workdir / "sentinel.txt").read_text() == "prepared\n"

    # The ordering is what makes `cat` succeed, and the saved log is where an
    # agent reads the result back from.
    logs = Atelier().store.read_logs(_flow_id(run.output))
    consumed = [e for e in logs if e.task == "consume"]
    assert [e.exit_code for e in consumed] == [0]
    assert "prepared" in consumed[0].output


def test_checking_and_running_never_rewrite_the_source(workdir):
    """Nothing repairs the spelling for the author, on either path."""
    path = _write(workdir, "typo_demo", TYPO_DEMO.format(dep="depend_on"))
    before = path.read_bytes()
    runner = CliRunner()

    runner.invoke(app, ["check", "typo_demo"])
    runner.invoke(app, ["check", "typo_demo", "--json"])
    runner.invoke(app, ["run", "typo_demo"])

    assert path.read_bytes() == before


def test_a_rejected_recipe_does_not_hide_the_valid_ones_after_it(workdir):
    """A batch check still reports every conduit it was asked about."""
    _write(workdir, "aaa_typo", TYPO_DEMO.format(dep="depend_on").replace(
        "name: typo_demo", "name: aaa_typo"
    ))
    _write(workdir, "zzz-fine", OK_BASH.format(name="zzz-fine"))

    result = CliRunner().invoke(app, ["check", "--json"])
    rows = json.loads(result.stdout)

    assert result.exit_code == 1
    assert [(r["name"], r["ok"]) for r in rows] == [
        ("aaa_typo", False), ("zzz-fine", True)
    ]


def test_a_rejected_project_copy_is_not_swapped_for_the_global_one(workdir):
    """Shadowing is reported as it runs: the project copy, typo and all."""
    broken = _write(workdir, "typo_demo", TYPO_DEMO.format(dep="depend_on"))
    _write(
        workdir,
        "typo_demo",
        TYPO_DEMO.format(dep="depends_on"),
        source="global",
    )

    result = CliRunner().invoke(app, ["check", "typo_demo", "--json"])
    rows = json.loads(result.stdout)

    assert result.exit_code == 1
    assert rows[0]["source"] == "project"
    assert rows[0]["path"] == str(broken.absolute())
    assert "depend_on" in rows[0]["error"]


def test_recursive_checking_names_the_unknown_field_in_a_child(workdir):
    """A parent fails here, with the call chain and the child's own file."""
    _write(
        workdir,
        "parent",
        "name: parent\ndescription: d\ntasks:\n"
        "  - name: call\n    description: d\n    task: typo_demo\n"
        "    tool: tool:conduit\n",
    )
    child = _write(workdir, "typo_demo", TYPO_DEMO.format(dep="depend_on"))

    result = CliRunner().invoke(app, ["check", "parent", "--recursive", "--json"])
    rows = json.loads(result.stdout)
    error = next(r for r in rows if r["name"] == "parent")["error"]

    assert result.exit_code == 1
    assert "parent.call -> typo_demo" in error
    assert str(child.absolute()) in error
    assert "tasks[1].depend_on" in error


# --- the published example ---------------------------------------------------

_README = Path(__file__).resolve().parents[2] / "README.md"


def _readme_typo_demo() -> str:
    """Return the README's `typo_demo` YAML block, exactly as published.

    :returns: the block's text.
    :raises AssertionError: if the README no longer carries it.
    """
    text = _README.read_text(encoding="utf-8")
    for block in text.split("```yaml\n")[1:]:
        body = block.split("```")[0]
        if body.startswith("name: typo_demo\n"):
            return body
    raise AssertionError("README no longer publishes the typo_demo example")


def test_the_readme_example_fails_exactly_as_the_readme_says(workdir):
    """The documented FAIL line is the one the checker actually prints."""
    published = _readme_typo_demo()
    _write(workdir, "typo_demo", published)

    result = CliRunner().invoke(app, ["check", "typo_demo"])

    assert result.exit_code == 1
    documented = (
        "typo_demo [project] — FAIL: "
        "tasks[1].depend_on: Extra inputs are not permitted"
    )
    assert documented in _README.read_text(encoding="utf-8")
    assert documented in " ".join(result.stdout.split())


def test_the_readme_repair_produces_the_second_wave_it_promises(workdir):
    """Changing only `depend_on` to `depends_on` is the whole fix."""
    repaired = _readme_typo_demo().replace("depend_on:", "depends_on:")
    path = _write(workdir, "typo_demo", repaired)
    runner = CliRunner()

    assert runner.invoke(app, ["check", "typo_demo"]).exit_code == 0
    plan = runner.invoke(app, ["plan", "typo_demo"])
    assert "Wave 1" in plan.stdout
    run = runner.invoke(app, ["run", "typo_demo"])

    assert run.exit_code == 0, run.output
    assert (workdir / "sentinel.txt").read_text() == "prepared\n"
    # The published file, with one key spelled correctly, and nothing else.
    assert path.read_text() == repaired
