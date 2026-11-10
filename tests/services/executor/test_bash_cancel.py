"""BashExecutor tests for a step that is cancelled or killed.

A cancelled step (fail-fast, ``atelier stop``, Ctrl-C, a cancelled parent
conduit) must stop with every process it started. One that exits on its own
keeps what it started in the background.
"""
import asyncio
import os
import signal
import subprocess
import sys

import pytest

import flow_atelier.services.executor.bash as bash_mod
from flow_atelier.services.executor.base import FlowContext
from flow_atelier.services.executor.bash import BashExecutor
from tests._shell import group_alive
from tests.services.executor.test_bash import _ctx, _recording_ctx, _task


async def _assert_group_dies(pgid: int) -> None:
    """Fail unless group ``pgid`` is gone within a second.

    :param pgid: process group id of the stopped shell.
    """
    for _ in range(10):
        if not group_alive(pgid):
            return
        await asyncio.sleep(0.1)
    pytest.fail(f"process group {pgid} is still running")


def _see_the_shell_exit_first(monkeypatch) -> asyncio.Event:
    """Hand the executor its shell only once the shell's exit was seen.

    ``proc.wait()`` then returns at once, even while a child the shell left
    behind still holds its output, so the step waits in the final drain of
    that output instead. A busy event loop gives the same order by chance.

    :param monkeypatch: pytest monkeypatch fixture.
    :returns: an event set when the executor gets the exited shell.
    """
    real_spawn = asyncio.create_subprocess_exec
    exited = asyncio.Event()

    async def _spawn(*args, **kwargs):
        proc = await real_spawn(*args, **kwargs)
        while proc.returncode is None:
            await asyncio.sleep(0.01)
        exited.set()
        return proc

    monkeypatch.setattr(bash_mod.asyncio, "create_subprocess_exec", _spawn)
    return exited


async def _cancel_once_started(cmd: str, pidfile, ctx: FlowContext | None = None) -> int:
    """Start ``cmd``, cancel it once it wrote ``pidfile``, and return the shell pid.

    :param cmd: shell command that writes its pid to ``pidfile`` first.
    :param pidfile: file the command writes ``$$`` to.
    :param ctx: optional context; a plain one by default.
    :returns: the pid of the cancelled shell, which leads its own group.
    """
    run = asyncio.create_task(BashExecutor().execute(_task(cmd), cmd, ctx or _ctx()))
    for _ in range(100):
        if pidfile.exists() and pidfile.read_text().strip():
            break
        await asyncio.sleep(0.05)
    else:
        pytest.fail("the command never started")
    run.cancel()
    with pytest.raises(asyncio.CancelledError):
        await run
    return int(pidfile.read_text().strip())


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX process groups only")
@pytest.mark.parametrize(
    "body",
    [
        # The shell itself is still running when it is cancelled.
        "sleep 2; touch {marker}",
        # The shell already exited; only a background child holds its output.
        "(sleep 2; touch {marker}) & exit 0",
    ],
    ids=["shell-running", "shell-exited"],
)
async def test_cancel_kills_the_step_and_everything_it_started(tmp_path, body):
    """A cancelled step dies with its whole group, so its marker never appears."""
    pidfile, marker = tmp_path / "shell.pid", tmp_path / "marker"
    cmd = f"echo $$ > {pidfile}; " + body.format(marker=marker)
    pgid = await _cancel_once_started(cmd, pidfile)

    # Gone well before the 2s sleep could end on its own.
    await _assert_group_dies(pgid)
    await asyncio.sleep(2)
    assert not marker.exists()


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX process groups only")
async def test_cancel_during_the_output_drain_kills_the_background_child(tmp_path, monkeypatch):
    """The shell exited but its child still holds the output: a cancel kills the child."""
    exited = _see_the_shell_exit_first(monkeypatch)
    pidfile, marker = tmp_path / "shell.pid", tmp_path / "marker"
    cmd = f"echo $$ > {pidfile}; (sleep 2; touch {marker}) & exit 0"
    run = asyncio.create_task(BashExecutor().execute(_task(cmd), cmd, _ctx()))
    await asyncio.wait_for(exited.wait(), timeout=10)
    await asyncio.sleep(0.1)
    run.cancel()
    with pytest.raises(asyncio.CancelledError):
        await run

    await _assert_group_dies(int(pidfile.read_text().strip()))
    await asyncio.sleep(2)
    assert not marker.exists()


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX process groups only")
async def test_timeout_covers_the_output_drain(tmp_path, monkeypatch):
    """A child that holds the output of an exited shell still dies at the timeout."""
    _see_the_shell_exit_first(monkeypatch)
    pidfile, marker = tmp_path / "shell.pid", tmp_path / "marker"
    cmd = f"echo $$ > {pidfile}; (sleep 2; touch {marker}) & exit 0"
    r = await BashExecutor().execute(_task(cmd), cmd, _ctx(timeout=1))

    assert r.exit_code == 124
    await _assert_group_dies(int(pidfile.read_text().strip()))
    await asyncio.sleep(1.5)
    assert not marker.exists()


