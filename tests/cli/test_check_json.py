"""CLI tests for `atelier check --json` — the machine-readable check report."""
from __future__ import annotations

import json
import os
import stat

import pytest
from typer.testing import CliRunner

from flow_atelier.cli import app
from flow_atelier.services.store.filesystem import FilesystemStore

OK_BASH = (
    "name: {name}\ndescription: d\n"
    "tasks:\n  - name: a\n    description: a\n    task: echo hi\n    tool: tool:bash\n"
)

# Declared-required, defaulted, and empty-string-defaulted: the last is
# optional, because an empty string is still a default.
INPUT_SHAPES = """\
name: needy
description: d
inputs:
  topic:
    description: no default, so it must be supplied
  tone:
    description: has a default
    default: blunt
  suffix:
    description: an empty string is still a default
    default: ""
tasks:
  - name: a
    description: a
    task: "echo {{inputs.topic}}"
    tool: tool:bash
"""

MISSING_DEP = """\
name: dangling
description: d
tasks:
  - name: a
    description: a
    task: echo hi
    tool: tool:bash
    depends_on: [nope]
"""

# Unicode and Rich markup inside text the checker quotes back at the author.
WEIRD_NAME = 'name: "é[b]"\ndescription: d\ntasks: []\n'


@pytest.fixture
def workdir(tmp_path, monkeypatch):
    """Isolated cwd with empty project and global conduit stores.

    :param tmp_path: pytest temp directory fixture.
    :param monkeypatch: pytest monkeypatch fixture.
    :returns: the working directory path.
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


def _write(workdir, name: str, body: str | bytes, source: str = "project"):
    """Write `<name>/conduit.yaml` into the project or the global store.

    :param workdir: working directory path.
    :param name: conduit name, which is also the folder name.
    :param body: the YAML document, as text or as raw bytes.
    :param source: ``project`` or ``global``.
    :returns: the path of the written conduit file.
    """
    root = workdir / ".atelier" if source == "project" else workdir / "global"
    cdir = root / "conduits" / name
    cdir.mkdir(parents=True, exist_ok=True)
    path = cdir / "conduit.yaml"
    if isinstance(body, bytes):
        path.write_bytes(body)
    else:
        path.write_text(body, encoding="utf-8")
    return path


def _report(args, expect_exit=None):
    """Run `check` with `--json` and parse the whole of stdout.

    :param args: argv after ``check``.
    :param expect_exit: exit status to assert, or None to skip the assertion.
    :returns: ``(result, rows)`` — the CLI result and the parsed array.
    """
    result = CliRunner().invoke(app, ["check", *args, "--json"])
    if expect_exit is not None:
        assert result.exit_code == expect_exit, result.output
    rows = json.loads(result.stdout)
    assert isinstance(rows, list)
    return result, rows


def _flow_ids(workdir) -> list[str]:
    """Return the recorded flow ids, tolerating a store that made the folder.

    :param workdir: working directory path.
    :returns: sorted flow directory names, empty when nothing ever ran.
    """
    flows = workdir / ".atelier" / "flows"
    return sorted(p.name for p in flows.iterdir()) if flows.exists() else []


def _assert_shape(row):
    """Assert one row carries exactly the documented keys and types.

    :param row: one parsed result row.
    """
    assert set(row) == {"name", "source", "path", "ok", "error", "required_inputs"}
    assert isinstance(row["name"], str)
    assert row["source"] in {"project", "global"}
    assert row["path"] is None or isinstance(row["path"], str)
    assert isinstance(row["ok"], bool)
    if row["ok"]:
        assert row["error"] is None
        assert isinstance(row["required_inputs"], list)
    else:
        # Failure nullability is explicit: unknown metadata is null, never an
        # empty list a caller would read as "no inputs needed".
        assert isinstance(row["error"], str) and row["error"]
        assert row["required_inputs"] is None


def test_named_success_is_one_clean_row(workdir):
    """A named check emits one row and nothing a parser has to strip."""
    path = _write(workdir, "hello", OK_BASH.format(name="hello"))
    result, rows = _report(["hello"], expect_exit=0)

    assert result.stderr == ""
    assert result.stdout.endswith("]\n")
    assert "\x1b[" not in result.stdout
    assert len(rows) == 1
    _assert_shape(rows[0])
    assert rows[0] == {
        "name": "hello",
        "source": "project",
        "path": str(path.absolute()),
        "ok": True,
        "error": None,
        "required_inputs": [],
    }
    assert os.path.isabs(rows[0]["path"])


def test_required_inputs_lists_only_the_ones_without_a_default(workdir):
    """A default — including an empty string — keeps a key off the list."""
    _write(workdir, "needy", INPUT_SHAPES)
    _, rows = _report(["needy"], expect_exit=0)
    assert rows[0]["required_inputs"] == ["topic"]


def test_empty_store_reports_an_empty_array_and_succeeds(workdir):
    """Nothing installed is not a failure, and not evidence of a check either."""
    result, rows = _report([], expect_exit=0)
    assert rows == []
    assert "no conduits found" not in result.stdout


def test_a_broken_conduit_does_not_hide_the_ones_after_it(workdir):
    """Every selected conduit is reported, in the store's sorted order."""
    _write(workdir, "aaa-broken", "name: aaa-broken\ndescription: [unclosed\n")
    _write(workdir, "zzz-fine", OK_BASH.format(name="zzz-fine"))
    result, rows = _report([], expect_exit=1)

    assert [r["name"] for r in rows] == ["aaa-broken", "zzz-fine"]
    for row in rows:
        _assert_shape(row)
    assert rows[0]["ok"] is False
    assert rows[1]["ok"] is True
    # A multi-line parser diagnostic is data in this mode, not layout.
    assert "\n" in rows[0]["error"]
    assert result.stderr == ""


