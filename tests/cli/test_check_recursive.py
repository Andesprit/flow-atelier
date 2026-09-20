"""CLI tests for `atelier check --recursive` — validating a composed workflow.

The flag follows `tool:conduit` calls, so the unit of feedback becomes the
whole call tree rather than the one file you named. These tests pin the graph
behaviour (missing, invalid and unrunnable children, cycles, depth, shared
children), the error boundary around every child, the fact that nothing is
executed, and the README walkthrough as a real shell session.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest
from typer.testing import CliRunner

from flow_atelier.cli import app
from flow_atelier.modules.engine import MAX_NESTED_CONDUIT_DEPTH
from flow_atelier.services.store.filesystem import FilesystemStore

_REPO_ROOT = Path(__file__).resolve().parents[2]
_README = _REPO_ROOT / "README.md"
_SECTION = "### Checking a workflow that calls other workflows"
_BASH_BLOCK = re.compile(r"^```bash\n(.*?)^```$", re.MULTILINE | re.DOTALL)
# Same shim trick as the other real-process CLI tests: run this interpreter's
# CLI as plain `atelier`, without depending on the console script on PATH.
_CLI = "from flow_atelier.main import app; app()"

BASH = (
    "name: {name}\ndescription: d\n"
    "tasks:\n  - name: step\n    description: a\n"
    "    task: echo hi\n    tool: tool:bash\n"
)


def _calls(name: str, *targets: str, task: str = "call") -> str:
    """Build a conduit that calls each of ``targets`` with ``tool:conduit``.

    :param name: the conduit's own name.
    :param targets: raw ``task:`` values, one nested call each, in order.
    :param task: base name for the generated calling tasks.
    :returns: a complete conduit YAML document.
    """
    body = f"name: {name}\ndescription: d\ntasks:\n"
    for i, target in enumerate(targets):
        body += (
            f"  - name: {task}{i}\n    description: c\n"
            f'    task: "{target}"\n    tool: tool:conduit\n'
        )
    return body


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


@pytest.fixture(autouse=True)
def never_executes(monkeypatch):
    """Make any task execution or ACP session startup an outright test failure.

    :param monkeypatch: pytest monkeypatch fixture.
    """

    async def _boom(*_args, **_kwargs):
        """Fail loudly instead of running anything during a check.

        :raises AssertionError: always.
        """
        raise AssertionError("check executed a task")

    monkeypatch.setattr("flow_atelier.services.executor.bash.BashExecutor.execute", _boom)
    monkeypatch.setattr(
        "flow_atelier.services.executor.harness.AcpHarnessExecutor.execute", _boom
    )
    monkeypatch.setattr(
        "flow_atelier.services.executor.harness._ProbeClient.__init__",
        lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("check started ACP")),
    )


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


def _shape(row):
    """Assert one row still carries exactly the six documented keys.

    :param row: one parsed result row.
    """
    assert set(row) == {"name", "source", "path", "ok", "error", "required_inputs"}
    if row["ok"]:
        assert row["error"] is None and isinstance(row["required_inputs"], list)
    else:
        assert isinstance(row["error"], str) and row["error"]
        assert row["required_inputs"] is None


def _flow_ids(workdir) -> list[str]:
    """Return the recorded flow ids, tolerating a store that made the folder.

    :param workdir: working directory path.
    :returns: sorted flow directory names, empty when nothing ever ran.
    """
    flows = workdir / ".atelier" / "flows"
    return sorted(p.name for p in flows.iterdir()) if flows.exists() else []


def _chain_of(workdir, depth: int, leaf: str = BASH) -> None:
    """Install ``c0 -> c1 -> ... -> c<depth-1>``, the last one a plain task.

    :param workdir: working directory path.
    :param depth: how many conduits the chain holds, including the root.
    :param leaf: YAML template for the final conduit.
    """
    for i in range(depth - 1):
        _write(workdir, f"c{i}", _calls(f"c{i}", f"c{i + 1}"))
    _write(workdir, f"c{depth - 1}", leaf.format(name=f"c{depth - 1}"))


# --------------------------------------------------------------- happy paths


def test_a_valid_three_level_composition_passes(workdir):
    """A root, its child and its grandchild all load, validate and are ready."""
    _write(workdir, "root", _calls("root", "mid"))
    _write(workdir, "mid", _calls("mid", "leaf"))
    _write(workdir, "leaf", BASH.format(name="leaf"))
    _, rows = _report(["root", "--recursive"], expect_exit=0)
    assert len(rows) == 1
    _shape(rows[0])
    assert rows[0]["ok"] is True and rows[0]["name"] == "root"


def test_a_root_with_no_nested_calls_behaves_exactly_as_before(workdir):
    """The flag is inert when there is nothing to follow."""
    _write(workdir, "plain", BASH.format(name="plain"))
    _, plain_rows = _report(["plain"], expect_exit=0)
    _, deep_rows = _report(["plain", "--recursive"], expect_exit=0)
    assert plain_rows == deep_rows


def test_whitespace_around_a_target_is_stripped_like_the_executor_does(workdir):
    """`task: "  leaf  "` names `leaf`, both here and at run time."""
    _write(workdir, "root", _calls("root", "   leaf   "))
    _write(workdir, "leaf", BASH.format(name="leaf"))
    _, rows = _report(["root", "--recursive"], expect_exit=0)
    assert rows[0]["ok"] is True


def test_a_global_child_is_followed_from_a_project_root(workdir):
    """Child lookup uses the same project-then-global order a run would."""
    _write(workdir, "root", _calls("root", "shared"))
    _write(workdir, "shared", BASH.format(name="shared"), source="global")
    _, rows = _report(["root", "--recursive"], expect_exit=0)
    assert rows[0]["ok"] is True


# ------------------------------------------------------------- broken children


def test_a_missing_direct_child_fails_only_in_recursive_mode(workdir):
    """The gap the flag exists to close: the parent file is perfectly valid."""
    _write(workdir, "root", _calls("root", "nope"))
    _, shallow = _report(["root"], expect_exit=0)
    assert shallow[0]["ok"] is True

    result, rows = _report(["root", "--recursive"], expect_exit=1)
    _shape(rows[0])
    assert rows[0]["name"] == "root" and rows[0]["source"] == "project"
    assert rows[0]["path"].endswith(f"conduits{os.sep}root{os.sep}conduit.yaml")
    assert rows[0]["error"] == "root.call0 -> nope — conduit not found"
    assert result.stderr == ""


def test_a_missing_grandchild_names_the_whole_calling_chain(workdir):
    """Two hops down, the report still says which steps led there."""
    _write(workdir, "root", _calls("root", "mid"))
    _write(workdir, "mid", _calls("mid", "gone", task="hop"))
    _, rows = _report(["root", "--recursive"], expect_exit=1)
    assert rows[0]["error"] == "root.call0 -> mid.hop0 -> gone — conduit not found"


@pytest.mark.parametrize(
    ("body", "says"),
    [
        ("name: kid\ndescription: [unclosed\n", "invalid YAML"),
        (BASH.format(name="kid") + "max_concurrency: 0\n", "greater than or equal to 1"),
        (BASH.format(name="other"), "folder name"),
        (
            "name: kid\ndescription: d\ntasks:\n  - name: a\n    description: a\n"
            "    task: echo hi\n    tool: tool:bash\n    depends_on: [nope]\n",
            "unknown task 'nope'",
        ),
        (b"\xff\xfe\x00name: kid\n", "cannot read"),
    ],
)
def test_each_kind_of_broken_child_fails_the_root_with_its_own_file(
    workdir, body, says
):
    """Bad YAML, a bad field, a name mismatch, a bad edge and raw bytes."""
    _write(workdir, "root", _calls("root", "kid"))
    child = _write(workdir, "kid", body)
    _, shallow = _report(["root"], expect_exit=0)
    assert shallow[0]["ok"] is True

    _, rows = _report(["root", "--recursive"], expect_exit=1)
    _shape(rows[0])
    assert rows[0]["error"].startswith("root.call0 -> kid (")
    assert str(child.absolute()) in rows[0]["error"]
    assert says in rows[0]["error"]


def test_an_unavailable_child_tool_fails_the_root(workdir, monkeypatch):
    """Readiness reaches into the call tree, not just the selected file."""
    monkeypatch.setattr(
        "flow_atelier.services.executor.harness.shutil.which", lambda _b: None
    )
    _write(workdir, "root", _calls("root", "kid"))
    _write(
        workdir,
        "kid",
        "name: kid\ndescription: d\ntasks:\n  - name: build\n    description: b\n"
        "    task: do it\n    tool: harness:claude-code\n",
    )
    assert _report(["root"], expect_exit=0)[1][0]["ok"] is True
    _, rows = _report(["root", "--recursive"], expect_exit=1)
    assert "root.call0 -> kid (" in rows[0]["error"]
    assert "build" in rows[0]["error"]


def test_an_unavailable_child_supervisor_fails_the_root(workdir, monkeypatch):
    """A supervised child needs its harness too, and the facade already knows."""
    monkeypatch.setattr(
        "flow_atelier.services.executor.harness.shutil.which", lambda _b: None
    )
    _write(workdir, "root", _calls("root", "kid"))
    _write(
        workdir,
        "kid",
        BASH.format(name="kid")
        + "interaction:\n  questions: supervisor\n"
        "  supervisor:\n    tool: harness:claude-code\n",
    )
    assert _report(["root"], expect_exit=0)[1][0]["ok"] is True
    _, rows = _report(["root", "--recursive"], expect_exit=1)
    assert "supervisor" in rows[0]["error"]


def test_a_broken_project_child_is_never_swapped_for_the_global_one(workdir):
    """Shadowing is checked as it runs: no silent fallback to the good copy."""
    _write(workdir, "root", _calls("root", "kid"))
    broken = _write(workdir, "kid", "name: kid\ndescription: [unclosed\n")
    _write(workdir, "kid", BASH.format(name="kid"), source="global")
    _, rows = _report(["root", "--recursive"], expect_exit=1)
    assert str(broken.absolute()) in rows[0]["error"]
    assert "invalid YAML" in rows[0]["error"]


# ------------------------------------------------------------ graph behaviour


def test_a_conduit_that_calls_itself_is_a_cycle_not_a_recursion(workdir):
    """Bounded and readable, where the engine would only find out at run time."""
    _write(workdir, "loop", _calls("loop", "loop"))
    assert _report(["loop"], expect_exit=0)[1][0]["ok"] is True
    result, rows = _report(["loop", "--recursive"], expect_exit=1)
    assert rows[0]["error"] == "loop.call0 -> loop — nested conduit cycle detected"
    assert "RecursionError" not in result.output


def test_two_conduits_calling_each_other_are_a_cycle(workdir):
    """Neither file is wrong on its own; the pair is."""
    _write(workdir, "ping", _calls("ping", "pong"))
    _write(workdir, "pong", _calls("pong", "ping"))
    for name in ("ping", "pong"):
        assert _report([name], expect_exit=0)[1][0]["ok"] is True
    _, rows = _report(["ping", "--recursive"], expect_exit=1)
    assert rows[0]["error"] == (
        "ping.call0 -> pong.call0 -> ping — nested conduit cycle detected"
    )


def test_calling_the_same_child_twice_is_not_a_cycle(workdir):
    """A repeated call is ordinary reuse, not a loop."""
    _write(workdir, "root", _calls("root", "leaf", "leaf"))
    _write(workdir, "leaf", BASH.format(name="leaf"))
    assert _report(["root", "--recursive"], expect_exit=0)[1][0]["ok"] is True


def test_a_diamond_sharing_one_valid_child_passes(workdir):
    """Two branches meeting on the same grandchild is a DAG, not a cycle."""
    _write(workdir, "root", _calls("root", "left", "right"))
    _write(workdir, "left", _calls("left", "leaf"))
    _write(workdir, "right", _calls("right", "leaf"))
    _write(workdir, "leaf", BASH.format(name="leaf"))
    assert _report(["root", "--recursive"], expect_exit=0)[1][0]["ok"] is True


def test_a_chain_exactly_at_the_engine_depth_limit_passes(workdir):
    """The limit counts the root, and the last allowed level is still fine."""
    _chain_of(workdir, MAX_NESTED_CONDUIT_DEPTH)
    assert _report(["c0", "--recursive"], expect_exit=0)[1][0]["ok"] is True


def test_one_level_beyond_the_depth_limit_fails(workdir):
    """The chain the engine would refuse to run is refused here instead."""
    _chain_of(workdir, MAX_NESTED_CONDUIT_DEPTH + 1)
    result, rows = _report(["c0", "--recursive"], expect_exit=1)
    assert f"nested conduit depth exceeded {MAX_NESTED_CONDUIT_DEPTH}" in rows[0]["error"]
    assert f"-> c{MAX_NESTED_CONDUIT_DEPTH}" in rows[0]["error"]
    assert "RecursionError" not in result.output


def test_a_shared_child_seen_shallow_first_still_fails_on_the_deep_path(workdir):
    """Caching must not hide the long path through an already-checked child.

    `shared` is reached at level 1 and again at the last allowed level, where
    its own child tips the chain over. Keying the walk by name alone would
    report this composition as fine.
    """
    limit = MAX_NESTED_CONDUIT_DEPTH
    # root -> shared (level 1) listed first, then root -> a1 -> ... -> shared.
    _write(workdir, "root", _calls("root", "shared", "a1"))
    for i in range(1, limit - 2):
        _write(workdir, f"a{i}", _calls(f"a{i}", f"a{i + 1}"))
    _write(workdir, f"a{limit - 2}", _calls(f"a{limit - 2}", "shared"))
    _write(workdir, "shared", _calls("shared", "tail"))
    _write(workdir, "tail", BASH.format(name="tail"))

    _, rows = _report(["root", "--recursive"], expect_exit=1)
    assert rows[0]["error"].endswith(
        f"a{limit - 2}.call0 -> shared.call0 -> tail "
        f"— nested conduit depth exceeded {limit}"
    ), rows[0]["error"]


# ----------------------------------------------------------- static boundaries


@pytest.mark.parametrize(
    "target", ["{{inputs.which}}", "{{step.output}}", "prefix-{{inputs.which}}"]
)
def test_a_templated_target_is_refused_rather_than_guessed(workdir, target):
    """Including one whose input has a default: no resolution without a run."""
    _write(
        workdir,
        "root",
        "name: root\ndescription: d\ninputs:\n  which:\n    description: d\n"
        "    default: leaf\ntasks:\n"
        "  - name: step\n    description: a\n    task: echo leaf\n"
        "    tool: tool:bash\n"
        "  - name: call0\n    description: c\n"
        f'    task: "{target}"\n    tool: tool:conduit\n'
        "    depends_on: [step]\n",
    )
    _write(workdir, "leaf", BASH.format(name="leaf"))
    assert _report(["root"], expect_exit=0)[1][0]["ok"] is True

    _, rows = _report(["root", "--recursive"], expect_exit=1)
    assert rows[0]["error"].startswith("root.call0 — cannot recursively check")
    assert "check its resolved target separately" in rows[0]["error"]


def test_a_conditionally_skipped_call_is_still_inspected(workdir):
    """Conservative by design: a loop predicate does not excuse a bad child."""
    _write(
        workdir,
        "root",
        "name: root\ndescription: d\ntasks:\n"
        "  - name: step\n    description: a\n    task: echo hi\n    tool: tool:bash\n"
        "  - name: call0\n    description: c\n    task: gone\n"
        "    tool: tool:conduit\n    depends_on: [step]\n"
        "    repeat: 3\n    until: \"output.match(done)\"\n",
    )
    _, rows = _report(["root", "--recursive"], expect_exit=1)
    assert rows[0]["error"] == "root.call0 -> gone — conduit not found"


def test_child_inputs_never_become_parent_cli_requirements(workdir):
    """`required_inputs` stays the root's own declarations."""
    _write(
        workdir,
        "root",
        "name: root\ndescription: d\ninputs:\n  topic:\n    description: d\n"
        "tasks:\n  - name: call0\n    description: c\n    task: kid\n"
        "    tool: tool:conduit\n",
    )
    _write(
        workdir,
        "kid",
        "name: kid\ndescription: d\ninputs:\n  finding:\n    description: d\n"
        "tasks:\n  - name: a\n    description: a\n"
        '    task: "echo {{inputs.finding}}"\n    tool: tool:bash\n',
    )
    _, rows = _report(["root", "--recursive"], expect_exit=0)
    assert rows[0]["required_inputs"] == ["topic"]


