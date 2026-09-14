"""CLI smoke tests via Typer's CliRunner."""
import io
import os
import sys
from datetime import UTC, datetime

import pytest
from rich.console import Console
from typer.testing import CliRunner

from flow_atelier.cli import app
from flow_atelier.cli.rendering.render import render_task_event
from flow_atelier.schemas.log import TaskEvent

CONDUIT_YAML = """
name: hello
description: Say hello
tasks:
  - greet:
      description: greet
      task: "echo hello {{inputs.name}}"
      tool: tool:bash
      depends_on: []
"""


@pytest.fixture
def workdir(tmp_path, monkeypatch):
    """Provide an isolated working directory seeded with the hello conduit.

    :param tmp_path: pytest temp directory fixture.
    :param monkeypatch: pytest monkeypatch fixture.
    """
    atelier_dir = tmp_path / ".atelier"
    (atelier_dir / "conduits" / "hello").mkdir(parents=True)
    (atelier_dir / "conduits" / "hello" / "conduit.yaml").write_text(CONDUIT_YAML)
    monkeypatch.chdir(tmp_path)
    for k in list(os.environ):
        if k.startswith("ATELIER_") and k not in (
            "ATELIER_GLOBAL_ATELIER_DIR",
            "ATELIER_NO_UPDATE_CHECK",
        ):
            monkeypatch.delenv(k, raising=False)
    return tmp_path


def test_list_conduits(workdir):
    """Verify `list conduits` shows the project conduit and table columns.

    :param workdir: isolated working directory fixture.
    """
    runner = CliRunner()
    result = runner.invoke(app, ["list", "conduits"])
    assert result.exit_code == 0, result.output
    assert "hello" in result.output
    # New table includes the source tag and column headers.
    assert "project" in result.output
    assert "name" in result.output
    assert "tasks" in result.output
    assert "inputs" in result.output


GLOBAL_DEPLOY_YAML = """
name: deploy
description: Global deploy
tasks:
  - step:
      description: step
      task: "echo deploying"
      tool: tool:bash
      depends_on: []
"""


def test_list_conduits_shows_global_and_shadowing(
    workdir, _isolate_global_atelier_dir
):
    """Verify global conduits appear and project conduits shadow them.

    :param workdir: isolated working directory fixture.
    :param _isolate_global_atelier_dir: isolated global atelier dir fixture.
    """
    global_dir = _isolate_global_atelier_dir
    (global_dir / "conduits" / "deploy").mkdir(parents=True)
    (global_dir / "conduits" / "deploy" / "conduit.yaml").write_text(
        GLOBAL_DEPLOY_YAML
    )
    # a global "hello" that should be shadowed by the project copy from workdir
    (global_dir / "conduits" / "hello").mkdir(parents=True)
    (global_dir / "conduits" / "hello" / "conduit.yaml").write_text(
        GLOBAL_DEPLOY_YAML.replace("deploy", "hello")
    )

    runner = CliRunner()
    result = runner.invoke(app, ["list", "conduits"])
    assert result.exit_code == 0, result.output
    out = result.output
    assert "deploy" in out and "global" in out
    assert "hello" in out and "project" in out
    # hello only appears once (shadowed, not duplicated). Match by row,
    # i.e. lines containing the conduit name in the first column.
    name_col_lines = [
        l for l in out.splitlines()
        if l.startswith("│") and "hello" in l.split("│")[1]
    ]
    assert len(name_col_lines) == 1


def test_run_and_status(workdir):
    """Verify `run` succeeds and `status` reports completion for the flow.

    :param workdir: isolated working directory fixture.
    """
    runner = CliRunner()
    result = runner.invoke(app, ["run", "hello", "--input", "name=world"])
    assert result.exit_code == 0, result.output
    assert "flow_id" in result.output
    # Live output is on by default: the greet task should show up.
    assert "greet" in result.output
    assert "tool:bash" in result.output
    # extract flow id
    line = [l for l in result.output.splitlines() if "flow_id" in l][0]
    flow_id = line.split()[-1]
    result2 = runner.invoke(app, ["status", flow_id])
    assert result2.exit_code == 0
    assert "greet" in result2.output
    assert "completed" in result2.output


GATED_BRACKET_YAML = """
name: gated
description: d
tasks:
  - gate:
      description: d
      task: "echo lowercase"
      tool: tool:bash
      depends_on: []
  - work:
      description: d
      task: "echo work"
      tool: tool:bash
      depends_on:
        - "gate.output.match([A-Z]+)"
"""


def test_status_escapes_bracketed_skip_reason(workdir, monkeypatch):
    """`status` renders a bracketed conditional skip reason literally, not markup.

    A conditional dependency whose regex contains a character class like
    ``[A-Z]`` makes the engine record a skip reason carrying those brackets.
    Rich would read ``[A-Z]`` as a style tag and crash the table render unless
    the cell is escaped.

    :param workdir: isolated working directory fixture.
    :param monkeypatch: pytest monkeypatch fixture.
    """
    monkeypatch.setenv("COLUMNS", "200")
    d = workdir / ".atelier" / "conduits" / "gated"
    d.mkdir(parents=True)
    (d / "conduit.yaml").write_text(GATED_BRACKET_YAML)
    runner = CliRunner()
    run_result = runner.invoke(app, ["run", "gated"])
    assert run_result.exit_code == 0, run_result.output
    line = [l for l in run_result.output.splitlines() if "flow_id" in l][0]
    flow_id = line.split()[-1]
    result = runner.invoke(app, ["status", flow_id])
    assert result.exit_code == 0, result.output
    assert result.exception is None
    assert "[A-Z]" in result.output


def test_version_flag():
    """Verify --version prints the package version and exits 0."""
    runner = CliRunner()
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0
    assert "flow-atelier" in result.output


def test_run_unknown_conduit_exits_cleanly(workdir):
    """Verify `run <typo>` prints a friendly error instead of a traceback.

    :param workdir: isolated working directory fixture.
    """
    runner = CliRunner()
    result = runner.invoke(app, ["run", "no-such-conduit"])
    assert result.exit_code == 1
    assert "unknown conduit" in result.output
    assert "no-such-conduit" in result.output
    assert "Traceback" not in result.output


def test_list_flows(workdir):
    """Verify `list flows` shows the flow id and status columns.

    :param workdir: isolated working directory fixture.
    """
    runner = CliRunner()
    runner.invoke(app, ["run", "hello", "--input", "name=a"])
    result = runner.invoke(app, ["list", "flows"])
    assert result.exit_code == 0
    assert "_hello" in result.output
    # New table-based output includes per-flow status and conduit columns.
    assert "status" in result.output
    assert "completed" in result.output
    assert "duration" in result.output


def test_status_includes_duration_and_summary(workdir):
    """Verify `status` output includes timing and a per-task summary glyph.

    :param workdir: isolated working directory fixture.
    """
    runner = CliRunner()
    run_result = runner.invoke(app, ["run", "hello", "--input", "name=a"])
    line = [l for l in run_result.output.splitlines() if "flow_id" in l][0]
    flow_id = line.split()[-1]
    result = runner.invoke(app, ["status", flow_id])
    assert result.exit_code == 0
    assert "started=" in result.output
    assert "duration=" in result.output
    # Aggregate summary uses ✓ glyph for the completed task.
    assert "✓" in result.output


def test_run_prints_summary_footer(workdir):
    """Verify `run` prints a totals footer line.

    :param workdir: isolated working directory fixture.
    """
    runner = CliRunner()
    result = runner.invoke(app, ["run", "hello", "--input", "name=a"])
    assert result.exit_code == 0, result.output
    # Footer line: glyph(s) + total duration.
    assert "✓1" in result.output
    assert "total" in result.output