@pytest.mark.parametrize(
    ("name", "body", "says"),
    [
        ("badyaml", "name: badyaml\ndescription: [unclosed\n", "invalid YAML"),
        ("empty", "", "Input should be"),
        (
            "badfield",
            OK_BASH.format(name="badfield") + "max_concurrency: 0\n",
            "greater than or equal to 1",
        ),
        ("mismatch", OK_BASH.format(name="other"), "folder name"),
        ("dangling", MISSING_DEP, "unknown task 'nope'"),
    ],
)
def test_each_malformed_conduit_fails_with_a_readable_diagnostic(
    workdir, name, body, says
):
    """Bad YAML, a bad field, a name mismatch and a bad edge each fail alone."""
    path = _write(workdir, name, body)
    _write(workdir, "zzz-fine", OK_BASH.format(name="zzz-fine"))
    _, rows = _report([], expect_exit=1)

    row = next(r for r in rows if r["name"] == name)
    _assert_shape(row)
    assert row["ok"] is False
    assert says in row["error"]
    # The path is the file to repair, even though the load failed.
    assert row["path"] == str(path.absolute())
    assert rows[-1]["name"] == "zzz-fine" and rows[-1]["ok"] is True


def test_an_unavailable_harness_is_a_failure_not_a_crash(workdir, monkeypatch):
    """Readiness still gates the report: a missing CLI is a failed row."""
    monkeypatch.setattr(
        "flow_atelier.services.executor.harness.shutil.which",
        lambda _binary: None,
    )
    _write(
        workdir,
        "agentic",
        "name: agentic\ndescription: d\n"
        "tasks:\n  - name: build\n    description: b\n"
        "    task: do it\n    tool: harness:claude-code\n",
    )
    _, rows = _report(["agentic"], expect_exit=1)
    _assert_shape(rows[0])
    assert "build" in rows[0]["error"]


def test_undecodable_bytes_fail_without_stopping_the_batch(workdir):
    """A conduit that is not text at all is a failed row, not a traceback."""
    _write(workdir, "binary", b"\xff\xfe\x00name: binary\n")
    _write(workdir, "zzz-fine", OK_BASH.format(name="zzz-fine"))
    result, rows = _report([], expect_exit=1)

    assert [r["name"] for r in rows] == ["binary", "zzz-fine"]
    _assert_shape(rows[0])
    assert rows[0]["ok"] is False
    assert rows[1]["ok"] is True
    assert "Traceback" not in result.output


def test_diagnostic_text_survives_json_serialization(workdir):
    """Unicode, markup and newlines quoted back are data, not formatting."""
    _write(workdir, "weird", WEIRD_NAME)
    _, rows = _report(["weird"], expect_exit=1)
    assert "é[b]" in rows[0]["error"]

    plain = CliRunner().invoke(app, ["check", "weird"])
    assert plain.exit_code == 1
    assert "FAIL" in plain.stdout
    assert "Traceback" not in plain.output


def test_a_broken_project_copy_is_never_swapped_for_the_global_one(workdir):
    """Shadowing is reported as it runs: the project copy, broken and all."""
    broken = _write(workdir, "hello", "name: hello\ndescription: [unclosed\n")
    _write(workdir, "hello", OK_BASH.format(name="hello"), source="global")
    _, rows = _report(["hello"], expect_exit=1)

    assert len(rows) == 1
    assert rows[0]["source"] == "project"
    assert rows[0]["path"] == str(broken.absolute())
    assert rows[0]["ok"] is False


def test_a_global_conduit_is_included_and_labelled(workdir):
    """The batch covers the global store, so a report is not project-only."""
    _write(workdir, "shared", OK_BASH.format(name="shared"), source="global")
    _, rows = _report([], expect_exit=0)
    assert [(r["name"], r["source"]) for r in rows] == [("shared", "global")]