# -------------------------------------------------------------- report contract


def test_a_named_recursive_check_ignores_unrelated_broken_conduits(workdir):
    """Selection is unchanged: only the named root and what it reaches."""
    _write(workdir, "root", _calls("root", "leaf"))
    _write(workdir, "leaf", BASH.format(name="leaf"))
    _write(workdir, "unrelated", "name: unrelated\ndescription: [unclosed\n")
    _, rows = _report(["root", "--recursive"], expect_exit=0)
    assert [r["name"] for r in rows] == ["root"]


def test_a_failed_root_does_not_stop_the_next_one(workdir):
    """Batch recursion reports every root, in the store's sorted order."""
    _write(workdir, "aaa-root", _calls("aaa-root", "gone"))
    _write(workdir, "zzz-fine", BASH.format(name="zzz-fine"))
    _, rows = _report(["--recursive"], expect_exit=1)
    assert [(r["name"], r["ok"]) for r in rows] == [
        ("aaa-root", False), ("zzz-fine", True)
    ]
    for row in rows:
        _shape(row)


def test_text_and_json_agree_about_every_root(workdir):
    """The two modes differ in formatting only, never in the verdict."""
    _write(workdir, "aaa-root", _calls("aaa-root", "gone"))
    _write(workdir, "zzz-fine", BASH.format(name="zzz-fine"))
    plain = CliRunner().invoke(app, ["check", "--recursive"])
    json_result, rows = _report(["--recursive"], expect_exit=plain.exit_code)

    assert plain.exit_code == 1 and json_result.exit_code == 1
    assert "OK" in plain.stdout and "FAIL" in plain.stdout
    assert "aaa-root.call0 -> gone" in plain.stdout
    assert {r["name"]: r["ok"] for r in rows} == {"aaa-root": False, "zzz-fine": True}
    assert "Traceback" not in plain.output


