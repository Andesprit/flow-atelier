"""Tests for `atelier wait`, the bounded observer of an already-started run.

Most tests drive the command through ``CliRunner`` against hand-seeded
progress files and a fake clock, so every deadline and interleaving is
deterministic. The tests at the bottom are the opposite: two real processes,
the real engine and the real ``tool:bash`` executor, checking that the
documented wait-to-results shell expression actually works.
"""
from __future__ import annotations

import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import time as real_time
from pathlib import Path

import pytest
from typer.testing import CliRunner

from flow_atelier.cli import app
from flow_atelier.cli.commands import wait as wait_cmd
from flow_atelier.core.atelier import Atelier
from flow_atelier.schemas.progress import FlowStatus, Progress
from flow_atelier.services.executor.bash import to_bash_path
from flow_atelier.services.store.filesystem import FilesystemStore
from tests._shell import CLI as _CLI
from tests._shell import run_expression, write_shim


@pytest.fixture
def workdir(tmp_path, monkeypatch):
    """Isolated cwd with an empty `.atelier` tree and an isolated global dir."""
    (tmp_path / ".atelier" / "conduits").mkdir(parents=True)
    global_dir = tmp_path / "global"
    (global_dir / "conduits").mkdir(parents=True)
    monkeypatch.chdir(tmp_path)
    for key in list(os.environ):
        if key.startswith("ATELIER_"):
            monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("ATELIER_GLOBAL_ATELIER_DIR", str(global_dir))
    monkeypatch.setenv("ATELIER_NO_UPDATE_CHECK", "1")
    return tmp_path


class FakeClock:
    """Deterministic stand-in for the `time` module inside the wait loop.

    Sleeping advances the clock instead of the wall, and optionally runs a
    callback — which is how a test publishes "the runner finished during our
    sleep" at an exact point in the budget.
    """

    def __init__(self, on_sleep=None):
        """Start at zero.

        :param on_sleep: called with the 1-based sleep count after each sleep.
        """
        self.now = 0.0
        self.sleeps: list[float] = []
        self._on_sleep = on_sleep

    def monotonic(self) -> float:
        """Return the simulated monotonic time.

        :returns: seconds since the clock was created.
        """
        return self.now

    def sleep(self, seconds: float) -> None:
        """Advance the simulated clock and fire the callback.

        :param seconds: how far to move the clock forward.
        """
        self.sleeps.append(seconds)
        self.now += seconds
        if self._on_sleep is not None:
            self._on_sleep(len(self.sleeps))


def _use_clock(monkeypatch, on_sleep=None) -> FakeClock:
    """Install a fake clock in the wait command module.

    :param monkeypatch: pytest monkeypatch fixture.
    :param on_sleep: callback invoked after each simulated sleep.
    :returns: the installed clock.
    """
    clock = FakeClock(on_sleep)
    monkeypatch.setattr(wait_cmd, "time", clock)
    return clock


def _seed(status: FlowStatus = FlowStatus.running, conduit: str = "report", **fields) -> str:
    """Create a flow on disk whose progress says exactly what a test needs.

    :param status: flow status to persist.
    :param conduit: conduit name embedded in the generated flow id.
    :param fields: extra Progress fields (runner pid/host, started_at, ...).
    :returns: the created flow id.
    """
    atelier = Atelier()
    flow_id = atelier.store.create_flow(conduit, {})
    atelier.store.write_progress(flow_id, Progress(status=status, **fields))
    return flow_id


def _write(flow_id: str, status: FlowStatus, **fields) -> None:
    """Overwrite a flow's persisted progress, as its runner would.

    :param flow_id: flow to rewrite.
    :param status: new flow status.
    :param fields: extra Progress fields.
    """
    Atelier().store.write_progress(flow_id, Progress(status=status, **fields))


def _dead_local_pid() -> int:
    """Return a pid that has provably exited on this host.

    :returns: the pid of a reaped child process.
    """
    proc = subprocess.Popen([sys.executable, "-c", ""])
    proc.wait()
    return proc.pid


def _crashed_fields() -> dict:
    """Progress fields describing a locally-dead runner.

    :returns: runner pid/host kwargs that make `is_crashed` true.
    """
    return {"runner_pid": _dead_local_pid(), "runner_host": socket.gethostname()}


