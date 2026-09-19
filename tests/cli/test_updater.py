"""Tests for flow_atelier.cli.updater — all network calls are mocked."""
from __future__ import annotations

import os
import sys
import time
from unittest.mock import patch

import pytest

import flow_atelier.cli.updater as mod
from flow_atelier.cli import app
from flow_atelier.cli.updater import (
    UpdateError,
    _announce_available,
    _background_check,
    _download_and_verify,
    _parse_version,
    _platform_asset_name,
    _swap_binary,
    is_frozen_binary,
    self_update,
    start_background_update_check,
)

# ---------------------------------------------------------------------------
# 1. is_frozen_binary
# ---------------------------------------------------------------------------


def test_is_frozen_returns_false_in_dev():
    """In the test environment (not PyInstaller), is_frozen_binary() is False."""
    assert is_frozen_binary() is False


# ---------------------------------------------------------------------------
# 2. _platform_asset_name
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("sys_name", "machine", "expected"),
    [
        ("Linux", "x86_64", "atelier-linux-x86_64"),
        ("Darwin", "arm64", "atelier-macos-arm64"),
        ("Windows", "AMD64", "atelier-windows-x86_64.exe"),
    ],
)
def test_platform_asset_name_known_platforms(sys_name, machine, expected):
    """All three supported platforms produce the correct asset name."""
    with (
        patch("flow_atelier.cli.updater.platform") as mock_plat,
    ):
        mock_plat.system.return_value = sys_name
        mock_plat.machine.return_value = machine
        assert _platform_asset_name() == expected


def test_platform_asset_name_intel_mac_raises():
    """Intel macOS (x86_64) is unsupported and must raise, not return arm64."""
    with patch("flow_atelier.cli.updater.platform") as mock_plat:
        mock_plat.system.return_value = "Darwin"
        mock_plat.machine.return_value = "x86_64"
        with pytest.raises(RuntimeError):
            _platform_asset_name()


# ---------------------------------------------------------------------------
# 2b. _parse_version — numeric comparison
# ---------------------------------------------------------------------------


def test_parse_version_numeric_ordering():
    """0.10.0 must compare as newer than 0.9.0 (not lexicographically)."""
    assert _parse_version("0.10.0") > _parse_version("0.9.0")
    assert _parse_version("v0.10.0") > _parse_version("0.9.0")
    assert _parse_version("0.9.0") == _parse_version("0.9.0")


def test_parse_version_garbage_fails_closed():
    """An unparseable tag sorts below any real version (fails closed)."""
    assert _parse_version("not-a-version") < _parse_version("0.0.1")


# ---------------------------------------------------------------------------
# 3. start_background_update_check disabled by env
# ---------------------------------------------------------------------------


def test_background_check_disabled_by_env(monkeypatch):
    """ATELIER_NO_UPDATE_CHECK=1 prevents thread spawn and pip hint."""
    monkeypatch.setenv("ATELIER_NO_UPDATE_CHECK", "1")
    # Should return immediately without doing anything.
    start_background_update_check()
    # No assertion needed — just verify no exception and no stderr output.


# ---------------------------------------------------------------------------
# 4. daily check only announces; nothing is downloaded
# ---------------------------------------------------------------------------


def test_background_check_announces_newer_version(monkeypatch, capsys):
    """A newer tag is reported on stderr with the self-update command, not installed."""
    monkeypatch.setattr(mod, "_available", None)
    monkeypatch.setattr(mod, "_latest_release", lambda: {"tag_name": "v99.0.0"})
    with patch("flow_atelier.cli.updater._fetch_bytes") as fetch:
        _background_check()
        fetch.assert_not_called()
    _announce_available()
    err = capsys.readouterr().err
    assert "99.0.0" in err
    assert "atelier self-update" in err