# ---------------------------------------------------------------- logs cmd


MULTI_CONDUIT_YAML = """
name: multi
description: multi-task
tasks:
  - alpha:
      description: a
      task: "echo alpha-output; echo alpha-err >&2"
      tool: tool:bash
      depends_on: []
  - beta:
      description: b
      task: "echo beta-output"
      tool: tool:bash
      depends_on: [alpha]
"""


def _write_multi(workdir):
    """Write the multi-task conduit fixture into the working directory.

    :param workdir: working directory path.
    """
    d = workdir / ".atelier" / "conduits" / "multi"
    d.mkdir(parents=True)
    (d / "conduit.yaml").write_text(MULTI_CONDUIT_YAML)


def _run_and_id(runner, conduit, *args):
    """Invoke `run` and return the resulting flow id from the output.

    :param runner: Typer CliRunner instance.
    :param conduit: conduit name to run.
    :param args: extra CLI arguments forwarded to `run`.
    """
    res = runner.invoke(app, ["run", conduit, *args])
    line = [l for l in res.output.splitlines() if "flow_id" in l][0]
    return line.split()[-1]


def test_logs_unknown_flow(workdir):
    """Verify `logs` errors on an unknown flow id.

    :param workdir: isolated working directory fixture.
    """
    runner = CliRunner()
    result = runner.invoke(app, ["logs", "no_such_flow"])
    assert result.exit_code != 0
    assert "unknown flow" in result.output


@pytest.mark.skipif(sys.platform == "win32", reason="bash ; syntax in conduit YAML")
def test_logs_shows_task_output(workdir):
    """Verify `logs` prints stdout for every task in the flow.

    :param workdir: isolated working directory fixture.
    """
    _write_multi(workdir)
    runner = CliRunner()
    flow_id = _run_and_id(runner, "multi")
    result = runner.invoke(app, ["logs", flow_id])
    assert result.exit_code == 0, result.output
    assert "alpha" in result.output
    assert "beta" in result.output
    assert "alpha-output" in result.output
    assert "beta-output" in result.output


@pytest.mark.skipif(sys.platform == "win32", reason="bash ; syntax in conduit YAML")
def test_logs_filter_by_task(workdir):
    """Verify `logs --task` filters output to the named task only.

    :param workdir: isolated working directory fixture.
    """
    _write_multi(workdir)
    runner = CliRunner()
    flow_id = _run_and_id(runner, "multi")
    result = runner.invoke(app, ["logs", flow_id, "--task", "alpha"])
    assert result.exit_code == 0, result.output
    assert "alpha-output" in result.output
    assert "beta-output" not in result.output


@pytest.mark.skipif(sys.platform == "win32", reason="bash ; syntax in conduit YAML")
def test_logs_show_stderr(workdir):
    """Verify `logs --show stderr` shows stderr and hides stdout.

    :param workdir: isolated working directory fixture.
    """
    _write_multi(workdir)
    runner = CliRunner()
    flow_id = _run_and_id(runner, "multi")
    result = runner.invoke(app, ["logs", flow_id, "--task", "alpha", "--show", "stderr"])
    assert result.exit_code == 0, result.output
    assert "alpha-err" in result.output
    # stdout body should be omitted in stderr-only mode.
    assert "alpha-output" not in result.output


def test_logs_unknown_task_filter(workdir):
    """Verify `logs --task` errors when the task name is unknown.

    :param workdir: isolated working directory fixture.
    """
    _write_multi(workdir)
    runner = CliRunner()
    flow_id = _run_and_id(runner, "multi")
    result = runner.invoke(app, ["logs", flow_id, "--task", "ghost"])
    assert result.exit_code != 0
    assert "no log entries" in result.output


@pytest.mark.skipif(sys.platform == "win32", reason="bash ; syntax in conduit YAML")
def test_logs_last_zero_shows_nothing(workdir):
    """`logs --last 0` should render no entries (and `[]` in json), not all.

    :param workdir: isolated working directory fixture.
    """
    _write_multi(workdir)
    runner = CliRunner()
    flow_id = _run_and_id(runner, "multi")
    result = runner.invoke(app, ["logs", flow_id, "--last", "0"])
    assert result.exit_code == 0, result.output
    assert "alpha-output" not in result.output
    assert "beta-output" not in result.output
    result_json = runner.invoke(app, ["logs", flow_id, "--last", "0", "--json"])
    assert result_json.exit_code == 0, result_json.output
    assert result_json.output.strip() == "[]"


@pytest.mark.skipif(sys.platform == "win32", reason="bash ; syntax in conduit YAML")
def test_logs_last_negative_is_rejected(workdir):
    """`logs --last -1` should fail with a parameter error, not show everything.

    :param workdir: isolated working directory fixture.
    """
    _write_multi(workdir)
    runner = CliRunner()
    flow_id = _run_and_id(runner, "multi")
    result = runner.invoke(app, ["logs", flow_id, "--last", "-1"])
    assert result.exit_code != 0
    assert "alpha-output" not in result.output


# ---------------------------------------------------------------- prefix match


def test_status_accepts_short_prefix(workdir):
    """git-style: a unique short prefix should resolve to the full flow id.

    :param workdir: isolated working directory fixture.
    """
    runner = CliRunner()
    flow_id = _run_and_id(runner, "hello", "--input", "name=a")
    short = flow_id[: len(flow_id.split("_")[0]) + 5]  # conduit + 4 hex chars
    result = runner.invoke(app, ["status", short])
    assert result.exit_code == 0, result.output
    assert "completed" in result.output


def test_logs_accepts_short_prefix(workdir):
    """Verify `logs` resolves a unique short flow-id prefix.

    :param workdir: isolated working directory fixture.
    """
    runner = CliRunner()
    flow_id = _run_and_id(runner, "hello", "--input", "name=a")
    short = flow_id[: len(flow_id.split("_")[0]) + 5]
    result = runner.invoke(app, ["logs", short])
    assert result.exit_code == 0, result.output
    assert "greet" in result.output


def test_status_ambiguous_prefix(workdir):
    """Verify `status` errors when the flow-id prefix is ambiguous.

    :param workdir: isolated working directory fixture.
    """
    _write_multi(workdir)
    runner = CliRunner()
    # Two flows on the same UTC date → the date prefix is ambiguous.
    _run_and_id(runner, "multi")
    _run_and_id(runner, "multi")
    today = datetime.now(UTC).strftime("%Y%m%d")
    result = runner.invoke(app, ["status", today])
    assert result.exit_code != 0
    assert "ambiguous" in result.output.lower()


def test_status_prefix_no_match(workdir):
    """Verify `status` errors when no flow id matches the prefix.

    :param workdir: isolated working directory fixture.
    """
    runner = CliRunner()
    _run_and_id(runner, "hello", "--input", "name=a")
    result = runner.invoke(app, ["status", "nope_"])
    assert result.exit_code != 0
    assert "unknown flow" in result.output


# ---------------------------------------------------------------- --json mode


import json as _json


def test_list_flows_json(workdir):
    """Verify `list flows --json` returns the expected schema.

    :param workdir: isolated working directory fixture.
    """
    runner = CliRunner()
    _run_and_id(runner, "hello", "--input", "name=a")
    result = runner.invoke(app, ["list", "flows", "--json"])
    assert result.exit_code == 0, result.output
    data = _json.loads(result.output)
    assert isinstance(data, list)
    assert len(data) == 1
    entry = data[0]
    for key in ("flow_id", "conduit", "status", "started_at", "finished_at",
                "duration_seconds", "task_counts"):
        assert key in entry
    assert entry["conduit"] == "hello"
    assert entry["status"] == "completed"
    assert entry["task_counts"]["completed"] == 1


