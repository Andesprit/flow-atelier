"""Helpers for the tests that drive a real POSIX shell and a real `atelier`.

Windows is why these exist, and why they are shared rather than repeated:

* A bare ``"bash"`` in an argv is resolved by ``CreateProcess``, which finds
  ``System32\\bash.exe`` — the WSL launcher, with no distribution installed on
  CI — before the Git Bash that is on PATH. ``shutil.which`` walks PATH the
  way the ``tool:bash`` executor does, so the tests and the product agree on
  which shell they mean.
* ``Path.write_text`` translates ``\\n`` to ``\\r\\n``, and a ``#!`` line with
  a trailing carriage return is not a usable interpreter.
* A host path handed to that shell has to be in the shell's own namespace,
  which is what the executor's ``to_bash_path`` already computes.
"""
from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from flow_atelier.services.executor.bash import to_bash_path

# Runs this interpreter's CLI as plain `atelier`, without depending on the
# console script being installed on PATH inside the test environment.
CLI = "from flow_atelier.main import app; app()"


def bash() -> str:
    """Return the same ``bash`` the ``tool:bash`` executor would run.

    :returns: absolute path to the shell.
    """
    found = shutil.which("bash")
    if found is None:
        pytest.skip("needs bash on PATH")
    return found


def write_shim(bin_dir: Path, guard: str = "") -> Path:
    """Write an `atelier` shim that runs this interpreter's CLI.

    :param bin_dir: directory holding the shim; goes first on the child's PATH.
    :param guard: shell lines placed before the exec, to break one subcommand.
    :returns: the shim's path.
    """
    shim = bin_dir / "atelier"
    shim.write_text(
        f'#!/bin/sh\n{guard}exec "{to_bash_path(sys.executable)}" -c \'{CLI}\' "$@"\n',
        newline="\n",
    )
    shim.chmod(0o755)
    return shim


def record_path(shell_var: str, marker_env: str) -> str:
    """Shell line that saves ``$shell_var`` as a path the host can open.

    Git Bash reports ``/tmp/...``, which means nothing to Python on Windows;
    ``cygpath -w`` turns it back into a drive path, and is absent everywhere
    the value was already a host path.

    :param shell_var: name of the shell variable holding the path.
    :param marker_env: name of the env var naming the file to write.
    :returns: one shell line, newline included.
    """
    return (
        f'printf "%s\\n" "$(cygpath -w "${shell_var}" 2>/dev/null'
        f' || printf %s "${shell_var}")" > "${marker_env}"\n'
    )


def run_script(body: str, script: Path, cwd: Path, env: dict, timeout: float):
    """Run ``body`` as one shell script under the resolved bash.

    :param body: the script text.
    :param script: file to write it to.
    :param cwd: working directory for the shell.
    :param env: the child environment.
    :param timeout: seconds before the shell is killed.
    :returns: the CompletedProcess, decoded as UTF-8.
    """
    script.write_text(body, newline="\n")
    return subprocess.run(
        [bash(), to_bash_path(script)],
        cwd=cwd,
        env=env,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
    )


def run_expression(expression: str, cwd: Path, env: dict, timeout: float):
    """Run one shell expression under the resolved bash.

    Not ``shell=True``: that is ``cmd.exe`` on Windows, which cannot read the
    ``$(...)`` and ``&&`` the documented one-liners are written in.

    :param expression: the shell expression to evaluate.
    :param cwd: working directory for the shell.
    :param env: the child environment.
    :param timeout: seconds before the shell is killed.
    :returns: the CompletedProcess, decoded as UTF-8.
    """
    return subprocess.run(
        [bash(), "-c", expression],
        cwd=cwd,
        env=env,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
    )