def test_an_empty_store_still_reports_an_empty_array(workdir):
    """Nothing to check is not a failure, with or without the flag."""
    _, rows = _report(["--recursive"], expect_exit=0)
    assert rows == []


def test_an_unknown_name_is_still_a_command_error(workdir):
    """Pre-selection failures keep their own convention: empty stdout, stderr."""
    _write(workdir, "root", _calls("root", "leaf"))
    result = CliRunner().invoke(app, ["check", "roo", "--recursive", "--json"])
    assert result.exit_code == 1
    assert result.stdout == ""
    assert "unknown conduit" in result.stderr
    assert "Traceback" not in result.output


def test_help_mentions_the_flag_and_its_limits(workdir):
    """`--help` states the boundary before anyone relies on the check."""
    result = CliRunner().invoke(app, ["check", "--help"])
    assert result.exit_code == 0
    assert "--recursive" in result.output


# --------------------------------------------------------------- error boundary


@pytest.mark.parametrize("method", ["conduit_dir", "read_conduit"])
def test_a_child_io_failure_stays_inside_its_root_row(workdir, monkeypatch, method):
    """Resolving and reading a child are both inside the boundary."""
    _write(workdir, "aaa-root", _calls("aaa-root", "locked"))
    _write(workdir, "locked", BASH.format(name="locked"))
    _write(workdir, "zzz-fine", BASH.format(name="zzz-fine"))
    original = getattr(FilesystemStore, method)

    def _boom(self, name):
        """Raise for the locked child only, delegating for every other name.

        :param self: the store instance.
        :param name: conduit name being resolved or read.
        :returns: whatever the real method returns for other conduits.
        :raises PermissionError: for ``locked``.
        """
        if name == "locked":
            raise PermissionError(13, "Permission denied")
        return original(self, name)

    monkeypatch.setattr(FilesystemStore, method, _boom)
    result, rows = _report(["--recursive"], expect_exit=1)

    assert [r["name"] for r in rows] == ["aaa-root", "locked", "zzz-fine"]
    _shape(rows[0])
    assert rows[0]["ok"] is False
    assert "aaa-root.call0 -> locked" in rows[0]["error"]
    assert "Permission denied" in rows[0]["error"]
    assert rows[2]["ok"] is True
    assert "Traceback" not in result.output