# ------------------------------------------------------------------ outcomes


def test_wait_prints_the_id_when_the_run_completes(workdir, monkeypatch):
    """A run that finishes while we watch exits 0 with only its id on stdout."""
    flow_id = _seed()
    _use_clock(monkeypatch, lambda n: _write(flow_id, FlowStatus.completed) if n == 2 else None)

    result = CliRunner().invoke(app, ["wait", flow_id])

    assert result.exit_code == 0, result.output
    assert result.stdout == f"{flow_id}\n"
    assert "waiting for" in result.stderr


def test_wait_succeeds_immediately_for_an_already_finished_run(workdir, monkeypatch):
    """A completed run answers on the first read, without sleeping or banners."""
    flow_id = _seed(FlowStatus.completed)
    clock = _use_clock(monkeypatch)

    result = CliRunner().invoke(app, ["wait", flow_id])

    assert result.exit_code == 0, result.output
    assert result.stdout == f"{flow_id}\n"
    assert clock.sleeps == []
    assert result.stderr == ""


@pytest.mark.parametrize("status", [FlowStatus.failed, FlowStatus.stopped])
def test_wait_exits_1_for_an_unsuccessful_run(workdir, monkeypatch, status):
    """Failed and stopped runs exit 1, name the state, and print no id."""
    flow_id = _seed(status)
    _use_clock(monkeypatch)

    result = CliRunner().invoke(app, ["wait", flow_id])

    assert result.exit_code == 1
    assert result.stdout == ""
    assert f"flow {flow_id} {status.value}" in result.stderr
    assert f"atelier status {flow_id}" in result.stderr
    assert f"atelier logs {flow_id}" in result.stderr


def test_wait_exits_1_and_offers_resume_when_the_runner_died(workdir, monkeypatch):
    """A frozen `running` file whose local runner is gone is reported crashed."""
    flow_id = _seed(**_crashed_fields())
    _use_clock(monkeypatch)

    result = CliRunner().invoke(app, ["wait", flow_id])

    assert result.exit_code == 1
    assert result.stdout == ""
    assert f"flow {flow_id} crashed" in result.stderr
    assert f"atelier run --resume {flow_id}" in result.stderr


def test_wait_exits_124_when_the_budget_expires(workdir, monkeypatch):
    """A still-running flow times out with 124 and says so, not 'failed'."""
    flow_id = _seed(runner_pid=os.getpid(), runner_host=socket.gethostname())
    clock = _use_clock(monkeypatch)

    result = CliRunner().invoke(app, ["wait", flow_id, "--timeout", "1"])

    assert result.exit_code == 124
    assert result.stdout == ""
    assert "timed out after 1s" in result.stderr
    assert "still running" in result.stderr
    assert "failed" not in result.stderr
    # The budget is spent in whole poll intervals, never overshot.
    assert sum(clock.sleeps) == pytest.approx(1.0)


@pytest.mark.parametrize("bad", ["0", "-5"])
def test_wait_rejects_a_non_positive_timeout(workdir, bad):
    """A zero or negative budget is a usage error (exit 2), not a wait."""
    flow_id = _seed()
    result = CliRunner().invoke(app, ["wait", flow_id, "--timeout", bad])
    assert result.exit_code == 2
    assert "positive" in result.output


def test_wait_rejects_a_non_numeric_timeout(workdir):
    """A non-integer budget is rejected by the parser with exit 2."""
    flow_id = _seed()
    result = CliRunner().invoke(app, ["wait", flow_id, "--timeout", "soon"])
    assert result.exit_code == 2


def test_wait_exits_130_on_ctrl_c(workdir, monkeypatch):
    """Ctrl-C stops the observer with 130 and says the run is untouched."""
    flow_id = _seed()

    def _interrupt(_n):
        """Raise the interrupt a user's Ctrl-C would deliver during the sleep."""
        raise KeyboardInterrupt

    _use_clock(monkeypatch, _interrupt)

    result = CliRunner().invoke(app, ["wait", flow_id])

    assert result.exit_code == 130
    assert result.stdout == ""
    assert "interrupted" in result.stderr
    assert "keeps running" in result.stderr