def test_list_conduits_json(workdir):
    """Verify `list conduits --json` includes source and task counts.

    :param workdir: isolated working directory fixture.
    """
    runner = CliRunner()
    result = runner.invoke(app, ["list", "conduits", "--json"])
    assert result.exit_code == 0, result.output
    data = _json.loads(result.output)
    assert isinstance(data, list)
    by_name = {e["name"]: e for e in data}
    assert "hello" in by_name
    assert by_name["hello"]["source"] == "project"
    assert by_name["hello"]["tasks"] == 1


def test_status_json(workdir):
    """Verify `status --json` returns task-level status info.

    :param workdir: isolated working directory fixture.
    """
    runner = CliRunner()
    flow_id = _run_and_id(runner, "hello", "--input", "name=a")
    result = runner.invoke(app, ["status", flow_id, "--json"])
    assert result.exit_code == 0, result.output
    data = _json.loads(result.output)
    assert data["flow_id"] == flow_id
    assert data["status"] == "completed"
    assert "tasks" in data and "greet" in data["tasks"]
    assert data["tasks"]["greet"]["status"] == "completed"


@pytest.mark.skipif(sys.platform == "win32", reason="bash ; syntax in conduit YAML")
def test_logs_json(workdir):
    """Verify `logs --json` returns one entry per task with output.

    :param workdir: isolated working directory fixture.
    """
    _write_multi(workdir)
    runner = CliRunner()
    flow_id = _run_and_id(runner, "multi")
    result = runner.invoke(app, ["logs", flow_id, "--json"])
    assert result.exit_code == 0, result.output
    data = _json.loads(result.output)
    assert isinstance(data, list)
    tasks = [e["task"] for e in data]
    assert "alpha" in tasks and "beta" in tasks
    alpha = next(e for e in data if e["task"] == "alpha")
    assert "alpha-output" in alpha["output"]


# ---------------------------------------------------------------- outputs cmd


@pytest.mark.skipif(sys.platform == "win32", reason="bash ; syntax in conduit YAML")
def test_outputs_human(workdir):
    """Verify `outputs` prints each task's saved result.

    :param workdir: isolated working directory fixture.
    """
    runner = CliRunner()
    flow_id = _run_and_id(runner, "hello", "--input", "name=a")
    result = runner.invoke(app, ["outputs", flow_id])
    assert result.exit_code == 0, result.output
    assert "greet" in result.output
    assert "hello a" in result.output


@pytest.mark.skipif(sys.platform == "win32", reason="bash ; syntax in conduit YAML")
def test_outputs_json(workdir):
    """Verify `outputs --json` returns the per-task result map.

    :param workdir: isolated working directory fixture.
    """
    _write_multi(workdir)
    runner = CliRunner()
    flow_id = _run_and_id(runner, "multi")
    result = runner.invoke(app, ["outputs", flow_id, "--json"])
    assert result.exit_code == 0, result.output
    data = _json.loads(result.output)
    assert isinstance(data, dict)
    assert "alpha-output" in data["alpha"]
    assert "beta-output" in data["beta"]


@pytest.mark.skipif(sys.platform == "win32", reason="bash ; syntax in conduit YAML")
def test_outputs_single_task(workdir):
    """Verify `outputs --task` prints only the named task's raw output.

    :param workdir: isolated working directory fixture.
    """
    _write_multi(workdir)
    runner = CliRunner()
    flow_id = _run_and_id(runner, "multi")
    result = runner.invoke(app, ["outputs", flow_id, "--task", "alpha"])
    assert result.exit_code == 0, result.output
    assert "alpha-output" in result.output
    assert "beta-output" not in result.output


@pytest.mark.skipif(sys.platform == "win32", reason="bash ; syntax in conduit YAML")
def test_outputs_unknown_task(workdir):
    """Verify `outputs --task` errors on an unknown task name.

    :param workdir: isolated working directory fixture.
    """
    _write_multi(workdir)
    runner = CliRunner()
    flow_id = _run_and_id(runner, "multi")
    result = runner.invoke(app, ["outputs", flow_id, "--task", "nope"])
    assert result.exit_code != 0
    assert "unknown task" in result.output


def test_outputs_unknown_flow(workdir):
    """Verify `outputs` errors on an unknown flow id.

    :param workdir: isolated working directory fixture.
    """
    runner = CliRunner()
    result = runner.invoke(app, ["outputs", "no_such_flow"])
    assert result.exit_code != 0
    assert "unknown flow" in result.output


# ---------------------------------------------------------------- --follow


def test_logs_follow_on_completed_flow_exits(workdir):
    """--follow on an already-terminal flow must print all entries and exit
    on the first poll iteration (status != running).

    :param workdir: isolated working directory fixture.
    """
    _write_multi(workdir)
    runner = CliRunner()
    flow_id = _run_and_id(runner, "multi")
    result = runner.invoke(app, ["logs", flow_id, "--follow"])
    assert result.exit_code == 0, result.output
    assert "alpha" in result.output
    assert "beta" in result.output


def test_logs_follow_unknown_flow(workdir):
    """Verify `logs --follow` errors on an unknown flow id.

    :param workdir: isolated working directory fixture.
    """
    runner = CliRunner()
    result = runner.invoke(app, ["logs", "no_such_flow", "--follow"])
    assert result.exit_code != 0
    assert "unknown flow" in result.output


def test_run_missing_input_fails(workdir):
    """Verify `run` fails when a required template input is missing.

    :param workdir: isolated working directory fixture.
    """
    runner = CliRunner()
    # conduit has no declared inputs but the task uses {{inputs.name}} — engine
    # does not enforce that unused-declared inputs match, so this command
    # will fail only because the template references a missing input.
    result = runner.invoke(app, ["run", "hello"])
    assert result.exit_code != 0


FAILING_CONDUIT_YAML = """
name: failing
description: Always fails
tasks:
  - boom:
      description: fail on purpose
      task: "echo bye; exit 9"
      tool: tool:bash
      depends_on: []
"""


# ---------------------------------------------------------------- schedule cmds


SCHEDULE_RECURRING_JSON = """{
  "conduit_name": "hello",
  "inputs": {"name": "world"},
  "run_path": ".",
  "schedule": {
    "mode": "recurring",
    "name": "nightly",
    "days": [1, 5],
    "times": ["09:00"]
  }
}"""

SCHEDULE_ONCE_JSON = """{
  "conduit_name": "hello",
  "inputs": {"name": "once"},
  "run_path": ".",
  "schedule": {
    "mode": "once",
    "name": "backfill",
    "run_at": "2099-05-01T09:00:00Z"
  }
}"""


def test_schedule_add_and_list(workdir, tmp_path):
    """Verify `schedule add` installs a recurring schedule and `list` shows it.

    :param workdir: isolated working directory fixture.
    :param tmp_path: pytest temp directory fixture.
    """
    src = tmp_path / "nightly.json"
    src.write_text(SCHEDULE_RECURRING_JSON)
    runner = CliRunner()
    result = runner.invoke(app, ["schedule", "add", str(src)])
    assert result.exit_code == 0, result.output
    assert "installed" in result.output
    assert list((workdir / ".atelier" / "schedules").glob("nightly-*.yaml"))

    listing = runner.invoke(app, ["schedule", "list"])
    assert listing.exit_code == 0, listing.output
    assert "nightly" in listing.output
    assert "hello" in listing.output
    assert "recurring" in listing.output


SCHEDULE_BRACKET_NAME_JSON = """{
  "conduit_name": "hello",
  "inputs": {"name": "world"},
  "run_path": ".",
  "schedule": {
    "mode": "recurring",
    "name": "gate[A-Z]run",
    "days": [1, 5],
    "times": ["09:00"]
  }
}"""


