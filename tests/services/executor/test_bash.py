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


def _value_ctx(tmp_path, **values: str) -> FlowContext:
    """Build a FlowContext that runs in ``tmp_path`` with step values.

    :param tmp_path: pytest temp directory, used as the working directory.
    :param values: variable name -> value, as the engine would pass them.
    """
    ctx = _ctx()
    ctx.working_dir = tmp_path
    ctx.shell_env = dict(values)
    return ctx


async def test_a_value_read_from_its_variable_is_never_run(tmp_path):
    """The injection that `{{inputs.name}}` allows does nothing as "$INPUT_NAME"."""
    cmd = 'echo "hello $INPUT_NAME"'
    ctx = _value_ctx(tmp_path, INPUT_NAME="$(touch pwned)`touch pwned2`; touch pwned3")
    r = await BashExecutor().execute(_task(cmd), cmd, ctx)
    assert r.exit_code == 0, r.stderr
    assert r.stdout == "hello $(touch pwned)`touch pwned2`; touch pwned3\n"
    assert list(tmp_path.iterdir()) == []


async def test_every_value_is_also_in_a_file_removed_after_the_step(tmp_path):
    """<NAME>_FILE holds the exact value, newlines included, for the step only."""
    cmd = 'cat "$OUTPUT_BUILD_FILE"; printf "%s" "$OUTPUT_BUILD_FILE" > path.txt'
    ctx = _value_ctx(tmp_path, OUTPUT_BUILD="line one\nline two\n")
    r = await BashExecutor().execute(_task(cmd), cmd, ctx)
    assert r.exit_code == 0, r.stderr
    assert r.stdout == "line one\nline two\n"
    path = (tmp_path / "path.txt").read_text()
    assert path
    assert not os.path.exists(path)


async def test_a_value_over_the_limit_arrives_only_as_a_file(tmp_path):
    """150,000 bytes is too big for one variable, so only <NAME>_FILE is set."""
    big = "x" * 150_000
    cmd = 'echo "plain:${INPUT_BIG+set}"; wc -c < "$INPUT_BIG_FILE"; echo "$INPUT_SMALL"'
    ctx = _value_ctx(tmp_path, INPUT_BIG=big, INPUT_SMALL="ok")
    r = await BashExecutor().execute(_task(cmd), cmd, ctx)
    assert r.exit_code == 0, r.stderr
    assert r.stdout.split() == ["plain:", "150000", "ok"]


async def test_values_past_the_total_budget_arrive_only_as_files(tmp_path, monkeypatch):
    """Once the step's plain values reach the total cap, later names get files only."""
    monkeypatch.setattr(bash_mod, "ENV_BYTES", 5)
    cmd = 'echo "${INPUT_A-unset} ${INPUT_B-unset}"; cat "$INPUT_B_FILE"'
    ctx = _value_ctx(tmp_path, INPUT_A="abc", INPUT_B="def")
    r = await BashExecutor().execute(_task(cmd), cmd, ctx)
    assert r.exit_code == 0, r.stderr
    assert r.stdout == "abc unset\ndef"


async def test_the_names_a_script_reads_get_the_budget_first(tmp_path, monkeypatch):
    """A big input sorted first cannot push out the output the step reads."""
    monkeypatch.setattr(bash_mod, "ENV_BYTES", 3)
    cmd = 'echo "$OUTPUT_REVIEW"; env | grep -c "^INPUT_DO[C]="'
    ctx = _value_ctx(tmp_path, INPUT_DOC="abc", OUTPUT_REVIEW="ok")
    r = await BashExecutor().execute(_task(cmd), cmd, ctx)
    assert r.stdout == "ok\n0\n"


@pytest.mark.skipif(sys.platform == "win32", reason="Windows caps a command line at 32 KB")
async def test_plain_values_leave_programs_their_argument_room(tmp_path):
    """Six 95 KB values passed as arguments still fit under macOS's 1 MB limit.

    Only one of them also sits in the environment, which every program the
    step starts inherits; the others come through their files.
    """
    values = {f"OUTPUT_PART{i}": "a" * 95_000 for i in range(6)}
    args = " ".join(f'"$(cat "$OUTPUT_PART{i}_FILE")"' for i in range(6))
    cmd = f'/bin/echo {args} > /dev/null && env | grep -c "^OUTPUT_PART[0-9]="'
    r = await BashExecutor().execute(_task(cmd), cmd, _value_ctx(tmp_path, **values))
    assert (r.exit_code, r.stderr) == (0, "")
    assert r.stdout == "1\n"


async def test_a_lone_surrogate_does_not_break_the_step(tmp_path):
    """Half an emoji from JSON has no UTF-8 bytes; a step can still run."""
    cmd = 'echo "hi ${INPUT_NOTE-unset}"; cat "$INPUT_NOTE_FILE"'
    ctx = _value_ctx(tmp_path, INPUT_NOTE="half \ud83d")
    r = await BashExecutor().execute(_task(cmd), cmd, ctx)
    assert r.exit_code == 0, r.stderr
    assert r.stdout == "hi unset\nhalf ?"