@pytest.mark.skipif(sys.platform == "win32", reason="bash ; syntax not supported in cmd.exe")
async def test_cancel_still_records_the_last_output(tmp_path):
    """Output printed before the cancel reaches the step hook, unfinished line included."""
    steps: list = []
    pidfile = tmp_path / "shell.pid"
    cmd = f"printf 'first\\npartial'; echo $$ > {pidfile}; sleep 30"
    await _cancel_once_started(cmd, pidfile, _recording_ctx(steps))

    recorded = [s.text for _, s in steps if s.kind == "stdout"]
    assert recorded == ["first", "partial"]


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX process groups only")
async def test_normal_exit_leaves_a_background_server_running(tmp_path):
    """A step may start a server on purpose; finishing the step must not kill it."""
    pidfile = tmp_path / "server.pid"
    cmd = f"sleep 30 > /dev/null 2>&1 & echo $! > {pidfile}"
    r = await BashExecutor().execute(_task(cmd), cmd, _ctx())
    server = int(pidfile.read_text().strip())
    try:
        assert r.exit_code == 0
        os.kill(server, 0)
    finally:
        os.kill(server, signal.SIGKILL)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX process groups only")
async def test_a_background_child_holding_the_output_is_named_at_the_timeout(tmp_path):
    """The script is done, but an unredirected `&` child keeps the step waiting."""
    pidfile = tmp_path / "shell.pid"
    cmd = f"echo $$ > {pidfile}; sleep 30 &"
    r = await BashExecutor().execute(_task(cmd), cmd, _ctx(timeout=1))
    await _assert_group_dies(int(pidfile.read_text().strip()))
    assert r.exit_code == 124
    assert r.stderr == (
        "timeout after 1s: the script finished (exit 0) but a process it "
        "started in the background still holds its output; redirect that "
        "process's output, e.g. `cmd > log 2>&1 &`"
    )


class _FakeProc:
    """Stand-in for the shell process, recording a direct kill."""

    pid = 4242

    def __init__(self) -> None:
        """Start with no kill recorded."""
        self.killed = False

    def kill(self) -> None:
        """Record that only the shell itself was killed."""
        self.killed = True


@pytest.mark.parametrize(
    ("taskkill", "shell_killed"),
    [
        # taskkill took the whole tree: nothing left to do.
        (lambda argv: subprocess.CompletedProcess(argv, 0), False),
        # taskkill failed: at least the shell itself must go.
        (lambda argv: subprocess.CompletedProcess(argv, 128), True),
        (lambda argv: (_ for _ in ()).throw(FileNotFoundError("taskkill")), True),
        (lambda argv: (_ for _ in ()).throw(subprocess.TimeoutExpired(argv, 10)), True),
    ],
    ids=["tree-killed", "taskkill-failed", "no-taskkill", "taskkill-hangs"],
)
def test_windows_kill_takes_the_whole_process_tree(monkeypatch, taskkill, shell_killed):
    """Windows has no process groups, so the shell's tree is killed with taskkill."""
    calls: list = []

    def _run(argv, **kwargs):
        calls.append(argv)
        return taskkill(argv)

    def _no_killpg(*args):
        raise AssertionError("os.killpg must not be used on Windows")

    proc = _FakeProc()
    with monkeypatch.context() as m:
        m.setattr(bash_mod.os, "name", "nt")
        m.setattr(bash_mod.os, "killpg", _no_killpg, raising=False)
        m.setattr(bash_mod.subprocess, "run", _run)
        BashExecutor._kill_process_group(proc)

    assert calls == [["taskkill", "/PID", "4242", "/T", "/F"]]
    assert proc.killed is shell_killed


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX process groups only")
async def test_a_cancel_while_the_pipes_connect_stops_the_group(tmp_path, monkeypatch):
    """The shell runs before asyncio hands the process over: its work dies too.

    :param tmp_path: pytest temp directory fixture.
    :param monkeypatch: pytest monkeypatch fixture.
    """
    import asyncio.base_subprocess as base_subprocess

    connect = base_subprocess.BaseSubprocessTransport._connect_pipes

    async def late(self, waiter):
        # Timing only: a busy loop reaches the pipes late.
        await asyncio.sleep(0.5)
        return await connect(self, waiter)

    monkeypatch.setattr(base_subprocess.BaseSubprocessTransport, "_connect_pipes", late)
    pidfile, marker = tmp_path / "shell.pid", tmp_path / "marker"
    cmd = f"echo $$ > {pidfile}; sleep 3; touch {marker}"
    run = asyncio.create_task(BashExecutor().execute(_task(cmd), cmd, _ctx()))
    for _ in range(100):
        if pidfile.exists() and pidfile.read_text().strip():
            break
        await asyncio.sleep(0.01)
    else:
        pytest.fail("the command never started")
    run.cancel()
    with pytest.raises(asyncio.CancelledError):
        await run
    pgid = int(pidfile.read_text().strip())
    try:
        await _assert_group_dies(pgid)
    finally:
        if group_alive(pgid):
            os.killpg(pgid, signal.SIGKILL)
    assert not marker.exists()