def test_schedule_list_escapes_bracketed_name(workdir, tmp_path, monkeypatch):
    """`schedule list` renders a bracketed schedule name literally, not markup.

    A schedule name is free text; brackets in it would otherwise be read by
    Rich as a style tag and crash the listing table.

    :param workdir: isolated working directory fixture.
    :param tmp_path: pytest temp directory fixture.
    :param monkeypatch: pytest monkeypatch fixture.
    """
    monkeypatch.setenv("COLUMNS", "200")
    src = tmp_path / "bracket.json"
    src.write_text(SCHEDULE_BRACKET_NAME_JSON)
    runner = CliRunner()
    add = runner.invoke(app, ["schedule", "add", str(src)])
    assert add.exit_code == 0, add.output
    listing = runner.invoke(app, ["schedule", "list"])
    assert listing.exit_code == 0, listing.output
    assert listing.exception is None
    assert "[A-Z]" in listing.output


def test_schedule_add_rejects_invalid(workdir, tmp_path):
    """Verify `schedule add` rejects payloads with invalid schedule modes.

    :param workdir: isolated working directory fixture.
    :param tmp_path: pytest temp directory fixture.
    """
    bad = tmp_path / "bad.json"
    bad.write_text(
        '{"conduit_name": "x", "run_path": "/tmp", "schedule": {"mode": "weekly"}}'
    )
    runner = CliRunner()
    result = runner.invoke(app, ["schedule", "add", str(bad)])
    assert result.exit_code != 0
    assert "invalid" in result.output.lower()


def test_schedule_add_rejects_unknown_input(workdir, tmp_path, monkeypatch):
    """Verify `schedule add` refuses an input key the conduit cannot use.

    :param workdir: isolated working directory fixture.
    :param tmp_path: pytest temp directory fixture.
    :param monkeypatch: pytest monkeypatch fixture.
    """
    monkeypatch.setenv("COLUMNS", "200")
    src = tmp_path / "typo.json"
    src.write_text(SCHEDULE_RECURRING_JSON.replace('"name": "world"', '"nmae": "world"'))
    runner = CliRunner()
    result = runner.invoke(app, ["schedule", "add", str(src)])
    assert result.exit_code == 1, result.output
    assert "invalid schedule:" in result.output
    assert "unknown inputs: ['nmae']" in result.output
    assert "did you mean 'name' for 'nmae'?" in result.output
    assert "'hello' accepts inputs: ['name']" in result.output
    assert not (workdir / ".atelier" / "schedules").exists() or not list(
        (workdir / ".atelier" / "schedules").glob("*.yaml")
    )


def test_schedule_remove_by_name(workdir, tmp_path):
    """Verify `schedule remove` deletes the named schedule.

    :param workdir: isolated working directory fixture.
    :param tmp_path: pytest temp directory fixture.
    """
    src = tmp_path / "nightly.json"
    src.write_text(SCHEDULE_RECURRING_JSON)
    runner = CliRunner()
    runner.invoke(app, ["schedule", "add", str(src)])

    result = runner.invoke(app, ["schedule", "remove", "nightly"])
    assert result.exit_code == 0, result.output
    assert "removed" in result.output

    # After removal, it should no longer appear in the active list.
    listing = runner.invoke(app, ["schedule", "list"])
    assert "nightly" not in listing.output


def test_schedule_remove_unknown(workdir):
    """Verify `schedule remove` errors when the name is unknown.

    :param workdir: isolated working directory fixture.
    """
    runner = CliRunner()
    result = runner.invoke(app, ["schedule", "remove", "ghost"])
    assert result.exit_code != 0
    assert "not found" in result.output


def test_schedule_list_empty(workdir):
    """Verify `schedule list` reports an empty schedule store.

    :param workdir: isolated working directory fixture.
    """
    runner = CliRunner()
    result = runner.invoke(app, ["schedule", "list"])
    assert result.exit_code == 0
    assert "no schedules" in result.output


def test_schedule_list_json_includes_one_shot(workdir, tmp_path):
    """Verify `schedule list --json` includes one-shot schedules.

    :param workdir: isolated working directory fixture.
    :param tmp_path: pytest temp directory fixture.
    """
    src = tmp_path / "backfill.json"
    src.write_text(SCHEDULE_ONCE_JSON)
    runner = CliRunner()
    runner.invoke(app, ["schedule", "add", str(src)])

    result = runner.invoke(app, ["schedule", "list", "--json"])
    assert result.exit_code == 0, result.output
    payload = _json.loads(result.output)
    assert "schedules" in payload
    assert len(payload["schedules"]) == 1
    entry = payload["schedules"][0]
    assert entry["name"] == "backfill"
    assert entry["kind"] == "once"


def test_schedule_run_now_by_name(workdir, tmp_path):
    """Verify `schedule run-now <name>` executes the schedule's conduit.

    :param workdir: isolated working directory fixture.
    :param tmp_path: pytest temp directory fixture.
    """
    src = tmp_path / "nightly.json"
    src.write_text(SCHEDULE_RECURRING_JSON)
    runner = CliRunner()
    runner.invoke(app, ["schedule", "add", str(src)])

    result = runner.invoke(app, ["schedule", "run-now", "nightly"])
    assert result.exit_code == 0, result.output
    assert "flow_id" in result.output
    assert "greet" in result.output  # task from the hello conduit


def test_schedule_run_now_unknown(workdir):
    """Verify `schedule run-now` errors when the schedule name is unknown.

    :param workdir: isolated working directory fixture.
    """
    runner = CliRunner()
    result = runner.invoke(app, ["schedule", "run-now", "ghost"])
    assert result.exit_code != 0
    assert "schedule not found" in result.output


def test_scheduler_status_alias(workdir, tmp_path):
    """Verify `scheduler status` is an alias for `schedule list`.

    :param workdir: isolated working directory fixture.
    :param tmp_path: pytest temp directory fixture.
    """
    src = tmp_path / "nightly.json"
    src.write_text(SCHEDULE_RECURRING_JSON)
    runner = CliRunner()
    runner.invoke(app, ["schedule", "add", str(src)])
    result = runner.invoke(app, ["scheduler", "status"])
    assert result.exit_code == 0, result.output
    assert "nightly" in result.output


def _add_schedule_and_record(tmp_path, status, flow_id):
    """Install the recurring schedule and inject one history record.

    Run history is normally written by the daemon's fire path; CLI tests
    bypass the daemon, so we seed a record directly into the same store the
    CLI reads from.

    :param tmp_path: isolated working directory (== cwd in workdir fixture).
    :param status: ``"succeeded"`` or ``"failed"``.
    :param flow_id: flow id to attach to the record.
    :returns: the installed schedule's id.
    """
    from flow_atelier.services.scheduler.store import ScheduleStore

    src = tmp_path / "nightly.json"
    src.write_text(SCHEDULE_RECURRING_JSON)
    runner = CliRunner()
    runner.invoke(app, ["schedule", "add", str(src)])
    store = ScheduleStore(tmp_path / ".atelier")
    job = store.get_by_name("nightly")
    store.append_run_record(job.id, status, flow_id)
    return job.id


def test_schedule_history_empty(workdir, tmp_path):
    """`schedule history` reports a friendly message when there is no history.

    :param workdir: isolated working directory fixture.
    :param tmp_path: pytest temp directory fixture.
    """
    src = tmp_path / "nightly.json"
    src.write_text(SCHEDULE_RECURRING_JSON)
    runner = CliRunner()
    runner.invoke(app, ["schedule", "add", str(src)])
    result = runner.invoke(app, ["schedule", "history", "nightly"])
    assert result.exit_code == 0, result.output
    assert "no recorded runs" in result.output


