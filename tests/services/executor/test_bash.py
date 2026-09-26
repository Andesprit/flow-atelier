"""BashExecutor tests."""
import os
import sys
import time

import pytest

import flow_atelier.services.executor.bash as bash_mod
from flow_atelier.schemas.conduit import TaskDefinition, ToolType
from flow_atelier.services.executor.base import FlowContext
from flow_atelier.services.executor.bash import BashExecutor, to_bash_path


@pytest.mark.parametrize(
    ("ns", "src", "expected"),
    [
        # POSIX host: every path passes through untouched.
        ("posix", "/mnt/d/autonomous-projects", "/mnt/d/autonomous-projects"),
        ("posix", r"D:\foo\bar", r"D:\foo\bar"),
        # WSL bash: Windows drive path -> /mnt/<drive>/...; backslashes gone.
        ("wsl", r"D:\autonomous-projects\.atelier\conduits\ap",
         "/mnt/d/autonomous-projects/.atelier/conduits/ap"),
        ("wsl", "C:/foo/bar", "/mnt/c/foo/bar"),
        ("wsl", r"D:\\", "/mnt/d"),
        # git-bash/MSYS: Windows drive path -> /<drive>/...
        ("msys", r"D:\foo\bar", "/d/foo/bar"),
        # Idempotency guard: an already-POSIX value is never re-translated,
        # even when the resolved bash is WSL.
        ("wsl", "/mnt/d/autonomous-projects", "/mnt/d/autonomous-projects"),
        ("msys", "/d/foo", "/d/foo"),
    ],
)
def test_to_bash_path(monkeypatch, ns: str, src: str, expected: str) -> None:
    """to_bash_path translates only real Windows drive paths, per bash namespace."""
    monkeypatch.setattr(bash_mod, "_bash_namespace", lambda: ns)
    assert to_bash_path(src) == expected


def _task(cmd: str) -> TaskDefinition:
    """Build a bash TaskDefinition stub for tests.

    :param cmd: shell command string used as the task body.
    """
    return TaskDefinition(
        name="t",
        description="d",
        task=cmd,
        tool=ToolType.bash,
        depends_on=[],
    )


def _ctx(timeout: int = 30) -> FlowContext:
    """Build a FlowContext stub with a fake store.

    :param timeout: per-task timeout in seconds.
    """
    return FlowContext(
        flow_id="fake",
        store=None,  # type: ignore[arg-type]
        inputs={},
        timeout=timeout,
    )


async def test_echo_success():
    """Verify a simple echo command succeeds with stdout captured."""
    r = await BashExecutor().execute(_task("echo hello"), "echo hello", _ctx())
    assert r.exit_code == 0
    assert "hello" in r.output
    assert r.stderr == ""


async def test_failure_exit_code():
    """Verify non-zero exit codes propagate and success is False."""
    r = await BashExecutor().execute(_task("exit 5"), "exit 5", _ctx())
    assert r.exit_code == 5
    assert not r.success


@pytest.mark.skipif(sys.platform == "win32", reason="bash ; syntax not supported in cmd.exe")
async def test_stderr_captured():
    """Verify stderr output is captured separately from stdout."""
    r = await BashExecutor().execute(
        _task("echo boom 1>&2; exit 1"),
        "echo boom 1>&2; exit 1",
        _ctx(),
    )
    assert r.exit_code == 1
    assert "boom" in r.stderr


async def test_bashism_pipefail_supported():
    """`set -o pipefail` must work: the executor runs bash, not /bin/sh."""
    cmd = "set -euo pipefail; echo ok"
    r = await BashExecutor().execute(_task(cmd), cmd, _ctx())
    assert r.exit_code == 0
    assert "ok" in r.output


async def test_timeout_kills_process():
    """Verify a long-running command is killed when the timeout expires."""
    r = await BashExecutor().execute(
        _task("sleep 5"),
        "sleep 5",
        _ctx(timeout=1),
    )
    assert r.exit_code == 124
    assert "timeout" in r.stderr


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX process groups only")
async def test_timeout_kills_background_grandchild(tmp_path):
    """A backgrounded grandchild must not survive the task timeout.

    The shell backgrounds a long sleep and records its pid; on timeout the
    executor kills the whole process group, so the grandchild dies too.
    """
    pidfile = tmp_path / "child.pid"
    cmd = f"sleep 30 & echo $! > {pidfile}; wait"
    r = await BashExecutor().execute(_task(cmd), cmd, _ctx(timeout=1))
    assert r.exit_code == 124

    # The pid file is written before `wait`, so it exists by the time we return.
    grandchild_pid = int(pidfile.read_text().strip())
    # Give the group kill a moment to land, then assert the grandchild is gone.
    for _ in range(20):
        try:
            os.kill(grandchild_pid, 0)
        except ProcessLookupError:
            break
        time.sleep(0.1)
    else:
        pytest.fail(f"grandchild pid {grandchild_pid} survived the timeout")


@pytest.mark.skipif(sys.platform == "win32", reason="bash ; syntax not supported in cmd.exe")
async def test_timeout_preserves_partial_output():
    """Verify output printed before the timeout survives the kill."""
    cmd = "echo hello; sleep 5"
    r = await BashExecutor().execute(_task(cmd), cmd, _ctx(timeout=1))
    assert r.exit_code == 124
    assert "hello" in r.stdout
    assert "hello" in r.output
    assert "timeout" in r.stderr


def _recording_ctx(steps: list) -> FlowContext:
    """Build a FlowContext whose step hook records each step and when it came.

    :param steps: list receiving ``(monotonic_time, step)`` tuples.
    """
    ctx = _ctx()

    async def _on_step(step) -> None:
        steps.append((time.monotonic(), step))

    ctx.on_step = _on_step
    return ctx


@pytest.mark.skipif(sys.platform == "win32", reason="bash ; syntax not supported in cmd.exe")
async def test_output_is_recorded_as_it_is_printed():
    """Each line reaches the step hook while the command still runs."""
    steps: list = []
    cmd = "echo first; sleep 0.5; printf 'second\\npartial'; echo oops >&2"
    r = await BashExecutor().execute(_task(cmd), cmd, _recording_ctx(steps))
    done = time.monotonic()

    out = [line for _, s in steps if s.kind == "stdout" for line in s.text.splitlines()]
    err = [line for _, s in steps if s.kind == "stderr" for line in s.text.splitlines()]
    assert out == ["first", "second", "partial"]
    assert err == ["oops"]
    first_at = next(at for at, s in steps if s.text.startswith("first"))
    assert done - first_at >= 0.4
    assert r.stdout == "first\nsecond\npartial"


async def test_a_failing_output_hook_does_not_fail_the_command():
    """Recording is best-effort: the command's own result is what counts."""
    ctx = _ctx()

    async def _broken(step) -> None:
        raise OSError("disk full")

    ctx.on_step = _broken
    r = await BashExecutor().execute(_task("echo hello"), "echo hello", ctx)
    assert r.exit_code == 0
    assert r.stdout == "hello\n"


async def test_recorded_output_stops_at_the_cap(monkeypatch):
    """A noisy command cannot grow steps.jsonl without bound; its result keeps everything."""
    monkeypatch.setattr(bash_mod, "LIVE_OUTPUT_BYTES", 20)
    steps: list = []
    cmd = "for i in $(seq 1 50); do echo line-$i; done"
    r = await BashExecutor().execute(_task(cmd), cmd, _recording_ctx(steps))

    recorded = "".join(s.text for _, s in steps if s.kind == "stdout")
    assert 0 < len(recorded.encode()) <= 20
    assert r.stdout.count("line-") == 50
