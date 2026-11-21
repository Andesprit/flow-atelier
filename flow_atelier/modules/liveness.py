"""Derive run liveness from the saved heartbeat and a local process probe."""
from __future__ import annotations

import os
import socket
import sys
from enum import Enum
from time import time

from flow_atelier.schemas.progress import FlowStatus, Progress


class _PidState(Enum):
    """Outcome of probing a local pid: provably alive, provably dead, or unknown."""

    alive = "alive"
    dead = "dead"
    unknown = "unknown"


def _pid_state(pid: int) -> _PidState:
    """Probe a pid on *this* host without signalling it.

    ``dead`` only when the OS proves no such process exists; every permission
    or probe error is ``unknown`` so callers can stay conservative. ``os.kill``
    is never used on Windows, where ``os.kill(pid, 0)`` calls ``TerminateProcess``
    and would kill the very runner we are inspecting.
    """
    if sys.platform == "win32":
        return _win_pid_state(pid)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return _PidState.dead
    except (PermissionError, OSError):
        return _PidState.unknown
    return _PidState.alive


def _win_pid_state(pid: int) -> _PidState:
    """Windows probe via ``OpenProcess``/``GetExitCodeProcess`` (no signalling)."""
    import ctypes
    from ctypes import wintypes

    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    STILL_ACTIVE = 259
    ERROR_INVALID_PARAMETER = 87

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        if ctypes.get_last_error() == ERROR_INVALID_PARAMETER:
            return _PidState.dead  # no such process
        return _PidState.unknown  # access denied or other probe failure
    try:
        kernel32.GetExitCodeProcess.argtypes = (
            wintypes.HANDLE,
            ctypes.POINTER(wintypes.DWORD),
        )
        kernel32.GetExitCodeProcess.restype = wintypes.BOOL
        code = wintypes.DWORD()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
            return _PidState.unknown
        return _PidState.alive if code.value == STILL_ACTIVE else _PidState.dead
    finally:
        kernel32.CloseHandle(handle)


def heartbeat_stale(progress: Progress) -> bool:
    return progress.heartbeat_at is not None and time() - progress.heartbeat_at.timestamp() >= 120


def is_crashed(progress: Progress) -> bool:
    """A missed heartbeat or a provably dead local PID marks an unparked run crashed."""
    if progress.status != FlowStatus.running or progress.pending_ask:
        return False
    if heartbeat_stale(progress):
        return True
    if progress.runner_pid is None:
        return False
    if progress.runner_host != socket.gethostname():
        return False
    return _pid_state(progress.runner_pid) is _PidState.dead


def is_runner_alive(progress: Progress) -> bool:
    """Return True only when a ``running`` flow's local runner is provably alive.

    :param progress: the flow's progress snapshot.
    :returns: True iff status is ``running``, the runner pid/host are recorded,
        the host matches this machine, and a liveness probe proves the pid is
        alive. Any other case returns False.
    """
    if progress.status != FlowStatus.running:
        return False
    if progress.runner_pid is None:
        return False
    if progress.runner_host != socket.gethostname():
        return False
    return _pid_state(progress.runner_pid) is _PidState.alive


def display_status(progress: Progress) -> str:
    """Classify the saved progress for a human or dashboard without changing it."""
    if progress.status == FlowStatus.running and progress.pending_ask:
        return "needs_input"
    return "crashed" if is_crashed(progress) else progress.status.value


class StopDecision(str, Enum):
    """Verdict for whether a flow can be safely stopped from this host.

    Only ``stoppable`` means "send SIGTERM"; every other value is a refusal
    that the caller turns into a plain message and a non-zero exit.
    """

    stoppable = "stoppable"
    not_running = "not_running"
    crashed = "crashed"
    foreign_host = "foreign_host"
    no_pid = "no_pid"
    not_stoppable = "not_stoppable"
    shared_runner = "shared_runner"


def stop_decision(progress: Progress, others: list[Progress]) -> StopDecision:
    """Decide whether ``progress``'s flow can be stopped by signalling its runner.

    Conservative by design: signalling ``runner_pid`` is coarse (it hits the
    whole process), so every case where that would be unsafe or pointless
    refuses rather than guesses. Checks run in order so the most specific
    reason wins.

    :param progress: the target flow's progress snapshot.
    :param others: progress snapshots of every *other* known flow, used to
        detect a runner process shared by more than one running flow (the
        scheduler-daemon case, where one PID owns several flows).
    :returns: a :class:`StopDecision`; only ``stoppable`` authorises a signal.
    """
    if progress.status != FlowStatus.running:
        return StopDecision.not_running
    if is_crashed(progress):
        return StopDecision.crashed
    if progress.runner_host != socket.gethostname():
        return StopDecision.foreign_host
    if progress.runner_pid is None:
        return StopDecision.no_pid
    state = _pid_state(progress.runner_pid)
    if state is _PidState.dead:
        return StopDecision.crashed
    if state is _PidState.unknown:
        # Pid exists but we can't probe it cleanly — treat as foreign rather
        # than risk signalling a process we don't actually own.
        return StopDecision.foreign_host
    if not progress.stoppable:
        # The runner never installed a graceful-stop handler (daemon/serve/
        # nested run), so its SIGTERM means "tear the whole process down".
        # Refuse independent of whether a co-tenant flow happens to exist.
        return StopDecision.not_stoppable
    for other in others:
        if other.status != FlowStatus.running:
            continue
        if (
            other.runner_pid == progress.runner_pid
            and other.runner_host == progress.runner_host
        ):
            return StopDecision.shared_runner
    return StopDecision.stoppable