def test_wait_polls_progress_and_nothing_else(workdir, monkeypatch):
    """The loop reads progress only — never the logs, steps or outputs files."""
    flow_id = _seed()
    _use_clock(monkeypatch, lambda n: _write(flow_id, FlowStatus.completed) if n == 3 else None)

    def _forbidden(name):
        """Build a store method that fails the test if the loop calls it."""
        def _call(*args, **kwargs):
            raise AssertionError(f"wait must not read {name}")

        return _call

    for method in ("read_logs", "read_steps", "read_outputs"):
        monkeypatch.setattr(FilesystemStore, method, _forbidden(method))

    result = CliRunner().invoke(app, ["wait", flow_id])

    assert result.exit_code == 0, result.output


# ------------------------------------------------------------------ identity


def test_wait_holds_the_flow_latest_resolved(workdir, monkeypatch):
    """`latest` is resolved once: a newer run starting mid-wait never steals it."""
    first = _seed(started_at="2026-01-01T10:00:00Z")
    later: list[str] = []

    def _on_sleep(n):
        """Start a newer run during the wait, then finish the original one."""
        if n == 1:
            later.append(_seed(conduit="other", started_at="2026-01-01T12:00:00Z"))
        if n == 2:
            _write(first, FlowStatus.completed)

    _use_clock(monkeypatch, _on_sleep)

    result = CliRunner().invoke(app, ["wait", "latest"])

    assert result.exit_code == 0, result.output
    assert result.stdout == f"{first}\n"
    assert later and later[0] not in result.stdout
    # The alias is announced exactly once, so it was resolved exactly once.
    assert result.stderr.count("latest: ") == 1


def test_wait_accepts_a_unique_prefix(workdir, monkeypatch):
    """A git-style unique prefix resolves to the full id it prints on success."""
    flow_id = _seed(FlowStatus.completed)
    _use_clock(monkeypatch)

    result = CliRunner().invoke(app, ["wait", flow_id[:12]])

    assert result.exit_code == 0, result.output
    assert result.stdout == f"{flow_id}\n"


def test_wait_accepts_an_exact_child_id(workdir, monkeypatch):
    """A nested sub-conduit run can be waited on by its exact id."""
    atelier = Atelier()
    parent = atelier.store.create_flow("parent", {})
    child = atelier.store.create_flow("child", {}, parent_flow_id=parent)
    atelier.store.write_progress(child, Progress(status=FlowStatus.completed))
    _use_clock(monkeypatch)

    result = CliRunner().invoke(app, ["wait", child])

    assert result.exit_code == 0, result.output
    assert result.stdout == f"{child}\n"


def test_wait_reports_an_unknown_id(workdir, monkeypatch):
    """An id nothing matches exits 1 with the resolver's usual hint."""
    _seed()
    _use_clock(monkeypatch)

    result = CliRunner().invoke(app, ["wait", "nope"])

    assert result.exit_code == 1
    assert "unknown flow" in result.output


def test_wait_reports_an_ambiguous_prefix(workdir, monkeypatch):
    """A prefix matching several runs exits 1 and lists them."""
    Atelier().store.create_flow("report", {}, flow_id="20260101_aaaaaaaa_report")
    Atelier().store.create_flow("report", {}, flow_id="20260101_bbbbbbbb_report")
    _use_clock(monkeypatch)

    result = CliRunner().invoke(app, ["wait", "20260101"])

    assert result.exit_code == 1
    assert "ambiguous flow id" in result.output


def test_wait_latest_with_no_flows_exits_1(workdir, monkeypatch):
    """`latest` against an empty store is an unknown flow, not a wait."""
    _use_clock(monkeypatch)

    result = CliRunner().invoke(app, ["wait", "latest"])

    assert result.exit_code == 1
    assert "no flows recorded yet" in result.output


# ------------------------------------------------------------------ bad reads


def test_wait_fails_cleanly_on_an_invalid_child_record(workdir, monkeypatch):
    """A corrupt child progress breaks resolution with one line, not a traceback."""
    atelier = Atelier()
    parent = atelier.store.create_flow("parent", {})
    child = atelier.store.create_flow("child", {}, parent_flow_id=parent)
    (atelier.store._flow_dir(child) / "progress.json").write_text("{not json")
    _use_clock(monkeypatch)

    result = CliRunner().invoke(app, ["wait", child])

    assert result.exit_code == 1
    assert "cannot resolve a flow to wait for" in result.stderr
    assert result.exception is None or isinstance(result.exception, SystemExit)