def test_background_check_is_silent_when_current(monkeypatch, capsys):
    """An equal or older tag prints nothing at exit."""
    monkeypatch.setattr(mod, "_available", None)
    monkeypatch.setattr(mod, "_latest_release", lambda: {"tag_name": "v0.0.1"})
    _background_check()
    _announce_available()
    assert capsys.readouterr().err == ""


# ---------------------------------------------------------------------------
# 4b. self_update
# ---------------------------------------------------------------------------


def test_self_update_returns_none_when_current(monkeypatch):
    """Nothing is downloaded or swapped when the release is not newer."""
    monkeypatch.setattr(mod, "_latest_release", lambda: {"tag_name": "v0.0.1"})
    with patch("flow_atelier.cli.updater._swap_binary") as swap:
        assert self_update() is None
        swap.assert_not_called()


def test_self_update_swaps_verified_binary(monkeypatch):
    """A newer release with a matching checksum is swapped in and its tag returned."""
    release = {
        "tag_name": "v99.0.0",
        "assets": [
            {"name": "atelier-linux-x86_64", "browser_download_url": "https://x/bin"},
            {"name": "SHA256SUMS", "browser_download_url": "https://x/sums"},
        ],
    }
    monkeypatch.setattr(mod, "_latest_release", lambda: release)
    monkeypatch.setattr(mod, "_platform_asset_name", lambda: "atelier-linux-x86_64")
    monkeypatch.setattr(mod, "_download_and_verify", lambda *a: b"new binary")
    with patch("flow_atelier.cli.updater._swap_binary") as swap:
        assert self_update() == "v99.0.0"
        swap.assert_called_once_with(b"new binary")


def test_self_update_rejects_bad_checksum(monkeypatch):
    """A failed verification raises UpdateError and swaps nothing."""
    release = {
        "tag_name": "v99.0.0",
        "assets": [
            {"name": "atelier-linux-x86_64", "browser_download_url": "https://x/bin"},
            {"name": "SHA256SUMS", "browser_download_url": "https://x/sums"},
        ],
    }
    monkeypatch.setattr(mod, "_latest_release", lambda: release)
    monkeypatch.setattr(mod, "_platform_asset_name", lambda: "atelier-linux-x86_64")
    monkeypatch.setattr(mod, "_download_and_verify", lambda *a: None)
    with patch("flow_atelier.cli.updater._swap_binary") as swap:
        with pytest.raises(UpdateError):
            self_update()
        swap.assert_not_called()


def test_self_update_command_points_pip_installs_at_uv(monkeypatch):
    """Outside a frozen binary the command exits 1 with the uv upgrade hint."""
    monkeypatch.setenv("ATELIER_NO_UPDATE_CHECK", "1")
    from typer.testing import CliRunner

    result = CliRunner().invoke(app, ["self-update"])
    assert result.exit_code == 1
    assert "uv tool upgrade" in result.output


# ---------------------------------------------------------------------------
# 5. pip install shows upgrade hint
# ---------------------------------------------------------------------------


def test_pip_install_shows_upgrade_hint(monkeypatch, tmp_path, capsys):
    """Non-frozen install prints a uv upgrade hint to stderr."""
    monkeypatch.delenv("ATELIER_NO_UPDATE_CHECK", raising=False)
    # Redirect the throttle stamp so the test never reads or writes the real
    # global dir (and so a stamp left by a prior run can't suppress the hint).
    monkeypatch.setenv("ATELIER_GLOBAL_ATELIER_DIR", str(tmp_path))
    # is_frozen_binary() returns False (default in test env).
    start_background_update_check()
    captured = capsys.readouterr()
    assert "uv tool upgrade" in captured.err


