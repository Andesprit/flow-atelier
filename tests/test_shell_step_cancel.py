"""A shell step the run gives up on stops, with every process it started.

Each test starts a ``tool:bash`` step that records its shell's pid (which is
also its process group id) and then works for a while. The run is then ended
early: fail-fast from a failing sibling, a failing sibling of the parent
conduit, Ctrl-C on ``atelier run``, or ``atelier stop``. The step's whole
process group must be gone soon after, so the work it would have finished
never happens.
"""
import asyncio
import os
import signal
import subprocess
import sys
import time

import pytest

from flow_atelier.core.atelier import Atelier
from tests._shell import CLI, INTERRUPTIBLE_CLI, group_alive

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX process groups only")


def _write_conduit(base, name, body):
    """Write a conduit YAML under `base/.atelier/conduits/<name>/conduit.yaml`.

    :param base: base working directory path.
    :param name: conduit name.
    :param body: YAML body to write.
    """
    d = base / ".atelier" / "conduits" / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "conduit.yaml").write_text(body)


def _slow_step(pidfile, marker, sleep: float = 2) -> str:
    """Shell command that records its group, sleeps, then leaves a marker.

    :param pidfile: file receiving the shell's pid.
    :param marker: file created only if the step is never stopped.
    :param sleep: seconds of work before the marker.
    """
    return f"echo $$ > {pidfile}; sleep {sleep}; touch {marker}"


def _fail_once_started(pidfile) -> str:
    """Shell command that fails as soon as the slow step has started.

    :param pidfile: file the slow step writes when it is running.
    """
    return f"while [ ! -s {pidfile} ]; do sleep 0.05; done; exit 1"


def _assert_group_gone(pidfile, within: float = 1.0) -> None:
    """Fail unless the group recorded in ``pidfile`` dies within ``within`` seconds.

    :param pidfile: file holding the slow step's shell pid.
    :param within: seconds to allow; shorter than the step's own sleep.
    """
    pgid = int(pidfile.read_text().strip())
    deadline = time.monotonic() + within
    while group_alive(pgid):
        if time.monotonic() > deadline:
            os.killpg(pgid, signal.SIGKILL)
            pytest.fail(f"process group {pgid} of the cancelled step is still running")
        time.sleep(0.05)


@pytest.fixture
def workdir(tmp_path, monkeypatch):
    """Provide an isolated working directory for in-process runs.

    :param tmp_path: pytest temp directory fixture.
    :param monkeypatch: pytest monkeypatch fixture.
    """
    monkeypatch.chdir(tmp_path)
    return tmp_path


async def test_fail_fast_stops_the_running_sibling(workdir):
    """The reported reproduction: the sibling cancelled by fail-fast really stops."""
    pidfile, marker = workdir / "slow.pid", workdir / "marker"
    _write_conduit(
        workdir,
        "fast",
        f"""
name: fast
description: one step fails while the other still works
tasks:
  - bad:
      description: fails
      task: "{_fail_once_started(pidfile)}"
      tool: tool:bash
      depends_on: []
  - slow:
      description: works for a while
      task: "{_slow_step(pidfile, marker)}"
      tool: tool:bash
      depends_on: []
""",
    )
    a = Atelier()
    with pytest.raises(RuntimeError):
        await a.run_conduit("fast", {})
    p = a.get_status(a.list_flows("fast")[0])
    assert p.tasks["slow"].status.value == "cancelled"

    _assert_group_gone(pidfile)
    await asyncio.sleep(2)
    assert not marker.exists()


async def test_cancelled_parent_stops_the_nested_conduits_shell_step(workdir):
    """Fail-fast in the parent stops the shell step running inside a tool:conduit."""
    pidfile, marker = workdir / "slow.pid", workdir / "marker"
    _write_conduit(
        workdir,
        "child",
        f"""
name: child
description: works for a while
tasks:
  - slow:
      description: works for a while
      task: "{_slow_step(pidfile, marker)}"
      tool: tool:bash
      depends_on: []
""",
    )
    _write_conduit(
        workdir,
        "parent",
        f"""
name: parent
description: one step fails while the nested conduit still works
tasks:
  - bad:
      description: fails
      task: "{_fail_once_started(pidfile)}"
      tool: tool:bash
      depends_on: []
  - nested:
      description: run child
      task: child
      tool: tool:conduit
      depends_on: []
""",
    )
    a = Atelier()
    with pytest.raises(RuntimeError):
        await a.run_conduit("parent", {})

    _assert_group_gone(pidfile)
    await asyncio.sleep(2)
    assert not marker.exists()


@pytest.mark.parametrize("how", ["ctrl-c", "atelier stop"])
def test_interrupted_run_stops_its_shell_step(tmp_path, how):
    """Ctrl-C on `atelier run` and `atelier stop` both end the step's processes.

    The shell runs in its own session, so a terminal's Ctrl-C never reaches
    it directly: only the runner can stop it.
    """
    work = tmp_path / "project"
    (work / ".atelier" / "conduits").mkdir(parents=True)
    (tmp_path / "global" / "conduits").mkdir(parents=True)
    env = {k: v for k, v in os.environ.items() if not k.startswith("ATELIER_")}
    env["ATELIER_GLOBAL_ATELIER_DIR"] = str(tmp_path / "global")
    env["ATELIER_NO_UPDATE_CHECK"] = "1"
    pidfile, marker = work / "slow.pid", work / "marker"
    _write_conduit(
        work,
        "slow",
        f"""
name: slow
description: works for a long while
tasks:
  - slow:
      description: works for a long while
      task: "{_slow_step(pidfile, marker, sleep=30)}"
      tool: tool:bash
      depends_on: []
""",
    )
    runner = subprocess.Popen(
        [sys.executable, "-c", INTERRUPTIBLE_CLI, "run", "slow"],
        cwd=work,
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        deadline = time.monotonic() + 60
        while not (pidfile.exists() and pidfile.read_text().strip()):
            assert runner.poll() is None, "the run ended before its step started"
            assert time.monotonic() < deadline, "the step never started"
            time.sleep(0.05)

        if how == "ctrl-c":
            runner.send_signal(signal.SIGINT)
        else:
            stop = subprocess.run(
                [sys.executable, "-c", CLI, "stop", "latest"],
                cwd=work,
                env=env,
                capture_output=True,
                text=True,
                timeout=60,
            )
            assert stop.returncode == 0, stop.stdout + stop.stderr
        # A stopped run is cut short, not a success: 143 is 128 + SIGTERM.
        assert runner.wait(timeout=20) == (130 if how == "ctrl-c" else 143)
    finally:
        if runner.poll() is None:
            runner.kill()
            runner.wait(timeout=30)
            if pidfile.exists() and pidfile.read_text().strip():
                pgid = int(pidfile.read_text().strip())
                if group_alive(pgid):
                    os.killpg(pgid, signal.SIGKILL)

    _assert_group_gone(pidfile, within=5)
    assert not marker.exists()