def test_wait_fails_cleanly_when_the_run_is_deleted_mid_wait(workdir, monkeypatch):
    """Deleting the run while we watch ends the wait at 1, never at success."""
    flow_id = _seed()
    flow_dir = Atelier().store._flow_dir(flow_id)
    _use_clock(monkeypatch, lambda n: shutil.rmtree(flow_dir) if n == 1 else None)

    result = CliRunner().invoke(app, ["wait", flow_id])

    assert result.exit_code == 1
    assert result.stdout == ""
    assert "unknown flow" in result.stderr


def test_wait_fails_cleanly_when_progress_becomes_corrupt(workdir, monkeypatch):
    """A truncated progress.json mid-wait is reported, not parsed into success."""
    flow_id = _seed()
    path = Atelier().store._flow_dir(flow_id) / "progress.json"
    _use_clock(monkeypatch, lambda n: path.write_text('{"status": ') if n == 1 else None)

    result = CliRunner().invoke(app, ["wait", flow_id])

    assert result.exit_code == 1
    assert result.stdout == ""
    assert "invalid progress record" in result.stderr


def test_wait_fails_cleanly_when_progress_cannot_be_read(workdir, monkeypatch):
    """A permission error reading progress exits 1 with the OS reason.

    Injected rather than reproduced with file modes: a test running as root,
    or on a filesystem that ignores them, would otherwise read the file fine.
    """
    flow_id = _seed()
    _use_clock(monkeypatch)

    def _denied(self, _flow_id):
        """Stand in for an unreadable progress.json."""
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(FilesystemStore, "read_progress", _denied)

    result = CliRunner().invoke(app, ["wait", flow_id])

    assert result.exit_code == 1
    assert "cannot read progress" in result.stderr
    assert "Permission denied" in result.stderr


# ------------------------------------------------------------------ crash race


def test_wait_rereads_progress_after_the_liveness_probe(workdir, monkeypatch):
    """A runner that finished and exited during the probe is a success, not a crash."""
    flow_id = _seed(**_crashed_fields())
    _use_clock(monkeypatch)

    def _crashed_but_actually_done(progress):
        """Publish completion during the probe, exactly as the real race does."""
        _write(flow_id, FlowStatus.completed)
        return True

    monkeypatch.setattr(wait_cmd, "is_crashed", _crashed_but_actually_done)

    result = CliRunner().invoke(app, ["wait", flow_id])

    assert result.exit_code == 0, result.output
    assert result.stdout == f"{flow_id}\n"
    assert "crashed" not in result.stderr


def test_wait_does_not_blame_a_resumed_runner_for_the_old_crash(workdir, monkeypatch):
    """When `run --resume` installs a live runner, the wait continues instead of failing."""
    flow_id = _seed(**_crashed_fields())
    real_is_crashed = wait_cmd.is_crashed
    probes: list[int] = []

    def _probe(progress):
        """Look crashed once, with a resume landing during that probe."""
        probes.append(1)
        if len(probes) == 1:
            _write(
                flow_id,
                FlowStatus.running,
                runner_pid=os.getpid(),
                runner_host=socket.gethostname(),
            )
            return True
        return real_is_crashed(progress)

    monkeypatch.setattr(wait_cmd, "is_crashed", _probe)
    _use_clock(monkeypatch, lambda n: _write(flow_id, FlowStatus.completed) if n == 1 else None)

    result = CliRunner().invoke(app, ["wait", flow_id])

    assert result.exit_code == 0, result.output
    assert result.stdout == f"{flow_id}\n"
    assert "crashed" not in result.stderr


@pytest.mark.parametrize(
    "fields",
    [
        {"runner_pid": None, "runner_host": socket.gethostname()},
        {"runner_pid": 424242, "runner_host": "some-other-host"},
    ],
    ids=["no-pid", "foreign-host"],
)
def test_wait_keeps_waiting_when_liveness_is_unknown(workdir, monkeypatch, fields):
    """Unprovable liveness is never a crash: the wait runs out its budget instead."""
    flow_id = _seed(**fields)
    _use_clock(monkeypatch)

    result = CliRunner().invoke(app, ["wait", flow_id, "--timeout", "1"])

    assert result.exit_code == 124
    assert "crashed" not in result.stderr