def test_schedule_history_lists_records(workdir, tmp_path):
    """`schedule history` shows the flow id and status of recorded fires.

    :param workdir: isolated working directory fixture.
    :param tmp_path: pytest temp directory fixture.
    """
    _add_schedule_and_record(tmp_path, "failed", "FLOW-overnight")
    runner = CliRunner()
    result = runner.invoke(app, ["schedule", "history", "nightly"])
    assert result.exit_code == 0, result.output
    assert "FLOW-overnight" in result.output
    assert "FAILED" in result.output


def test_schedule_history_json(workdir, tmp_path):
    """`schedule history --json` emits the records as structured data.

    :param workdir: isolated working directory fixture.
    :param tmp_path: pytest temp directory fixture.
    """
    _add_schedule_and_record(tmp_path, "succeeded", "FLOW-good")
    runner = CliRunner()
    result = runner.invoke(app, ["schedule", "history", "nightly", "--json"])
    assert result.exit_code == 0, result.output
    payload = _json.loads(result.output)
    assert payload["name"] == "nightly"
    assert payload["runs"][0]["flow_id"] == "FLOW-good"
    assert payload["runs"][0]["status"] == "succeeded"


def test_schedule_history_unknown(workdir):
    """`schedule history` errors when the schedule is unknown.

    :param workdir: isolated working directory fixture.
    """
    runner = CliRunner()
    result = runner.invoke(app, ["schedule", "history", "ghost"])
    assert result.exit_code != 0
    assert "schedule not found" in result.output


def test_schedule_list_includes_last_run(workdir, tmp_path):
    """`schedule list` surfaces the most recent run at a glance.

    :param workdir: isolated working directory fixture.
    :param tmp_path: pytest temp directory fixture.
    """
    _add_schedule_and_record(tmp_path, "succeeded", "FLOW-glance")
    runner = CliRunner()
    result = runner.invoke(app, ["schedule", "list"])
    assert result.exit_code == 0, result.output
    # The "last run" column is present; the flow id may be width-truncated in
    # the table, so the full-id round-trip is asserted via --json elsewhere.
    assert "last run" in result.output
    assert "FLOW-gla" in result.output


def test_schedule_list_json_includes_last_run(workdir, tmp_path):
    """`schedule list --json` includes a last_run object per schedule.

    :param workdir: isolated working directory fixture.
    :param tmp_path: pytest temp directory fixture.
    """
    _add_schedule_and_record(tmp_path, "failed", "FLOW-json")
    runner = CliRunner()
    result = runner.invoke(app, ["schedule", "list", "--json"])
    assert result.exit_code == 0, result.output
    payload = _json.loads(result.output)
    entry = payload["schedules"][0]
    assert entry["last_run"]["flow_id"] == "FLOW-json"
    assert entry["last_run"]["status"] == "failed"


@pytest.mark.skipif(sys.platform == "win32", reason="bash ; syntax in conduit YAML")
def test_run_failure_prints_flow_id_and_status_hint(tmp_path, monkeypatch):
    """Failure output must include the flow_id and a next-step hint so
    the user can inspect what happened. Previously the flow_id was only
    printed on success, leaving failed runs un-inspectable.

    :param tmp_path: pytest temp directory fixture.
    :param monkeypatch: pytest monkeypatch fixture.
    """
    atelier_dir = tmp_path / ".atelier"
    (atelier_dir / "conduits" / "failing").mkdir(parents=True)
    (atelier_dir / "conduits" / "failing" / "conduit.yaml").write_text(
        FAILING_CONDUIT_YAML
    )
    monkeypatch.chdir(tmp_path)
    for k in list(os.environ):
        if k.startswith("ATELIER_") and k not in (
            "ATELIER_GLOBAL_ATELIER_DIR",
            "ATELIER_NO_UPDATE_CHECK",
        ):
            monkeypatch.delenv(k, raising=False)

    runner = CliRunner()
    result = runner.invoke(app, ["run", "failing"])
    assert result.exit_code != 0, result.output
    assert "flow_id" in result.output
    # Hint should point the user at how to resume the failed run.
    assert "atelier run --resume" in result.output
    # The id should match the <date>_<uid>_failing shape.
    assert "_failing" in result.output


# ---------------------------------------------------------------- renderer


def _capture(event: TaskEvent) -> str:
    """Render a TaskEvent into a string using a width-fixed Rich console.

    :param event: the task event to render.
    """
    buf = io.StringIO()
    console = Console(
        file=buf, force_terminal=False, color_system=None, width=120
    )
    render_task_event(event, console)
    return buf.getvalue()


def test_render_successful_task_with_output():
    """Verify a successful task renders task name, tool, body and timing."""
    event = TaskEvent(
        task="greet",
        tool="tool:bash",
        exit_code=0,
        duration_seconds=0.12,
        output="hello world",
        stdout="hello world",
        success=True,
    )
    out = _capture(event)
    assert "greet" in out
    assert "tool:bash" in out
    assert "hello world" in out
    assert "exit=0" in out
    assert "0.12s" in out


def test_render_successful_task_empty_output_is_compact():
    """Verify an empty-output success renders as a compact one-liner."""
    event = TaskEvent(
        task="ping",
        tool="tool:bash",
        exit_code=0,
        duration_seconds=0.01,
        output="",
        stdout="",
        success=True,
    )
    out = _capture(event)
    assert "ping" in out
    assert "no output" in out
    # No panel border glyph for the compact path.
    assert "─" not in out


def test_render_failed_task_falls_back_to_stderr():
    """Verify a failed task with no stdout renders the stderr body."""
    event = TaskEvent(
        task="boom",
        tool="tool:bash",
        exit_code=1,
        duration_seconds=0.05,
        output="",
        stdout="",
        stderr="segfault",
        success=False,
    )
    out = _capture(event)
    assert "boom" in out
    assert "segfault" in out
    assert "exit=1" in out


def test_render_failed_task_shows_both_stdout_and_stderr():
    """When a failure has both stdout and stderr, the panel must show
    both — stderr is the most important diagnostic and was previously
    hidden by `body_source = event.output or event.stderr`.
    """
    event = TaskEvent(
        task="boom",
        tool="tool:bash",
        exit_code=7,
        duration_seconds=0.01,
        output="about to fail on stdout",
        stdout="about to fail on stdout",
        stderr="this is the actual error",
        success=False,
    )
    out = _capture(event)
    assert "about to fail on stdout" in out
    assert "this is the actual error" in out
    # Section labels make the split unambiguous to a human reader.
    assert "stdout" in out.lower()
    assert "stderr" in out.lower()


def test_render_failed_task_with_only_output_does_not_label_sections():
    """When only stdout/output is present (no stderr), keep the
    existing single-body rendering — don't gratuitously add labels.
    """
    event = TaskEvent(
        task="boom",
        tool="tool:bash",
        exit_code=2,
        duration_seconds=0.02,
        output="just some output",
        stdout="just some output",
        stderr="",
        success=False,
    )
    out = _capture(event)
    assert "just some output" in out
    # No "stderr:" label since there's nothing to label.
    assert "stderr:" not in out.lower()


def test_render_does_not_truncate_long_output():
    """Task output is rendered whole, however long.

    This stream is parsed by other agents, not just skimmed by people, and
    a machine reader cannot recover elided lines without going to the store.
    """
    long_out = "\n".join(f"line{i}" for i in range(100))
    event = TaskEvent(
        task="chatty",
        tool="tool:bash",
        exit_code=0,
        duration_seconds=1.5,
        output=long_out,
        success=True,
    )
    out = _capture(event)
    assert "truncated" not in out
    assert "line0" in out
    assert "line99" in out