async def test_a_value_with_a_nul_byte_does_not_break_the_step(tmp_path):
    """No variable can hold NUL, so that value is only in its file."""
    cmd = 'echo "${INPUT_RAW-unset}"; wc -c < "$INPUT_RAW_FILE"'
    ctx = _value_ctx(tmp_path, INPUT_RAW="a\0b")
    r = await BashExecutor().execute(_task(cmd), cmd, ctx)
    assert r.exit_code == 0, r.stderr
    assert r.stdout.split() == ["unset", "3"]


async def test_a_script_far_over_the_argument_limit_runs(tmp_path):
    """A 1.5 MB script used to fail as one `bash -c` argument; a file has no limit."""
    cmd = f"x='{'a' * 1_500_000}'\necho ${{#x}}"
    r = await BashExecutor().execute(_task(cmd), cmd, _value_ctx(tmp_path))
    assert r.exit_code == 0, r.stderr
    assert r.stdout == "1500000\n"


async def test_a_small_script_still_runs_as_bash_c(tmp_path):
    """$0 and $- stay what they were, so "$0" -c still starts bash."""
    cmd = "case $- in *c*) echo inline;; esac\n\"$0\" -c 'echo nested ok'"
    r = await BashExecutor().execute(_task(cmd), cmd, _value_ctx(tmp_path))
    assert (r.exit_code, r.stderr) == (0, "")
    assert r.stdout == "inline\nnested ok\n"


@pytest.mark.parametrize("inline_bytes", [bash_mod.INLINE_BYTES, 0])
async def test_bash_error_lines_read_the_same_on_every_run(
    tmp_path, monkeypatch, inline_bytes
):
    """No per-run script path in bash's own errors, inline or from a file."""
    monkeypatch.setattr(bash_mod, "INLINE_BYTES", inline_bytes)
    cmd = "if ( no_such_cmd_q ) 2>&1; then :; fi\nno_such_cmd_q"
    first = await BashExecutor().execute(_task(cmd), cmd, _value_ctx(tmp_path))
    second = await BashExecutor().execute(_task(cmd), cmd, _value_ctx(tmp_path))
    assert "atelier-step-" not in first.stdout + first.stderr
    assert "no_such_cmd_q" in first.stderr
    assert (first.stdout, first.stderr) == (second.stdout, second.stderr)


async def test_a_script_file_never_takes_the_task_name(tmp_path, monkeypatch):
    """A task named after a Windows device (nul, con) gets a plain script name."""
    monkeypatch.setattr(bash_mod, "INLINE_BYTES", 0)
    task = _task('basename "$0"')
    task.name = "nul"
    r = await BashExecutor().execute(task, task.task, _value_ctx(tmp_path))
    assert (r.exit_code, r.stdout) == (0, "step.sh\n")


async def test_a_file_only_value_hides_an_inherited_variable_of_its_name(
    tmp_path, monkeypatch
):
    """A parent run's INPUT_BIG never stands in for a value too big to set."""
    monkeypatch.setenv("INPUT_BIG", "stale-parent")
    monkeypatch.setenv("OUTPUT_DIR", "/my/build")
    cmd = 'echo "plain:${INPUT_BIG-unset} dir:$OUTPUT_DIR"; wc -c < "$INPUT_BIG_FILE"'
    ctx = _value_ctx(tmp_path, INPUT_BIG="x" * 150_000)
    r = await BashExecutor().execute(_task(cmd), cmd, ctx)
    assert r.exit_code == 0, r.stderr
    assert r.stdout.split() == ["plain:unset", "dir:/my/build", "150000"]


async def test_a_named_variable_left_unset_is_reported(tmp_path, monkeypatch):
    """A script that names a value with only a file form hears why it is empty."""
    monkeypatch.setattr(bash_mod, "ENV_BYTES", 5)
    seen = []

    async def on_step(step):
        seen.append(step.text)

    cmd = 'echo "[$OUTPUT_B]"; cat "$OUTPUT_A_FILE"'
    ctx = _value_ctx(tmp_path, OUTPUT_A="never named", OUTPUT_B="toolong")
    ctx.on_step = on_step
    r = await BashExecutor().execute(_task(cmd), cmd, ctx)
    note = (
        'atelier: OUTPUT_B is not set: the step\'s values pass the 5-byte '
        'variable budget; read "$OUTPUT_B_FILE"'
    )
    assert (r.exit_code, r.stdout, r.stderr) == (0, "[]\nnever named", note)
    assert seen[0] == note  # live, before the script's own lines


async def test_the_api_token_never_reaches_a_shell_step(monkeypatch):
    """A shell step, or an agent it starts, must not get the server's token."""
    monkeypatch.setenv("ATELIER_API_TOKEN", "secret")
    monkeypatch.setenv("SOME_KEY", "kept")
    cmd = 'echo "token=[${ATELIER_API_TOKEN:-}] key=[$SOME_KEY]"'
    r = await BashExecutor().execute(_task(cmd), cmd, _ctx())
    assert r.exit_code == 0, r.stderr
    assert "token=[] key=[kept]" in r.output