def test_wait_accepts_a_completion_observed_at_the_deadline(workdir, monkeypatch):
    """Work finishing on the very last sleep still succeeds, rather than timing out."""
    flow_id = _seed()
    clock = _use_clock(
        monkeypatch,
        lambda n: _write(flow_id, FlowStatus.completed) if n == 4 else None,
    )

    result = CliRunner().invoke(app, ["wait", flow_id, "--timeout", "1"])

    assert result.exit_code == 0, result.output
    assert result.stdout == f"{flow_id}\n"
    assert clock.now == pytest.approx(1.0)


# ------------------------------------------------------------------ read-only


@pytest.mark.parametrize("status", [FlowStatus.completed, FlowStatus.failed])
def test_wait_writes_nothing(workdir, monkeypatch, status):
    """Observing a finished run leaves every saved file byte-for-byte untouched."""
    flow_id = _seed(status)
    _use_clock(monkeypatch)
    flows = workdir / ".atelier" / "flows"

    def _snapshot() -> dict[str, tuple[int, int]]:
        """Record size and mtime of every file under the flows directory."""
        return {
            str(p.relative_to(flows)): (p.stat().st_size, p.stat().st_mtime_ns)
            for p in sorted(flows.rglob("*"))
            if p.is_file()
        }

    before = _snapshot()
    result = CliRunner().invoke(app, ["wait", flow_id])

    assert result.exit_code == (0 if status is FlowStatus.completed else 1)
    assert _snapshot() == before


# ------------------------------------------------------------ real processes

WAITER_CONDUIT = """
name: waiter
description: hold until released, then report
tasks:
  - work:
      description: wait for the release file
      task: "touch '{ready}'; while [ ! -f '{release}' ]; do sleep 0.05; done; echo released"
      tool: tool:bash
      depends_on: []
  - never:
      description: only on an impossible verdict
      task: "echo unreachable"
      tool: tool:bash
      depends_on:
        - work.output.match(IMPOSSIBLE)
"""

BOOM_CONDUIT = """
name: boomer
description: fail on purpose
tasks:
  - boom:
      description: fail
      task: "echo nope; exit 3"
      tool: tool:bash
      depends_on: []
"""


@pytest.fixture
def real_project(tmp_path, monkeypatch):
    """A disposable project plus an `atelier` shim, for two-process tests.

    :returns: ``(work, env)`` — the project directory and the child environment
        whose PATH runs this interpreter's CLI as plain ``atelier``.
    """
    work = tmp_path / "project"
    (work / ".atelier" / "conduits").mkdir(parents=True)
    (tmp_path / "global" / "conduits").mkdir(parents=True)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    write_shim(bin_dir)

    env = {k: v for k, v in os.environ.items() if not k.startswith("ATELIER_")}
    env["ATELIER_GLOBAL_ATELIER_DIR"] = str(tmp_path / "global")
    env["ATELIER_NO_UPDATE_CHECK"] = "1"
    env["PATH"] = f"{bin_dir}{os.pathsep}{env.get('PATH', '')}"
    monkeypatch.chdir(work)
    return work, env


def _install(work: Path, name: str, body: str) -> None:
    """Write a conduit into the disposable project.

    :param work: project directory.
    :param name: conduit name (and directory).
    :param body: conduit YAML.
    """
    d = work / ".atelier" / "conduits" / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "conduit.yaml").write_text(body)


def _atelier(work: Path, env: dict, *args: str, timeout: float = 120):
    """Run one `atelier` command to completion in a child process.

    :param work: working directory for the command.
    :param env: child environment.
    :param args: CLI arguments.
    :param timeout: seconds before the child is killed.
    :returns: the CompletedProcess.
    """
    return subprocess.run(
        [sys.executable, "-c", _CLI, *args],
        cwd=work,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
    )


def _install_waiter(work: Path, ready: Path, release: Path) -> None:
    """Install the held `waiter` conduit, with its sentinels in bash's namespace.

    :param work: project directory.
    :param ready: sentinel the task touches once it is inside its wait loop.
    :param release: sentinel the test creates to let the task finish.
    """
    _install(
        work,
        "waiter",
        WAITER_CONDUIT.format(ready=to_bash_path(ready), release=to_bash_path(release)),
    )


