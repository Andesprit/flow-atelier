"""Update check and self-update for frozen (PyInstaller) binaries.

Once a day, the first command spawns a daemon thread that asks GitHub
Releases for the latest tag. When a newer version exists, the process says
so on stderr as it exits. Nothing is downloaded until the user runs
``atelier self-update``, which fetches the binary, verifies it and swaps it
in place.

For ``pip``/``uv`` installs (``sys.frozen`` is falsy) the daily check prints
an upgrade hint instead. Set ``ATELIER_NO_UPDATE_CHECK=1`` to disable the
check (useful in CI/tests).

Trust model: the downloaded binary is verified only against a
``SHA256SUMS`` file fetched from the same GitHub release over HTTPS.
That detects transport corruption but not a tampered release — there is
no client-verifiable signature, so the integrity of an update rests
entirely on GitHub release integrity plus TLS, not on a key the client
holds. This is an accepted limitation for this project.
"""
from __future__ import annotations

import atexit
import hashlib
import json
import os
import platform
import sys
import tempfile
import threading
import time
import urllib.request
from pathlib import Path
from typing import Any

OWNER = "LGuillermoAngaritaG"
REPO = "flow-atelier"
RELEASES_API = f"https://api.github.com/repos/{OWNER}/{REPO}/releases/latest"

# Newer release tag found by the daemon thread, read by the atexit handler.
_available: str | None = None
_lock = threading.Lock()


class UpdateError(RuntimeError):
    """A self-update could not be completed."""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def is_frozen_binary() -> bool:
    """Return ``True`` when running inside a PyInstaller bundle."""
    return getattr(sys, "frozen", False)


def _parse_version(v: str) -> tuple[int, ...]:
    """Parse a dotted version string into a comparable tuple of ints.

    Strips a leading ``v``. Non-numeric/garbage parts make the whole
    version sort as ``(-1,)`` so the caller fails closed (treats it as
    "not newer") instead of crashing on an unparseable release tag.
    """
    v = v.strip().lstrip("v")
    try:
        return tuple(int(part) for part in v.split("."))
    except ValueError:
        return (-1,)


def _is_newer(tag: str) -> bool:
    """Return whether release ``tag`` is newer than the running version."""
    from flow_atelier import __version__

    return _parse_version(tag) > _parse_version(__version__)


def _platform_asset_name() -> str:
    """Return the release asset name for the current platform."""
    system = platform.system().lower()
    machine = platform.machine().lower()

    if system == "linux" and machine == "x86_64":
        return "atelier-linux-x86_64"
    if system == "darwin":
        # Only an arm64 build is published; Intel macs cannot run it and
        # there is no Rosetta fallback (consistent with install.sh).
        if machine == "arm64":
            return "atelier-macos-arm64"
        raise RuntimeError(f"unsupported platform: {system}-{machine}")
    if system == "windows" and machine in ("amd64", "x86_64"):
        return "atelier-windows-x86_64.exe"

    raise RuntimeError(f"unsupported platform: {system}-{machine}")


def _fetch_json(url: str) -> Any:
    """Fetch *url* and return the parsed JSON response."""
    req = urllib.request.Request(url, headers={"User-Agent": "flow-atelier-updater"})
    with urllib.request.urlopen(req, timeout=10) as resp:  # noqa: S310
        return json.loads(resp.read())


def _fetch_bytes(url: str) -> bytes:
    """Fetch *url* and return the raw bytes."""
    req = urllib.request.Request(url, headers={"User-Agent": "flow-atelier-updater"})
    with urllib.request.urlopen(req, timeout=60) as resp:  # noqa: S310
        return resp.read()


def _latest_release() -> dict[str, Any]:
    """Return the latest release record from the GitHub API."""
    return _fetch_json(RELEASES_API)


def _download_and_verify(
    asset_url: str,
    asset_name: str,
    sums_url: str,
) -> bytes | None:
    """Download *asset_url*, verify its SHA-256 against *sums_url*, return bytes.

    Returns ``None`` on any failure (network error, hash mismatch, etc.).
    """
    try:
        binary = _fetch_bytes(asset_url)
        sums_text = _fetch_bytes(sums_url).decode("utf-8")
    except Exception:
        return None

    expected_hash: str | None = None
    for line in sums_text.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[1] == asset_name:
            expected_hash = parts[0]
            break

    if expected_hash is None:
        return None

    actual_hash = hashlib.sha256(binary).hexdigest()
    if actual_hash != expected_hash:
        return None

    return binary


