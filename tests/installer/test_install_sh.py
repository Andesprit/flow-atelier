"""Regression tests for install.sh.

The real script is executed, but never against the network and never against
the invoking user's home directory: ``curl`` is replaced by a stub on ``PATH``
that serves a synthetic release from disk, and ``HOME`` points at a tmp dir.
Nothing here downloads software or writes to the caller's shell config.

Covers the two fixes that shipped with this branch:

- shell detection keys off the login shell (``$SHELL``), not the interpreter,
  so ``curl … | bash`` on macOS no longer writes to a ``~/.bashrc`` that zsh
  never reads;
- SHA-256 verification falls back across ``sha256sum`` / ``shasum`` /
  ``openssl`` rather than being skipped on the platform missing one.
"""
from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
from pathlib import Path

import pytest

INSTALL_SH = Path(__file__).resolve().parents[2] / "install.sh"

pytestmark = pytest.mark.skipif(
    shutil.which("bash") is None or os.name == "nt",
    reason="install.sh is a Unix shell script",
)

BINARY = b"#!/bin/sh\necho fake-atelier\n"
DIGEST = hashlib.sha256(BINARY).hexdigest()

# Every asset name the script may resolve to, so the stub works on whichever
# platform CI runs. The script picks one; SHA256SUMS lists them all.
ASSETS = ("atelier-linux-x86_64", "atelier-macos-arm64")

# Everything install.sh calls besides the hashing tool, so a slim PATH can
# isolate which SHA-256 branch the script takes.
_CORE_TOOLS = (
    "bash", "uname", "tr", "mktemp", "grep", "sed", "awk", "head", "tail",
    "cp", "chmod", "mkdir", "rm", "dirname", "basename", "cat",
)

CURL_STUB = r"""#!/usr/bin/env bash
# Stand-in for curl: serves a synthetic release, never touches the network.
set -euo pipefail
out=""
args=()
while [ $# -gt 0 ]; do
    case "$1" in
        -o) out="$2"; shift 2 ;;
        -*) shift ;;
        *) args+=("$1"); shift ;;
    esac
done
url="${args[0]:-}"
case "$url" in
    *releases/latest) cat "$FIXTURE_DIR/release.json" ;;
    *SHA256SUMS)      cp "$FIXTURE_DIR/SHA256SUMS" "$out" ;;
    *)                cp "$FIXTURE_DIR/binary" "$out" ;;
esac
"""


@pytest.fixture
def sandbox(tmp_path):
    """Build an isolated HOME plus a stubbed ``curl`` earlier on PATH.

    :param tmp_path: pytest temp directory fixture.
    :returns: tuple of the fake home dir and the environment to run with.
    """
    fixture = tmp_path / "fixture"
    fixture.mkdir()
    (fixture / "binary").write_bytes(BINARY)
    (fixture / "SHA256SUMS").write_text(
        "".join(f"{DIGEST}  {name}\n" for name in ASSETS)
    )
    # One URL per line, as GitHub's pretty-printed JSON has it: the script
    # extracts with `grep | sed`, and a greedy sed on a single line would take
    # the last URL on it regardless of which asset was asked for.
    urls = "\n".join(
        f'    "browser_download_url": "https://example.invalid/{n}",'
        for n in (*ASSETS, "SHA256SUMS")
    )
    (fixture / "release.json").write_text(
        '{\n  "tag_name": "v9.9.9",\n  "assets": [\n' + urls + "\n  ]\n}\n"
    )

    stub_dir = tmp_path / "stub"
    stub_dir.mkdir()
    curl = stub_dir / "curl"
    curl.write_text(CURL_STUB)
    curl.chmod(0o755)

    home = tmp_path / "home"
    home.mkdir()

    env = {
        **os.environ,
        "HOME": str(home),
        "FIXTURE_DIR": str(fixture),
        "PATH": f"{stub_dir}:{os.environ['PATH']}",
    }
    env.pop("ZDOTDIR", None)
    return home, env


def _run(env, **overrides) -> subprocess.CompletedProcess:
    """Execute install.sh with ``env`` plus ``overrides``.

    :param env: base environment from the sandbox fixture.
    :param overrides: extra environment variables to set.
    :returns: the completed process.
    """
    return subprocess.run(
        ["bash", str(INSTALL_SH)],
        env={**env, **overrides},
        capture_output=True,
        text=True,
        timeout=60,
    )


@pytest.mark.parametrize(
    ("login_shell", "rc_relpath", "expected_line"),
    [
        ("/bin/zsh", ".zshrc", 'export PATH="$PATH:'),
        ("/bin/bash", ".bashrc", 'export PATH="$PATH:'),
        ("/usr/bin/fish", ".config/fish/config.fish", "set -gx PATH $PATH "),
        ("/usr/bin/ksh", ".profile", 'export PATH="$PATH:'),
    ],
)
def test_path_is_written_to_the_login_shells_rc(
    sandbox, login_shell, rc_relpath, expected_line
) -> None:
    """Verify the rc file is chosen from $SHELL, not the interpreter.

    Under ``curl … | bash`` the interpreter is always bash, so keying off
    ``$BASH_VERSION`` wrote ~/.bashrc for every macOS user — a file zsh never
    reads, leaving `atelier` off PATH entirely.

    :param sandbox: isolated home + stubbed curl fixture.
    :param login_shell: value of $SHELL for this case.
    :param rc_relpath: rc file the installer is expected to write.
    :param expected_line: PATH syntax expected for that shell.
    """
    home, env = sandbox
    result = _run(env, SHELL=login_shell)
    assert result.returncode == 0, result.stderr

    rc = home / rc_relpath
    assert rc.exists(), f"{rc_relpath} was not written"
    assert f"Added {home / '.atelier/bin'} to PATH in {rc}" in result.stdout
    assert "Open a new shell" in result.stdout
    if login_shell.endswith("/fish"):
        assert 'set -gx PATH $PATH "$HOME/.atelier/bin"' in result.stdout.splitlines()
    else:
        assert 'export PATH="$PATH:$HOME/.atelier/bin"' in result.stdout.splitlines()
    body = rc.read_text()
    assert expected_line in body
    assert str(home / ".atelier" / "bin") in body

    # No other rc file was touched.
    for other in (".zshrc", ".bashrc", ".profile", ".config/fish/config.fish"):
        if other != rc_relpath:
            assert not (home / other).exists(), f"unexpectedly wrote {other}"


