"""Global pytest configuration.

Fails the run immediately if any required env var is missing, so tests
don't die halfway with cryptic errors. Add new settings here as they
become genuinely required.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

REQUIRED_ENV_VARS: list[str] = []


def pytest_configure(config: pytest.Config) -> None:
    """Abort collection if any required env var is missing.

    :param config: pytest config (unused)
    :returns: None
    """
    missing = [name for name in REQUIRED_ENV_VARS if not os.environ.get(name)]
    if missing:
        pytest.exit(f"missing required env vars: {missing}", returncode=2)


@pytest.fixture(autouse=True)
def _isolate_global_atelier_dir(tmp_path_factory, monkeypatch):
    """Point the global atelier dir at a throwaway tmp path for every test.

    Prevents tests from reading or writing the real ``~/.atelier``.

    :param tmp_path_factory: pytest tmp_path_factory fixture.
    :param monkeypatch: pytest monkeypatch fixture.
    """
    global_dir = tmp_path_factory.mktemp("global_atelier")
    monkeypatch.setenv("ATELIER_GLOBAL_ATELIER_DIR", str(global_dir))
    monkeypatch.setenv("ATELIER_NO_UPDATE_CHECK", "1")
    yield Path(global_dir)