def _await_file(path: Path, timeout: float = 60) -> None:
    """Block until ``path`` exists, or fail the test.

    :param path: file the other process is expected to create.
    :param timeout: seconds to allow.
    """
    deadline = real_time.monotonic() + timeout
    while real_time.monotonic() < deadline:
        if path.exists():
            return
        real_time.sleep(0.05)
    raise AssertionError(f"timed out waiting for {path}")


def _start_waiter(work: Path, env: dict, ready: Path):
    """Start the held `waiter` run in a background process and wait for it to arm.

    :param work: project directory.
    :param env: child environment.
    :param ready: sentinel the task touches once it is inside its wait loop.
    :returns: the running Popen.
    """
    proc = subprocess.Popen(
        [sys.executable, "-c", _CLI, "run", "waiter"],
        cwd=work,
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        _await_file(ready)
    except AssertionError:
        proc.kill()
        raise
    return proc


def test_wait_joins_a_real_run_and_feeds_its_results_to_the_next_command(real_project):
    """The documented timeout, release and read-back workflow, across processes."""
    work, env = real_project
    ready, release = work / "ready", work / "release"
    _install_waiter(work, ready, release)

    runner = _start_waiter(work, env, ready)
    try:
        listed = _atelier(work, env, "list", "flows", "--json")
        assert listed.returncode == 0, listed.stderr
        flow_id = json.loads(listed.stdout)[0]["flow_id"]

        timed_out = _atelier(work, env, "wait", flow_id, "--timeout", "1")
        assert timed_out.returncode == 124, timed_out.stderr
        assert timed_out.stdout == ""
        # Giving up watching must not have disturbed the run itself.
        assert runner.poll() is None

        release.touch()
        joined = _atelier(work, env, "wait", flow_id, "--timeout", "60")
        assert joined.returncode == 0, joined.stderr
        assert joined.stdout == f"{flow_id}\n"
        assert runner.wait(timeout=60) == 0

        # The exact expression the README documents, run by a real shell.
        chained = run_expression(
            'flow_id=$(atelier wait latest --timeout 60) && '
            'atelier outputs "$flow_id" --json',
            cwd=work, env=env, timeout=120,
        )
        assert chained.returncode == 0, chained.stderr
        assert json.loads(chained.stdout) == {"work": "released\n", "never": None}
    finally:
        release.touch()
        if runner.poll() is None:
            runner.kill()
            runner.wait(timeout=30)


def test_wait_keeps_a_failed_run_out_of_the_success_branch(real_project):
    """`wait && next` never runs the next command when the flow failed."""
    work, env = real_project
    _install(work, "boomer", BOOM_CONDUIT)
    assert _atelier(work, env, "run", "boomer").returncode == 1

    chained = run_expression(
        "flow_id=$(atelier wait latest --timeout 30) && touch followed-up",
        cwd=work, env=env, timeout=120,
    )

    assert chained.returncode == 1
    assert "failed" in chained.stderr
    assert not (work / "followed-up").exists()


@pytest.mark.skipif(
    os.name == "nt",
    reason="POSIX signals: Popen.send_signal rejects SIGINT and 130 is a shell convention",
)
def test_ctrl_c_stops_the_observer_and_leaves_the_run_alone(real_project):
    """Interrupting a real `atelier wait` exits 130; the runner keeps going."""
    work, env = real_project
    ready, release = work / "ready", work / "release"
    _install_waiter(work, ready, release)

    runner = _start_waiter(work, env, ready)
    try:
        observer = subprocess.Popen(
            [sys.executable, "-c", _CLI, "wait", "latest", "--timeout", "120"],
            cwd=work, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        # The banner is printed before the first sleep, so its arrival proves
        # the observer is inside the loop and safe to interrupt.
        while "waiting for" not in observer.stderr.readline():
            assert observer.poll() is None, "observer exited before it began waiting"
        observer.send_signal(signal.SIGINT)
        assert observer.wait(timeout=60) == 130
        assert observer.stdout.read() == ""
        assert runner.poll() is None

        release.touch()
        assert runner.wait(timeout=60) == 0
    finally:
        release.touch()
        if runner.poll() is None:
            runner.kill()
            runner.wait(timeout=30)