def test_skips_new_shell_hint_when_install_dir_is_on_path(sandbox) -> None:
    home, env = sandbox
    result = _run(env, SHELL="/bin/bash", PATH=f"{env['PATH']}:{home / '.atelier/bin'}")
    assert result.returncode == 0, result.stderr
    assert "Open a new shell" not in result.stdout
    assert 'export PATH="$PATH:$HOME/.atelier/bin"' not in result.stdout
    assert (home / ".bashrc").exists()


def test_zdotdir_is_honoured_for_zsh(sandbox, tmp_path) -> None:
    """Verify a zsh user with ZDOTDIR gets their real .zshrc, not ~/.zshrc.

    :param sandbox: isolated home + stubbed curl fixture.
    :param tmp_path: pytest temp directory fixture.
    """
    home, env = sandbox
    zdotdir = tmp_path / "zdot"
    zdotdir.mkdir()
    result = _run(env, SHELL="/bin/zsh", ZDOTDIR=str(zdotdir))
    assert result.returncode == 0, result.stderr
    assert (zdotdir / ".zshrc").exists()
    assert not (home / ".zshrc").exists()


def test_path_edit_is_idempotent(sandbox) -> None:
    """Verify a second install does not append the PATH block again.

    :param sandbox: isolated home + stubbed curl fixture.
    """
    home, env = sandbox
    assert _run(env, SHELL="/bin/zsh").returncode == 0
    first = (home / ".zshrc").read_text()
    assert _run(env, SHELL="/bin/zsh").returncode == 0
    assert (home / ".zshrc").read_text() == first


def test_binary_is_installed_and_executable(sandbox) -> None:
    """Verify the verified asset lands in ~/.atelier/bin and is executable.

    :param sandbox: isolated home + stubbed curl fixture.
    """
    home, env = sandbox
    result = _run(env, SHELL="/bin/zsh")
    assert result.returncode == 0, result.stderr
    binary = home / ".atelier" / "bin" / "atelier"
    assert binary.read_bytes() == BINARY
    assert os.access(binary, os.X_OK)
    assert "SHA-256 verified." in result.stdout


def test_checksum_mismatch_aborts_before_install(sandbox) -> None:
    """Verify a tampered asset is refused and nothing is installed.

    :param sandbox: isolated home + stubbed curl fixture.
    """
    home, env = sandbox
    Path(env["FIXTURE_DIR"], "binary").write_bytes(b"tampered payload")
    result = _run(env, SHELL="/bin/zsh")
    assert result.returncode != 0
    assert "SHA-256 mismatch" in result.stderr
    assert not (home / ".atelier" / "bin" / "atelier").exists()


@pytest.mark.parametrize("available", ["sha256sum", "shasum", "openssl"])
def test_each_sha_tool_alone_can_verify(sandbox, tmp_path, available) -> None:
    """Verify verification succeeds with only one of the three tools present.

    Stock macOS ships `shasum` but not `sha256sum`; the fallback chain is what
    keeps verification from being silently skipped there.

    :param sandbox: isolated home + stubbed curl fixture.
    :param tmp_path: pytest temp directory fixture.
    :param available: the single hashing tool left on PATH.
    """
    if shutil.which(available) is None:
        pytest.skip(f"{available} not installed on this host")

    home, env = sandbox
    # A minimal PATH holding the curl stub, coreutils basics, and exactly one
    # hashing tool -- so the script has to take that branch.
    slim = tmp_path / "slim"
    slim.mkdir()
    for tool in (*_CORE_TOOLS, available):
        found = shutil.which(tool)
        if found:
            (slim / tool).symlink_to(found)
    shutil.copy(Path(env["PATH"].split(":")[0]) / "curl", slim / "curl")
    (slim / "curl").chmod(0o755)

    result = _run(env, SHELL="/bin/zsh", PATH=str(slim))
    assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
    assert "SHA-256 verified." in result.stdout
    assert (home / ".atelier" / "bin" / "atelier").exists()


def test_no_sha_tool_refuses_to_install(sandbox, tmp_path) -> None:
    """Verify a host with no hashing tool aborts instead of skipping the check.

    :param sandbox: isolated home + stubbed curl fixture.
    :param tmp_path: pytest temp directory fixture.
    """
    home, env = sandbox
    slim = tmp_path / "nohash"
    slim.mkdir()
    for tool in _CORE_TOOLS:
        found = shutil.which(tool)
        if found:
            (slim / tool).symlink_to(found)
    shutil.copy(Path(env["PATH"].split(":")[0]) / "curl", slim / "curl")
    (slim / "curl").chmod(0o755)

    result = _run(env, SHELL="/bin/zsh", PATH=str(slim))
    assert result.returncode != 0
    assert "no SHA-256 tool found" in result.stderr
    assert not (home / ".atelier" / "bin" / "atelier").exists()