def test_render_does_not_truncate_failure_output():
    """A failure body is exactly what the reader came for; keep all of it."""
    event = TaskEvent(
        task="build",
        tool="tool:bash",
        exit_code=1,
        duration_seconds=1.5,
        stdout="\n".join(f"out{i}" for i in range(60)),
        stderr="\n".join(f"err{i}" for i in range(60)),
        success=False,
    )
    out = _capture(event)
    assert "truncated" not in out
    assert "out0" in out
    assert "err0" in out
    assert "err59" in out


def test_render_live_streamed_task_is_compact():
    """Interactive harness tasks already streamed their full transcript;
    the after-the-fact panel should be a one-line summary, not a body.
    """
    event = TaskEvent(
        task="ask_then_answer",
        tool="harness:claude-code",
        exit_code=0,
        duration_seconds=12.5,
        output="long multi-turn transcript that already streamed",
        success=True,
        live_streamed=True,
    )
    out = _capture(event)
    assert "ask_then_answer" in out
    assert "harness:claude-code" in out
    assert "streamed live above" in out
    # No box-drawing — compact line, not a panel.
    assert "─" not in out
    # Body content must NOT be re-rendered.
    assert "long multi-turn transcript" not in out


def test_render_skipped_task_shows_reason():
    """Verify a skipped task renders compactly with its skip reason."""
    from flow_atelier.schemas.progress import TaskStatus
    event = TaskEvent(
        task="deploy",
        tool="tool:bash",
        success=False,
        status=TaskStatus.skipped,
        reason="condition not met: review.output.match(APPROVE)",
    )
    out = _capture(event)
    # One-line summary, not a panel.
    assert "deploy" in out
    assert "skipped" in out.lower()
    assert "condition not met" in out
    # No box-drawing characters — must be a compact line.
    assert "─" not in out


def test_render_cancelled_task_shows_reason():
    """Verify a cancelled task renders compactly with its cancel reason."""
    from flow_atelier.schemas.progress import TaskStatus
    event = TaskEvent(
        task="after",
        tool="tool:bash",
        success=False,
        status=TaskStatus.cancelled,
        reason="upstream failed",
    )
    out = _capture(event)
    assert "after" in out
    assert "cancelled" in out.lower()
    assert "upstream failed" in out
    assert "─" not in out


def test_render_iteration_shown_when_repeat_gt_one():
    """Verify the renderer shows iteration markers when repeat > 1."""
    event = TaskEvent(
        task="retry",
        tool="tool:bash",
        iteration=2,
        of=3,
        exit_code=0,
        duration_seconds=0.1,
        output="ok",
        success=True,
    )
    out = _capture(event)
    assert "(2/3)" in out


# ---------------------------------------------------------------------------
# Interactive input prompting tests
# ---------------------------------------------------------------------------

PROMPTED_CONDUIT_YAML = """
name: prompted
description: Conduit with declared inputs
inputs:
  alpha: First input
  beta: Second input
tasks:
  - step:
      description: echo inputs
      task: "echo {{inputs.alpha}} {{inputs.beta}}"
      tool: tool:bash
      depends_on: []
"""


@pytest.fixture
def prompted_workdir(tmp_path, monkeypatch):
    """Provide an isolated working directory seeded with the prompted conduit.

    :param tmp_path: pytest temp directory fixture.
    :param monkeypatch: pytest monkeypatch fixture.
    """
    atelier_dir = tmp_path / ".atelier"
    (atelier_dir / "conduits" / "prompted").mkdir(parents=True)
    (atelier_dir / "conduits" / "prompted" / "conduit.yaml").write_text(
        PROMPTED_CONDUIT_YAML
    )
    monkeypatch.chdir(tmp_path)
    for k in list(os.environ):
        if k.startswith("ATELIER_") and k not in (
            "ATELIER_GLOBAL_ATELIER_DIR",
            "ATELIER_NO_UPDATE_CHECK",
        ):
            monkeypatch.delenv(k, raising=False)
    return tmp_path


def _patch_tty(monkeypatch, *, isatty: bool = True):
    """Make ``sys.stdin.isatty()`` inside ``app.cli.commands.run`` return *isatty*.

    The CliRunner replaces ``sys.stdin`` during ``invoke()``, so we need to
    patch the ``sys`` reference held by the *module* itself.

    :param monkeypatch: pytest monkeypatch fixture.
    :param isatty: value that ``sys.stdin.isatty()`` should report.
    """
    import flow_atelier.cli.commands.run as _run_mod

    _real_sys = _run_mod.sys

    class _FakeStdin:
        def isatty(self):
            """Return the patched isatty value."""
            return isatty

    class _FakeSys:
        stdin = _FakeStdin()

    # Delegate everything else to the real sys module.
    def __getattr__(name):
        """Delegate attribute access to the real `sys` module.

        :param name: attribute name to look up.
        """
        return getattr(_real_sys, name)

    _FakeSys.__getattr__ = __getattr__
    monkeypatch.setattr(_run_mod, "sys", _FakeSys())


def test_prompt_partial_inputs_only_missing(prompted_workdir, monkeypatch):
    """Pass alpha via --input; only beta should be prompted.

    :param prompted_workdir: working directory with prompted conduit fixture.
    :param monkeypatch: pytest monkeypatch fixture.
    """
    prompted_keys: list[str] = []

    def fake_input(prompt=""):
        """Record the prompt and return a canned beta value.

        :param prompt: prompt text emitted by the runtime.
        """
        prompted_keys.append(prompt)
        return "val_beta"

    monkeypatch.setattr("builtins.input", fake_input)
    _patch_tty(monkeypatch, isatty=True)

    runner = CliRunner()
    result = runner.invoke(app, ["run", "prompted", "-i", "alpha=val_alpha"])
    assert result.exit_code == 0, result.output
    # Only beta was prompted
    assert len(prompted_keys) == 1
    assert "beta" in prompted_keys[0]
    assert "alpha" not in "".join(prompted_keys)


def test_prompt_all_inputs_when_none_given(prompted_workdir, monkeypatch):
    """No --input flags → both alpha and beta prompted, in declaration order.

    :param prompted_workdir: working directory with prompted conduit fixture.
    :param monkeypatch: pytest monkeypatch fixture.
    """
    prompted_keys: list[str] = []
    answers = iter(["val_alpha", "val_beta"])

    def fake_input(prompt=""):
        """Record the prompt and return the next canned answer.

        :param prompt: prompt text emitted by the runtime.
        """
        prompted_keys.append(prompt)
        return next(answers)

    monkeypatch.setattr("builtins.input", fake_input)
    _patch_tty(monkeypatch, isatty=True)

    runner = CliRunner()
    result = runner.invoke(app, ["run", "prompted"])
    assert result.exit_code == 0, result.output
    assert len(prompted_keys) == 2
    # Declaration order: alpha first, then beta
    assert "alpha" in prompted_keys[0]
    assert "beta" in prompted_keys[1]


def test_no_prompt_when_all_inputs_provided(prompted_workdir, monkeypatch):
    """All inputs via --input → no prompting at all.

    :param prompted_workdir: working directory with prompted conduit fixture.
    :param monkeypatch: pytest monkeypatch fixture.
    """
    prompted_keys: list[str] = []

    def fake_input(prompt=""):
        """Record the prompt; should never be called in this test.

        :param prompt: prompt text emitted by the runtime.
        """
        prompted_keys.append(prompt)
        return "should_not_be_called"

    monkeypatch.setattr("builtins.input", fake_input)
    _patch_tty(monkeypatch, isatty=True)

    runner = CliRunner()
    result = runner.invoke(
        app, ["run", "prompted", "-i", "alpha=a", "-i", "beta=b"]
    )
    assert result.exit_code == 0, result.output
    assert len(prompted_keys) == 0