def test_upgrade_hint_throttled_to_once_per_interval(monkeypatch, tmp_path, capsys):
    """The hint prints on the first call, then stays quiet until the interval lapses."""
    monkeypatch.delenv("ATELIER_NO_UPDATE_CHECK", raising=False)
    monkeypatch.setenv("ATELIER_GLOBAL_ATELIER_DIR", str(tmp_path))
    stamp = mod._stamp_path()

    start_background_update_check()
    assert "uv tool upgrade" in capsys.readouterr().err

    start_background_update_check()
    assert capsys.readouterr().err == ""

    # Age the stamp past the interval: the hint is due again.
    old = time.time() - mod.CHECK_INTERVAL_SECONDS - 1
    os.utime(stamp, (old, old))
    start_background_update_check()
    assert "uv tool upgrade" in capsys.readouterr().err


def test_check_is_due_when_stamp_unwritable(monkeypatch, tmp_path):
    """An unwritable stamp location fails open — check every time, not never."""
    monkeypatch.setenv(
        "ATELIER_GLOBAL_ATELIER_DIR", str(tmp_path / "nonexistent-file" / "sub")
    )
    monkeypatch.setattr(
        mod.Path, "mkdir", lambda *a, **k: (_ for _ in ()).throw(OSError("read-only"))
    )
    assert mod._due_for_check(time.time()) is True


def test_stamp_path_follows_configured_global_dir(monkeypatch, tmp_path):
    """The stamp lands under the configured global dir, not a hardcoded ~/.atelier."""
    monkeypatch.setenv("ATELIER_GLOBAL_ATELIER_DIR", str(tmp_path / "custom"))
    assert mod._stamp_path() == tmp_path / "custom" / ".last-update-check"


# ---------------------------------------------------------------------------
# 6. verify hash rejects mismatch
# ---------------------------------------------------------------------------


def test_verify_hash_rejects_mismatch():
    """_download_and_verify returns None when the SHA-256 doesn't match."""
    fake_binary = b"this is a fake binary"
    wrong_hash = "0" * 64

    sums_content = f"{wrong_hash}  atelier-linux-x86_64\n"

    with (
        patch("flow_atelier.cli.updater._fetch_bytes") as mock_fetch,
    ):
        # First call = binary, second call = sums text.
        mock_fetch.side_effect = [
            fake_binary,
            sums_content.encode("utf-8"),
        ]
        result = _download_and_verify(
            "https://example.com/atelier-linux-x86_64",
            "atelier-linux-x86_64",
            "https://example.com/SHA256SUMS",
        )

    assert result is None


# ---------------------------------------------------------------------------
# 7. _swap_binary preserves the executable bit
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="POSIX executable-bit semantics; on Windows executability is "
    "extension-based and os.chmod cannot set S_IXUSR.",
)
def test_swap_binary_preserves_executable_bit(tmp_path, monkeypatch):
    """A swapped-in binary must remain executable, not inherit 0600."""
    import stat

    target = tmp_path / "atelier"
    target.write_bytes(b"old binary")
    target.chmod(0o755)

    monkeypatch.setattr(mod.sys, "executable", str(target))
    _swap_binary(b"new binary")

    assert target.read_bytes() == b"new binary"
    assert os.stat(target).st_mode & stat.S_IXUSR


# ---------------------------------------------------------------------------
# 8. _swap_binary rolls back when the final replace fails
# ---------------------------------------------------------------------------


def test_swap_binary_rolls_back_on_replace_failure(tmp_path, monkeypatch):
    """If os.replace fails after the .old rename, the original is restored."""
    target = tmp_path / "atelier"
    target.write_bytes(b"original binary")
    target.chmod(0o755)

    monkeypatch.setattr(mod.sys, "executable", str(target))

    real_replace = mod.os.replace
    calls = {"n": 0}

    def flaky_replace(src, dst):
        # First call = tmp -> live (force failure); later calls (rollback) work.
        calls["n"] += 1
        if calls["n"] == 1:
            raise OSError("simulated replace failure")
        return real_replace(src, dst)

    monkeypatch.setattr(mod.os, "replace", flaky_replace)

    with pytest.raises(OSError):
        _swap_binary(b"new binary")

    assert target.exists()
    assert target.read_bytes() == b"original binary"