def _swap_binary(binary: bytes) -> None:
    """Replace the running executable with ``binary``, keeping a ``.old`` copy.

    The new file is written beside the live one, given the live one's mode,
    then moved over it. If that final move fails the backup is restored, so a
    botched swap leaves a runnable binary instead of a hole.

    :param binary: the verified replacement executable.
    :raises OSError: when the swap fails; the original is back in place.
    """
    current = os.path.realpath(sys.executable)
    backup = current + ".old"

    fd, tmp_path = tempfile.mkstemp(dir=os.path.dirname(current))
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(binary)

        # mkstemp creates the file 0600. Copy the live binary's mode so the
        # swapped-in file stays executable.
        try:
            os.chmod(tmp_path, os.stat(current).st_mode)
        except OSError:
            os.chmod(tmp_path, 0o755)

        # Windows may refuse to rename a running .exe; fall through to a
        # direct replace in that case.
        renamed_backup = False
        try:
            if os.path.exists(backup):
                os.remove(backup)
            os.rename(current, backup)
            renamed_backup = True
        except OSError:
            pass

        try:
            os.replace(tmp_path, current)
        except OSError:
            if renamed_backup:
                os.replace(backup, current)
            raise
    finally:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)


def self_update() -> str | None:
    """Fetch, verify and install the latest release over the running binary.

    :returns: the installed release tag, or ``None`` when already up to date.
    :raises UpdateError: when no usable asset exists or verification fails.
    :raises OSError: when the binary swap fails; the original stays in place.
    """
    try:
        release = _latest_release()
    except Exception as exc:
        raise UpdateError(f"could not reach GitHub releases: {exc}") from exc
    tag = release.get("tag_name", "")
    if not tag or not _is_newer(tag):
        return None

    asset_name = _platform_asset_name()
    asset_url: str | None = None
    sums_url: str | None = None
    for asset in release.get("assets", []):
        name = asset.get("name", "")
        if name == asset_name:
            asset_url = asset.get("browser_download_url")
        elif name == "SHA256SUMS":
            sums_url = asset.get("browser_download_url")
    if not asset_url or not sums_url:
        raise UpdateError(f"release {tag} has no {asset_name} asset with a checksum")

    binary = _download_and_verify(asset_url, asset_name, sums_url)
    if binary is None:
        raise UpdateError("download failed or the SHA-256 checksum did not match")

    _swap_binary(binary)
    return tag


# ---------------------------------------------------------------------------
# Daily check
# ---------------------------------------------------------------------------

CHECK_INTERVAL_SECONDS = 24 * 60 * 60


def _background_check() -> None:
    """Run in a daemon thread: record the latest tag when it is newer."""
    global _available  # noqa: PLW0603

    try:
        tag = _latest_release().get("tag_name", "")
        if tag and _is_newer(tag):
            with _lock:
                _available = tag
    except Exception:
        # The check must never crash the CLI.
        pass


def _announce_available() -> None:
    """``atexit`` handler: point at ``self-update`` when a newer tag was seen.

    A command that exits before the thread returns prints nothing; the
    stamp is already touched, so the next check is tomorrow's.
    """
    with _lock:
        tag = _available
    if tag:
        print(
            f"flow-atelier {tag.lstrip('v')} is available: "
            "run `atelier self-update` to install it.",
            file=sys.stderr,
        )


def _stamp_path() -> Path:
    """Return the throttle stamp's location, under the global atelier dir.

    Resolved per call rather than at import: ``global_atelier_dir`` is
    configurable, and binding it at module import would pin whatever ``HOME``
    happened to be set to when this module was first loaded.

    :returns: path to the last-update-check stamp file.
    """
    from flow_atelier.core.settings import AtelierSettings

    return AtelierSettings().global_atelier_dir / ".last-update-check"


def _due_for_check(now: float) -> bool:
    """Return True at most once per :data:`CHECK_INTERVAL_SECONDS`, and stamp it.

    Without this the hint prints on *every* command (noise in scripts and
    pipes) and the frozen binary calls the GitHub API on every invocation,
    which burns the 60/hour unauthenticated rate limit. A stamp that can't be
    read or written fails open: the check runs, it just isn't throttled.

    :param now: current wall-clock time, seconds since the epoch.
    :returns: True when a check should run now.
    """
    stamp = _stamp_path()
    try:
        if now - stamp.stat().st_mtime < CHECK_INTERVAL_SECONDS:
            return False
    except OSError:
        pass  # missing or unreadable stamp — treat as due
    try:
        stamp.parent.mkdir(parents=True, exist_ok=True)
        stamp.touch()
    except OSError:
        pass  # read-only HOME: check every time rather than never
    return True


def start_background_update_check() -> None:
    """Spawn the background version check (or print a pip hint).

    Called from the CLI root callback. No-op when
    ``ATELIER_NO_UPDATE_CHECK=1`` is set, and throttled to once a day.
    """
    if os.environ.get("ATELIER_NO_UPDATE_CHECK") == "1":
        return

    if not _due_for_check(time.time()):
        return

    if not is_frozen_binary():
        # pip / uv install — just hint the user.
        print(
            "Tip: run `uv tool upgrade flow-atelier` to check for updates.",
            file=sys.stderr,
        )
        return

    thread = threading.Thread(target=_background_check, daemon=True)
    thread.start()

    atexit.register(_announce_available)