def test_no_prompt_when_stdin_not_tty(prompted_workdir, monkeypatch):
    """Non-TTY stdin → prompting skipped, engine raises ValueError.

    :param prompted_workdir: working directory with prompted conduit fixture.
    :param monkeypatch: pytest monkeypatch fixture.
    """
    _patch_tty(monkeypatch, isatty=False)

    runner = CliRunner()
    result = runner.invoke(app, ["run", "prompted"])
    assert result.exit_code != 0
    assert "missing required inputs" in result.output


def test_ctrl_c_during_prompt_exits_130(prompted_workdir, monkeypatch):
    """KeyboardInterrupt during prompting → exit code 130, no traceback.

    :param prompted_workdir: working directory with prompted conduit fixture.
    :param monkeypatch: pytest monkeypatch fixture.
    """

    def fake_input(prompt=""):
        """Raise KeyboardInterrupt to simulate the user hitting Ctrl-C.

        :param prompt: prompt text emitted by the runtime.
        """
        raise KeyboardInterrupt

    monkeypatch.setattr("builtins.input", fake_input)
    _patch_tty(monkeypatch, isatty=True)

    runner = CliRunner()
    result = runner.invoke(app, ["run", "prompted"])
    assert result.exit_code == 130
    # No Python traceback in output
    assert "Traceback" not in result.output


def test_no_prompt_for_conduit_without_inputs(workdir, monkeypatch):
    """Conduit with inputs: {} → no prompting.

    :param workdir: isolated working directory fixture.
    :param monkeypatch: pytest monkeypatch fixture.
    """
    prompted_keys: list[str] = []

    def fake_input(prompt=""):
        """Record the prompt; should never be called in this test.

        :param prompt: prompt text emitted by the runtime.
        """
        prompted_keys.append(prompt)
        return "should_not_be_called"

    monkeypatch.setattr("builtins.input", fake_input)
    _patch_tty(monkeypatch, isatty=True)

    runner = CliRunner()
    # 'hello' conduit has no declared inputs
    result = runner.invoke(app, ["run", "hello", "-i", "name=world"])
    assert result.exit_code == 0, result.output
    assert len(prompted_keys) == 0


# ---------------------------------------------------------------- check cmd


def _write_conduit(workdir, name, yaml_text, *, folder=None):
    """Write a conduit.yaml under the workdir's project conduits dir.

    :param workdir: tmp_path returned by the ``workdir`` fixture.
    :param name: conduit name used for the YAML and (default) folder.
    :param yaml_text: full conduit.yaml contents to write.
    :param folder: override folder name to provoke name/folder mismatch.
    """
    folder = folder or name
    d = workdir / ".atelier" / "conduits" / folder
    d.mkdir(parents=True, exist_ok=True)
    (d / "conduit.yaml").write_text(yaml_text)


def test_check_clean_conduit_ok(workdir):
    """A valid conduit reports OK and exits 0.

    :param workdir: isolated working directory fixture.
    """
    runner = CliRunner()
    result = runner.invoke(app, ["check", "hello"])
    assert result.exit_code == 0, result.output
    assert "hello" in result.output
    assert "OK" in result.output


def test_check_unknown_conduit(workdir):
    """`check <missing>` prints a clear error and exits 1.

    :param workdir: isolated working directory fixture.
    """
    runner = CliRunner()
    result = runner.invoke(app, ["check", "nope"])
    assert result.exit_code == 1
    assert "unknown conduit" in result.output
    assert "Traceback" not in result.output


def test_check_cycle(workdir):
    """A circular dependency is reported as FAIL with exit 1.

    :param workdir: isolated working directory fixture.
    """
    _write_conduit(
        workdir,
        "cyc",
        """
name: cyc
description: cycle
tasks:
  - a:
      description: a
      task: "echo a"
      tool: tool:bash
      depends_on: ["b"]
  - b:
      description: b
      task: "echo b"
      tool: tool:bash
      depends_on: ["a"]
""",
    )
    runner = CliRunner()
    result = runner.invoke(app, ["check", "cyc"])
    assert result.exit_code == 1
    assert "FAIL" in result.output
    assert "circular" in result.output


def test_check_unknown_dep(workdir):
    """A dependency on a missing task is reported as FAIL.

    :param workdir: isolated working directory fixture.
    """
    _write_conduit(
        workdir,
        "udep",
        """
name: udep
description: unknown dep
tasks:
  - a:
      description: a
      task: "echo a"
      tool: tool:bash
      depends_on: ["ghost"]
""",
    )
    runner = CliRunner()
    result = runner.invoke(app, ["check", "udep"])
    assert result.exit_code == 1
    assert "unknown task 'ghost'" in result.output


def test_check_dangling_template_ref(workdir):
    """A {{ref.output}} outside the depends_on chain is reported as FAIL.

    :param workdir: isolated working directory fixture.
    """
    _write_conduit(
        workdir,
        "dang",
        """
name: dang
description: dangling ref
tasks:
  - a:
      description: a
      task: "echo a"
      tool: tool:bash
      depends_on: []
  - b:
      description: b
      task: "use {{a.output}}"
      tool: tool:bash
      depends_on: []
""",
    )
    runner = CliRunner()
    result = runner.invoke(app, ["check", "dang"])
    assert result.exit_code == 1
    assert "references 'a'" in result.output
    assert "depends_on" in result.output


def test_check_bad_predicate_regex(workdir):
    """A malformed loop predicate regex is reported as FAIL.

    :param workdir: isolated working directory fixture.
    """
    _write_conduit(
        workdir,
        "badre",
        """
name: badre
description: bad regex
tasks:
  - a:
      description: a
      task: "echo a"
      tool: tool:bash
      depends_on: []
      repeat: 3
      until: "output.match([unclosed)"
""",
    )
    runner = CliRunner()
    result = runner.invoke(app, ["check", "badre"])
    assert result.exit_code == 1
    assert "FAIL" in result.output


def test_check_duplicate_task_names(workdir):
    """Duplicate task names are reported as FAIL.

    :param workdir: isolated working directory fixture.
    """
    _write_conduit(
        workdir,
        "dup",
        """
name: dup
description: dup names
tasks:
  - a:
      description: a
      task: "echo a"
      tool: tool:bash
      depends_on: []
  - a:
      description: a2
      task: "echo a2"
      tool: tool:bash
      depends_on: []
""",
    )
    runner = CliRunner()
    result = runner.invoke(app, ["check", "dup"])
    assert result.exit_code == 1
    assert "duplicate task names" in result.output


def test_check_name_folder_mismatch(workdir):
    """A conduit whose name differs from its folder is reported as FAIL.

    :param workdir: isolated working directory fixture.
    """
    _write_conduit(
        workdir,
        "inner",
        """
name: inner
description: mismatch
tasks:
  - a:
      description: a
      task: "echo a"
      tool: tool:bash
      depends_on: []
""",
        folder="outer",
    )
    runner = CliRunner()
    result = runner.invoke(app, ["check", "outer"])
    assert result.exit_code == 1
    assert "!=" in result.output


def test_check_malformed_yaml(workdir):
    """Broken YAML is reported as a one-line FAIL, not a traceback.

    :param workdir: isolated working directory fixture.
    """
    _write_conduit(workdir, "broke", "name: broke\ntasks: [unclosed\n")
    runner = CliRunner()
    result = runner.invoke(app, ["check", "broke"])
    assert result.exit_code == 1
    assert "invalid YAML" in result.output
    assert "Traceback" not in result.output


def test_check_all_reports_good_and_bad(workdir):
    """`check` (no arg) lists every conduit and exits 1 if any fails.

    :param workdir: isolated working directory fixture.
    """
    _write_conduit(
        workdir,
        "cyc",
        """
name: cyc
description: cycle
tasks:
  - a:
      description: a
      task: "echo a"
      tool: tool:bash
      depends_on: ["b"]
  - b:
      description: b
      task: "echo b"
      tool: tool:bash
      depends_on: ["a"]
""",
    )
    runner = CliRunner()
    result = runner.invoke(app, ["check"])
    assert result.exit_code == 1
    assert "hello" in result.output and "OK" in result.output
    assert "cyc" in result.output and "FAIL" in result.output