@pytest.mark.skipif(
    not hasattr(os, "geteuid") or os.geteuid() == 0,
    reason="needs POSIX file modes the current user cannot bypass",
)
def test_a_really_unreadable_child_is_a_failed_root(workdir):
    """Not fault injection: a mode the filesystem actually enforces."""
    _write(workdir, "root", _calls("root", "locked"))
    locked = _write(workdir, "locked", BASH.format(name="locked"))
    locked.chmod(0o000)
    try:
        result, rows = _report(["root", "--recursive"], expect_exit=1)
    finally:
        locked.chmod(stat.S_IRUSR | stat.S_IWUSR)

    assert rows[0]["name"] == "root" and rows[0]["ok"] is False
    assert "root.call0 -> locked" in rows[0]["error"]
    assert "Traceback" not in result.output


def test_a_recursive_check_records_no_flow_and_touches_nothing(workdir):
    """The whole tree is read; not one step of it runs."""
    sentinel = workdir / "ran.txt"
    _write(workdir, "root", _calls("root", "kid"))
    _write(
        workdir,
        "kid",
        "name: kid\ndescription: d\ntasks:\n  - name: a\n    description: a\n"
        f'    task: "touch {sentinel}"\n    tool: tool:bash\n',
    )
    _report(["root", "--recursive"], expect_exit=0)
    assert not sentinel.exists()
    assert _flow_ids(workdir) == []