def test_an_unknown_name_leaves_stdout_empty_and_explains_itself(workdir):
    """A command error is not a report: nothing parseable, guidance on stderr."""
    _write(workdir, "hello", OK_BASH.format(name="hello"))
    result = CliRunner().invoke(app, ["check", "helo", "--json"])

    assert result.exit_code == 1
    assert result.stdout == ""
    assert "unknown conduit" in result.stderr
    assert "did you mean: hello?" in result.stderr
    assert "Traceback" not in result.output


def test_a_discovery_failure_is_not_reported_as_an_empty_report(workdir, monkeypatch):
    """An unreadable store must never look like "checked all, none failed"."""

    def _boom(self):
        """Fail the conduit listing the way an unreadable store would.

        :param self: the store instance.
        :raises PermissionError: always.
        """
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(FilesystemStore, "list_conduits_with_source", _boom)
    result = CliRunner().invoke(app, ["check", "--json"])

    assert result.exit_code == 1
    assert result.stdout == ""
    assert "cannot list conduits" in result.stderr
    assert "Traceback" not in result.output


@pytest.mark.parametrize("method", ["conduit_dir", "read_conduit"])
def test_a_per_conduit_io_failure_stays_inside_its_own_row(
    workdir, monkeypatch, method
):
    """Resolution and reading are both inside the boundary, not only reading."""
    _write(workdir, "aaa-locked", OK_BASH.format(name="aaa-locked"))
    _write(workdir, "zzz-fine", OK_BASH.format(name="zzz-fine"))
    original = getattr(FilesystemStore, method)

    def _boom(self, name):
        """Raise for the locked conduit only, delegating for every other.

        :param self: the store instance.
        :param name: conduit name being resolved or read.
        :returns: whatever the real method returns for other conduits.
        :raises PermissionError: for ``aaa-locked``.
        """
        if name == "aaa-locked":
            raise PermissionError(13, "Permission denied")
        return original(self, name)

    monkeypatch.setattr(FilesystemStore, method, _boom)
    _, rows = _report([], expect_exit=1)

    assert [r["name"] for r in rows] == ["aaa-locked", "zzz-fine"]
    _assert_shape(rows[0])
    assert "Permission denied" in rows[0]["error"]
    # A failure during resolution has no path to report; a failure while
    # reading already knows which file it was.
    assert rows[0]["path"] is None if method == "conduit_dir" else rows[0]["path"]
    assert rows[1]["ok"] is True


@pytest.mark.skipif(
    hasattr(os, "geteuid") and os.geteuid() == 0, reason="root ignores file modes"
)
def test_a_real_unreadable_file_is_a_failed_row(workdir):
    """Not fault injection: a mode the filesystem actually enforces."""
    locked = _write(workdir, "aaa-locked", OK_BASH.format(name="aaa-locked"))
    _write(workdir, "zzz-fine", OK_BASH.format(name="zzz-fine"))
    locked.chmod(0o000)
    try:
        result, rows = _report([], expect_exit=1)
    finally:
        locked.chmod(stat.S_IRUSR | stat.S_IWUSR)

    assert [r["name"] for r in rows] == ["aaa-locked", "zzz-fine"]
    assert rows[0]["ok"] is False and rows[0]["path"] == str(locked.absolute())
    assert rows[1]["ok"] is True
    assert "Traceback" not in result.output


@pytest.mark.skipif(
    hasattr(os, "geteuid") and os.geteuid() == 0, reason="root ignores file modes"
)
def test_an_unreadable_conduit_directory_is_a_command_error(workdir):
    """Not fault injection: the store itself cannot enumerate, so nothing is
    reported rather than a partial array that looks complete."""
    locked = _write(workdir, "aaa-locked", OK_BASH.format(name="aaa-locked")).parent
    _write(workdir, "zzz-fine", OK_BASH.format(name="zzz-fine"))
    locked.chmod(0o000)
    try:
        result = CliRunner().invoke(app, ["check", "--json"])
    finally:
        locked.chmod(stat.S_IRWXU)

    assert result.exit_code == 1
    assert result.stdout == ""
    assert "cannot list conduits" in result.stderr
    assert "Traceback" not in result.output


def test_a_successful_conduit_is_loaded_once(workdir, monkeypatch):
    """The human line and the row come from one load, not two reads."""
    _write(workdir, "needy", INPUT_SHAPES)
    original = FilesystemStore.read_conduit
    calls: list[str] = []

    def _counted(self, name):
        """Record each load and delegate to the real reader.

        :param self: the store instance.
        :param name: conduit name being read.
        :returns: the loaded conduit.
        """
        calls.append(name)
        return original(self, name)

    monkeypatch.setattr(FilesystemStore, "read_conduit", _counted)

    assert CliRunner().invoke(app, ["check", "needy"]).exit_code == 0
    assert calls == ["needy"]