def test_list_conduits_invalid_shows_reason(workdir):
    """A malformed conduit shows ``(invalid: …)`` instead of ``(unreadable)``.

    :param workdir: isolated working directory fixture.
    """
    _write_conduit(
        workdir,
        "broke",
        """
name: broke
description: bad tool
tasks:
  - a:
      description: a
      task: "echo a"
      tool: tool:nope
      depends_on: []
""",
    )
    runner = CliRunner()
    result = runner.invoke(app, ["list", "conduits"])
    assert result.exit_code == 0, result.output
    # The good conduit still renders normally; the bad one shows a reason.
    assert "hello" in result.output
    assert "invalid" in result.output
    assert "(unreadable)" not in result.output
    assert "Traceback" not in result.output


def test_list_conduits_json_includes_error(workdir):
    """JSON output carries an ``error`` reason for unreadable conduits.

    :param workdir: isolated working directory fixture.
    """
    _write_conduit(workdir, "broke", "name: broke\ntasks: [unclosed\n")
    runner = CliRunner()
    result = runner.invoke(app, ["list", "conduits", "--json"])
    assert result.exit_code == 0, result.output
    by_name = {e["name"]: e for e in _json.loads(result.output)}
    assert by_name["hello"]["error"] is None
    assert by_name["broke"]["error"]
    assert "invalid YAML" in by_name["broke"]["error"]


def test_run_invalid_conduit(workdir):
    """`run <malformed>` exits 1 with a readable message and a fix pointer.

    :param workdir: isolated working directory fixture.
    """
    _write_conduit(workdir, "broke", "name: broke\ntasks: [unclosed\n")
    runner = CliRunner()
    result = runner.invoke(app, ["run", "broke"])
    assert result.exit_code == 1
    assert "invalid conduit" in result.output
    assert "fix conduits" in result.output
    assert "Traceback" not in result.output


def test_run_resume_invalid_conduit(workdir):
    """`run --resume` on a now-malformed conduit shows the friendly message.

    :param workdir: isolated working directory fixture.
    """
    _write_conduit(
        workdir,
        "willfail",
        """
name: willfail
description: fails
tasks:
  - boom:
      description: boom
      task: "exit 1"
      tool: tool:bash
      depends_on: []
""",
    )
    runner = CliRunner()
    r1 = runner.invoke(app, ["run", "willfail"])
    assert r1.exit_code == 1
    from flow_atelier.core.atelier import Atelier

    flows = Atelier().list_flows("willfail")
    assert flows
    fid = flows[0]
    # Corrupt the conduit, then resume the failed flow.
    (workdir / ".atelier" / "conduits" / "willfail" / "conduit.yaml").write_text(
        "name: willfail\ntasks: [unclosed\n"
    )
    r2 = runner.invoke(app, ["run", "--resume", fid])
    assert r2.exit_code == 1
    assert "invalid conduit" in r2.output
    assert "fix conduits" in r2.output
    assert "Traceback" not in r2.output


# --- liveness / crashed-flow detection -------------------------------------

def _run_hello(runner, name="a"):
    """Run the hello conduit and return its flow id.

    :param runner: CliRunner instance.
    :param name: value for the greet input.
    :returns: the created flow id.
    """
    result = runner.invoke(app, ["run", "hello", "--input", f"name={name}"])
    assert result.exit_code == 0, result.output
    line = [l for l in result.output.splitlines() if "flow_id" in l][0]
    return line.split()[-1]


def _dead_local_pid():
    """Return a pid that has provably exited on this host."""
    import subprocess
    import sys as _sys

    proc = subprocess.Popen([_sys.executable, "-c", ""])
    proc.wait()
    return proc.pid


def _force_running(flow_id, pid):
    """Rewrite a flow's progress.json to status=running with the given pid.

    :param flow_id: flow to mutate.
    :param pid: runner_pid to record (dead pid simulates a crash).
    """
    import socket

    from flow_atelier.core.atelier import Atelier
    from flow_atelier.schemas.progress import FlowStatus

    atelier = Atelier()
    progress = atelier.store.read_progress(flow_id)
    progress.status = FlowStatus.running
    progress.runner_pid = pid
    progress.runner_host = socket.gethostname()
    atelier.store.write_progress(flow_id, progress)


def test_run_records_runner_identity(workdir):
    """A started flow persists this process's pid and a non-empty host.

    :param workdir: isolated working directory fixture.
    """
    runner = CliRunner()
    flow_id = _run_hello(runner)
    from flow_atelier.core.atelier import Atelier

    progress = Atelier().store.read_progress(flow_id)
    # CliRunner runs in-process, so the runner pid is this test process.
    assert progress.runner_pid == os.getpid()
    assert progress.runner_host


def test_status_reports_crashed_with_resume_hint(workdir):
    """status on a dead-runner flow shows crashed + a resume hint.

    :param workdir: isolated working directory fixture.
    """
    runner = CliRunner()
    flow_id = _run_hello(runner)
    _force_running(flow_id, _dead_local_pid())

    result = runner.invoke(app, ["status", flow_id])
    assert result.exit_code == 0, result.output
    assert "crashed" in result.output
    assert f"--resume {flow_id}" in result.output

    jr = runner.invoke(app, ["status", flow_id, "--json"])
    assert jr.exit_code == 0
    import json as _json

    payload = _json.loads(jr.output)
    assert payload["crashed"] is True
    # Persisted status field is untouched.
    assert payload["status"] == "running"


def test_status_live_running_not_crashed(workdir):
    """status on a live-runner running flow shows running, no hint.

    :param workdir: isolated working directory fixture.
    """
    runner = CliRunner()
    flow_id = _run_hello(runner)
    _force_running(flow_id, os.getpid())

    result = runner.invoke(app, ["status", flow_id])
    assert result.exit_code == 0
    assert "crashed" not in result.output
    assert "--resume" not in result.output

    jr = runner.invoke(app, ["status", flow_id, "--json"])
    payload = __import__("json").loads(jr.output)
    assert payload["crashed"] is False


def test_list_flows_marks_crashed(workdir):
    """list flows renders a dead-runner flow as crashed, a live one running.

    :param workdir: isolated working directory fixture.
    """
    runner = CliRunner()
    dead_flow = _run_hello(runner, "dead")
    live_flow = _run_hello(runner, "live")
    _force_running(dead_flow, _dead_local_pid())
    _force_running(live_flow, os.getpid())

    result = runner.invoke(app, ["list", "flows"])
    assert result.exit_code == 0, result.output
    assert "crashed" in result.output
    assert "running" in result.output

    jr = runner.invoke(app, ["list", "flows", "--json"])
    import json as _json

    rows = {r["flow_id"]: r for r in _json.loads(jr.output)}
    assert rows[dead_flow]["crashed"] is True
    assert rows[live_flow]["crashed"] is False


def test_follow_logs_exits_on_crash(workdir):
    """logs --follow on a dead-runner flow terminates instead of hanging.

    :param workdir: isolated working directory fixture.
    """
    import threading

    runner = CliRunner()
    flow_id = _run_hello(runner)
    _force_running(flow_id, _dead_local_pid())

    box = {}

    def _go():
        box["result"] = runner.invoke(app, ["logs", flow_id, "--follow"])

    t = threading.Thread(target=_go)
    t.start()
    t.join(timeout=10)
    assert not t.is_alive(), "logs --follow hung on a crashed flow"
    assert "crashed" in box["result"].output