# ------------------------------------------------------------ README as a test


def _readme_script() -> str:
    """Return the README section's bash blocks, concatenated in order.

    :returns: one shell script, with no error handling the README omits.
    """
    body = _README.read_text(encoding="utf-8").split(_SECTION, 1)[1]
    body = body.split("\n### ", 1)[0]
    blocks = [m.group(1) for m in _BASH_BLOCK.finditer(body)]
    assert len(blocks) == 3, f"expected 3 bash blocks in the section, got {len(blocks)}"
    assert "--recursive \\\n  && atelier run report" in blocks[2], (
        "the published sequence no longer gates the run on the recursive check"
    )
    # Deliberately no `set -e`: the published block has to survive on its own
    # gating, and its final status is the status this test reads.
    tail = (
        "\nstatus=$?\n"
        'printf "%s\\n" "$workspace" > "$WORKSPACE_MARKER"\n'
        "exit $status\n"
    )
    return "\n".join(blocks) + tail


@pytest.fixture
def readme_env(tmp_path, request):
    """An isolated environment whose `atelier` is this checkout's CLI.

    :param tmp_path: pytest temp directory fixture.
    :param request: pytest request, whose ``param`` breaks `check` when true.
    :yields: ``(env, marker)`` — the child environment and the file the script
        writes its workspace path into.
    """
    break_check = getattr(request, "param", False)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    shim = bin_dir / "atelier"
    guard = (
        'if [ "$1" = check ]; then echo "INJECTED check failure" >&2; exit 1; fi\n'
        if break_check
        else ""
    )
    shim.write_text(f'#!/bin/sh\n{guard}exec "{sys.executable}" -c \'{_CLI}\' "$@"\n')
    shim.chmod(0o755)
    marker = tmp_path / "workspace-path"

    env = {k: v for k, v in os.environ.items() if not k.startswith("ATELIER_")}
    env["ATELIER_GLOBAL_ATELIER_DIR"] = str(tmp_path / "global")
    env["ATELIER_NO_UPDATE_CHECK"] = "1"
    env["PATH"] = f"{bin_dir}{os.pathsep}{env.get('PATH', '')}"
    env["WORKSPACE_MARKER"] = str(marker)
    yield env, marker
    # `mktemp -d` puts the workspace outside tmp_path, and the README never
    # tells a reader to delete it, so the test is what cleans up.
    if marker.exists():
        shutil.rmtree(Path(marker.read_text().strip()).parent, ignore_errors=True)