def test_plain_output_keeps_its_words_and_agrees_with_the_report(workdir):
    """The two modes never disagree about a conduit, only about formatting."""
    _write(workdir, "needy", INPUT_SHAPES)
    _write(workdir, "dangling", MISSING_DEP)

    plain = CliRunner().invoke(app, ["check"])
    json_result, rows = _report([], expect_exit=plain.exit_code)

    assert plain.exit_code == 1
    assert "OK" in plain.stdout and "FAIL" in plain.stdout
    assert "requires --input: topic" in plain.stdout
    assert {r["name"]: r["ok"] for r in rows} == {"needy": True, "dangling": False}
    assert json_result.exit_code == plain.exit_code


def test_checking_runs_nothing(workdir):
    """No task executes, so no flow is recorded and no side effect lands."""
    sentinel = workdir / "ran.txt"
    _write(
        workdir,
        "sideeffect",
        "name: sideeffect\ndescription: d\ntasks:\n"
        "  - name: a\n    description: a\n"
        f"    task: \"touch {sentinel}\"\n    tool: tool:bash\n",
    )
    _report(["sideeffect"], expect_exit=0)

    assert not sentinel.exists()
    assert _flow_ids(workdir) == []


def test_repair_a_broken_conduit_from_the_report_and_run_it(workdir):
    """The whole point: fail, find the file, fix it, pass, run, read a result."""
    runner = CliRunner()
    assert runner.invoke(app, ["init"]).exit_code == 0

    conduit = workdir / ".atelier" / "conduits" / "hello" / "conduit.yaml"
    conduit.write_text(
        conduit.read_text(encoding="utf-8").replace(
            "depends_on: []", "depends_on: [missing]"
        ),
        encoding="utf-8",
    )

    # The model still loads — this is exactly the mistake `show` cannot catch.
    shown = runner.invoke(app, ["show", "hello", "--json"])
    assert shown.exit_code == 0, shown.output
    assert json.loads(shown.stdout)["conduit"]["name"] == "hello"

    _, rows = _report(["hello"], expect_exit=1)
    assert rows[0]["ok"] is False
    assert "unknown task 'missing'" in rows[0]["error"]
    assert rows[0]["required_inputs"] is None

    # Repair the file the report named, without knowing where it lives.
    broken_path = workdir / rows[0]["path"]
    broken_path.write_text(
        broken_path.read_text(encoding="utf-8").replace(
            "depends_on: [missing]", "depends_on: []"
        ),
        encoding="utf-8",
    )

    _, rows = _report(["hello"], expect_exit=0)
    assert rows[0]["ok"] is True
    assert rows[0]["required_inputs"] == ["name"]
    assert _flow_ids(workdir) == []

    assert runner.invoke(app, ["run", "hello", "--input", "name=world"]).exit_code == 0
    outputs = runner.invoke(app, ["outputs", "latest", "--task", "greet"])
    assert outputs.exit_code == 0, outputs.output
    assert "hello world" in outputs.output
    assert len(_flow_ids(workdir)) == 1


def test_the_readme_report_example_works(workdir):
    """Save the report, keep the status, print the failed rows — then succeed."""
    _write(workdir, "dangling", MISSING_DEP)
    _write(workdir, "zzz-fine", OK_BASH.format(name="zzz-fine"))
    runner = CliRunner()

    failing = runner.invoke(app, ["check", "--json"])
    report = workdir / "check-report.json"
    report.write_text(failing.stdout, encoding="utf-8")
    status = failing.exit_code

    failed = [
        (row["path"], row["error"])
        for row in json.loads(report.read_text(encoding="utf-8"))
        if not row["ok"]
    ]
    assert status == 1
    assert len(failed) == 1
    assert failed[0][0].endswith("conduits/dangling/conduit.yaml")

    (workdir / ".atelier" / "conduits" / "dangling" / "conduit.yaml").write_text(
        MISSING_DEP.replace("depends_on: [nope]", "depends_on: []"), encoding="utf-8"
    )
    passing = runner.invoke(app, ["check", "--json"])
    report.write_text(passing.stdout, encoding="utf-8")
    rows = json.loads(report.read_text(encoding="utf-8"))
    assert passing.exit_code == 0
    assert [r["ok"] for r in rows] == [True, True]


def test_help_says_how_to_read_the_report(workdir):
    """`--help` explains the status-plus-stdout contract before you rely on it."""
    result = CliRunner().invoke(app, ["check", "--help"])
    assert result.exit_code == 0
    assert "--json" in result.output
