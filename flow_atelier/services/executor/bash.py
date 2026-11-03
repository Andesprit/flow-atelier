"""tool:bash executor — runs a shell command via asyncio subprocess."""
from __future__ import annotations

import asyncio
import contextlib
import functools
import logging
import os
import re
import shutil
import signal
import subprocess
from pathlib import Path

from flow_atelier.schemas.conduit import TaskDefinition
from flow_atelier.schemas.log import ExecutionResult, IntermediateStep, StepKind
from flow_atelier.services.executor.base import ExecutorBase, FlowContext

logger = logging.getLogger(__name__)

# Bytes of each stream recorded live per attempt. This bounds steps.jsonl
# only: the result still carries every byte the command printed.
LIVE_OUTPUT_BYTES = 1_000_000

# Windows drive-rooted path, e.g. ``D:\foo`` or ``C:/bar``. Engine-computed
# path variables (``{{conduit_dir}}``) are the only values that arrive in this
# flavor; bash-computed ones (``pwd -P``) are already POSIX.
_WIN_DRIVE_RE = re.compile(r"^([A-Za-z]):[\\/]")


@functools.cache
def _bash_namespace() -> str:
    """Path namespace the resolved ``bash`` expects: ``posix`` | ``wsl`` | ``msys``.

    On non-Windows hosts ``bash`` is native, so host paths are already POSIX.
    On Windows, ``shutil.which("bash")`` may be WSL bash (wants
    ``/mnt/<drive>/...``) or git-bash/MSYS (wants ``/<drive>/...``). These need
    different translations, so probe the actual bash for ``wslpath`` rather than
    guessing from its path. Cached: the answer can't change within a run.
    """
    if os.name != "nt":
        return "posix"
    bash = shutil.which("bash")
    if bash is None:
        return "posix"
    try:
        probe = subprocess.run(
            [bash, "-c", "command -v wslpath >/dev/null 2>&1 && echo wsl || echo msys"],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return "msys"  # ponytail: assume git-bash if the probe can't run
    return "wsl" if probe.stdout.strip() == "wsl" else "msys"


def to_bash_path(path: Path | str) -> str:
    """Render a host path in the namespace the resolved ``bash`` expects.

    No-op on POSIX hosts and on any value that isn't a Windows drive path — the
    ``^[A-Za-z]:[\\/]`` guard keeps this idempotent (``wslpath -u`` is not:
    ``wslpath -u /mnt/d/x`` returns ``/mnt/d/mnt/d/x``). A Windows drive path
    becomes ``/mnt/<drive>/...`` under WSL bash or ``/<drive>/...`` under
    git-bash/MSYS, so it survives the backslash-eating shell intact.

    :param path: host path (or already-POSIX string) to translate.
    :returns: the path in the bash executor's namespace.
    """
    text = str(path)
    m = _WIN_DRIVE_RE.match(text)
    if m is None or _bash_namespace() == "posix":
        return text
    drive = m.group(1).lower()
    rest = text[2:].replace("\\", "/").lstrip("/")
    prefix = f"/mnt/{drive}" if _bash_namespace() == "wsl" else f"/{drive}"
    return f"{prefix}/{rest}" if rest else prefix


class _LiveOutput:
    """Hand the complete lines of one output stream to the step hook as they arrive.

    Lines are cut at ``\\n`` on bytes, so a character split across two reads
    is never decoded in halves. Recording is a live view only: it stops at
    :data:`LIVE_OUTPUT_BYTES` or on the first hook failure, and the command's
    result keeps every byte either way.
    """

    def __init__(self, on_step, kind: StepKind) -> None:
        """Bind the recorder to a step hook and the stream it reports.

        :param on_step: the context's step hook, or ``None`` to record nothing.
        :param kind: :attr:`StepKind.stdout` or :attr:`StepKind.stderr`.
        """
        self._on_step = on_step
        self._kind = kind
        self._pending = bytearray()
        self._sent = 0

    async def feed(self, chunk: bytes) -> None:
        """Record every line ``chunk`` completes; keep the unfinished tail.

        :param chunk: bytes just read from the stream.
        """
        if self._on_step is None:
            return
        self._pending.extend(chunk)
        cut = self._pending.rfind(b"\n")
        if cut == -1:
            # A stream with no newline in sight cannot finish a line within the
            # budget either; stop rather than hold it all in memory twice.
            if len(self._pending) > LIVE_OUTPUT_BYTES - self._sent:
                self._on_step = None
                self._pending.clear()
            return
        lines = bytes(self._pending[:cut])
        del self._pending[: cut + 1]
        await self._send(lines)

    async def close(self) -> None:
        """Record a last line the command printed without a newline."""
        if self._on_step is not None and self._pending:
            lines = bytes(self._pending)
            self._pending.clear()
            await self._send(lines)

    async def _send(self, data: bytes) -> None:
        """Pass ``data`` to the hook, trimmed to whole lines within the budget.

        :param data: one or more complete lines, without the final newline.
        """
        room = LIVE_OUTPUT_BYTES - self._sent
        full = len(data) > room
        if full:
            kept = data[:room]
            data = kept[: max(kept.rfind(b"\n"), 0)]
        on_step = self._on_step
        if full:
            self._on_step = None
        if not data.strip() or on_step is None:
            return
        self._sent += len(data)
        try:
            await on_step(
                IntermediateStep(kind=self._kind, text=data.decode("utf-8", errors="replace"))
            )
        except Exception:  # noqa: BLE001
            logger.debug("live output recording failed", exc_info=True)
            self._on_step = None


class BashExecutor(ExecutorBase):
    """Executes ``tool:bash`` tasks via ``asyncio.create_subprocess_shell``.

    Trust model: ``{{inputs.x}}`` values are interpolated into the command
    string unescaped, so inputs are as trusted as the conduit author. A party
    who can only *supply* inputs (a scheduled job, a WS ``run`` message, a HITL
    answer) can inject shell metacharacters into an author's command. Authors
    should quote interpolations they don't control (e.g. ``"{{inputs.x}}"``).
    """

    @staticmethod
    def _kill_process_group(proc: asyncio.subprocess.Process) -> None:
        """Kill ``proc`` and every process it started.

        Because the shell was started with ``start_new_session=True`` it leads
        its own group, whose id is its pid, so SIGKILL to that group also kills
        the build/server/etc. it spawned — closing the orphaned-grandchild gap a
        bare ``proc.kill()`` leaves. The id is not looked up with
        ``os.getpgid``: once the shell itself has exited and been reaped, that
        lookup fails while a background child it left is still in the group.
        Windows has no process groups, so ``taskkill /T`` kills the shell's
        process tree instead. Falls back to killing just the shell if the group
        or tree can't be killed (race/permission/no ``taskkill``).

        :param proc: the shell subprocess to terminate together with its group.
        """
        if proc.pid is None:
            return
        if os.name == "nt":
            try:
                done = subprocess.run(
                    ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                    capture_output=True,
                    timeout=10,
                )
                if done.returncode == 0:
                    return
            except (OSError, subprocess.SubprocessError):
                pass
        else:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
                return
            except (ProcessLookupError, PermissionError):
                pass
        try:
            proc.kill()
        except ProcessLookupError:
            pass

    async def _stop(
        self, proc: asyncio.subprocess.Process, pumps: list[asyncio.Task]
    ) -> None:
        """Kill ``proc`` with everything it started, then end its output pumps.

        The pumps get a short grace to read what is still in the pipes, so the
        last lines (and an unfinished one) reach the step hook and the buffers.
        No proc.wait() here: its waiter only wakes once all pipes close, and a
        grandchild that survived the kill (e.g. one that escaped into its own
        session) could hold them open. The loop's child watcher reaps the shell.

        :param proc: the shell subprocess to kill.
        :param pumps: the tasks reading its stdout and stderr.
        """
        self._kill_process_group(proc)
        try:
            await asyncio.wait(pumps, timeout=0.5)
        finally:
            # Also on a second cancel during the grace: no pump may outlive
            # the step.
            for p in pumps:
                p.cancel()
            await asyncio.gather(*pumps, return_exceptions=True)

    async def execute(
        self,
        task: TaskDefinition,
        resolved_command: str,
        context: FlowContext,
    ) -> ExecutionResult:
        """Run ``resolved_command`` as a shell subprocess.

        :param task: the task definition (unused beyond contract compliance)
        :param resolved_command: the shell command with templates resolved
        :param context: runtime :class:`FlowContext`, used for ``timeout``
        :returns: :class:`ExecutionResult` with stdout/stderr/exit code
        """
        # Run under bash explicitly, not create_subprocess_shell: that uses
        # /bin/sh (dash on Debian/Ubuntu/WSL) or cmd.exe on Windows, so
        # bashisms like `set -o pipefail` fail even though the tool is "bash".
        bash = shutil.which("bash")
        if bash is None:
            raise RuntimeError(
                "tool:bash requires `bash` on PATH (install git-bash/WSL on Windows)"
            )
        spawn = asyncio.ensure_future(asyncio.create_subprocess_exec(
            bash,
            "-c",
            resolved_command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(context.working_dir) if context.working_dir else None,
            # Own session/group: lets a timeout or a cancel kill the shell
            # *and* every descendant it spawned (see _kill_process_group),
            # instead of leaving runaway grandchildren alive after the task
            # gives up.
            start_new_session=True,
        ))
        try:
            proc = await asyncio.shield(spawn)
        except asyncio.CancelledError:
            # Cancelled while asyncio still connects the shell's pipes: the
            # shell may already run commands, and asyncio's own cleanup would
            # kill the shell alone. Let the start finish, then kill the group.
            with contextlib.suppress(Exception):
                self._kill_process_group(await spawn)
            raise
        # Streams are pumped incrementally (not via communicate()) so a
        # timeout can kill the process and still keep the partial output:
        # cancelling communicate() discards data it already buffered.
        stdout_buf = bytearray()
        stderr_buf = bytearray()

        async def _pump(
            stream: asyncio.StreamReader, sink: bytearray, live: _LiveOutput
        ) -> None:
            while chunk := await stream.read(65536):
                sink.extend(chunk)
                await live.feed(chunk)
            await live.close()

        pumps = [
            asyncio.create_task(
                _pump(proc.stdout, stdout_buf, _LiveOutput(context.on_step, StepKind.stdout))
            ),
            asyncio.create_task(
                _pump(proc.stderr, stderr_buf, _LiveOutput(context.on_step, StepKind.stderr))
            ),
        ]

        async def _exit_and_drain() -> None:
            await proc.wait()
            # wait() returns at once when the shell's exit was already seen,
            # even while a child it left in the background still holds the
            # pipes. Waiting for the pumps here keeps that drain under the
            # timeout and the kill below. asyncio.wait, not gather: a timeout
            # must not cancel the pumps before _stop lets them finish.
            await asyncio.wait(pumps)

        try:
            await asyncio.wait_for(_exit_and_drain(), timeout=context.timeout)
            await asyncio.gather(*pumps)
        except TimeoutError:
            exited = proc.returncode
            await self._stop(proc, pumps)
            stdout = stdout_buf.decode("utf-8", errors="replace")
            stderr = stderr_buf.decode("utf-8", errors="replace")
            timeout_note = f"timeout after {context.timeout}s"
            if exited is not None:
                # The shell was done; only a background child kept the step
                # waiting, by holding its output. Say so, or the hang reads
                # as a slow script.
                timeout_note += (
                    f": the script finished (exit {exited}) but a process it "
                    "started in the background still holds its output; "
                    "redirect that process's output, e.g. `cmd > log 2>&1 &`"
                )
            return ExecutionResult(
                exit_code=124,
                stdout=stdout,
                stderr=f"{stderr}\n{timeout_note}" if stderr else timeout_note,
                output=stdout,
            )
        except BaseException:
            # Cancelled (fail-fast, `atelier stop`, Ctrl-C, a parent conduit
            # or the engine's backstop timeout) or failed while the command
            # runs: it must not outlive its task. The shell's own session
            # keeps it out of reach of a terminal's Ctrl-C, so only this
            # kill stops it.
            await self._stop(proc, pumps)
            raise
        stdout = stdout_buf.decode("utf-8", errors="replace")
        stderr = stderr_buf.decode("utf-8", errors="replace")
        return ExecutionResult(
            exit_code=proc.returncode or 0,
            stdout=stdout,
            stderr=stderr,
            output=stdout,
        )