def _run_readme(env, tmp_path):
    """Execute the README section as one shell script.

    :param env: child environment from the fixture.
    :param tmp_path: pytest temp directory fixture.
    :returns: the completed subprocess.
    """
    script = tmp_path / "section.sh"
    script.write_text(_readme_script())
    return subprocess.run(
        ["bash", str(script)],
        cwd=tmp_path,
        env=env,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=180,
    )


def test_the_readme_section_runs_and_claims_only_what_happens(readme_env, tmp_path):
    """Fail on the missing child, add it, check, run, read the saved result."""
    env, marker = readme_env
    run = _run_readme(env, tmp_path)
    assert run.returncode == 0, f"stdout:\n{run.stdout}\nstderr:\n{run.stderr}"
    out = run.stdout

    assert "report.summarise -> summary — conduit not found" in out
    assert "summary of 42" in out

    workspace = Path(marker.read_text().strip())
    assert " " in workspace.name, "the workspace path should contain a space"
    # One preparation: neither failed check executed a step.
    assert (workspace / "preparation.log").read_text() == "prepared\n"

    store = FilesystemStore(workspace / ".atelier")
    flows = store.list_flows("report")
    assert len(flows) == 1
    assert store.read_progress(flows[0]).status.value == "completed"
    assert store.read_outputs(flows[0])["summarise"].strip() == "summary of 42"


@pytest.mark.parametrize("readme_env", [True], indirect=True)
def test_the_readme_sequence_cannot_show_a_result_it_did_not_produce(
    readme_env, tmp_path
):
    """Break the gating check: the published block must stop, not sail past."""
    env, marker = readme_env
    run = _run_readme(env, tmp_path)

    assert run.returncode != 0, run.stdout
    assert "INJECTED check failure" in run.stderr
    assert "summary of 42" not in run.stdout

    workspace = Path(marker.read_text().strip())
    assert not (workspace / "preparation.log").exists()
    assert not (workspace / ".atelier" / "flows").exists()
