"""Keep public-name redaction tests independent of the developer's shell environment."""

import os

import pytest


@pytest.fixture(autouse=True)
def runner_environment(monkeypatch, _isolate_global_atelier_dir):
    keep = {"HOME", "USERPROFILE", "PATH", "PATHEXT", "SYSTEMROOT", "WINDIR", "COMSPEC",
            "TMP", "TEMP", "TMPDIR", "ATELIER_GLOBAL_ATELIER_DIR", "ATELIER_NO_UPDATE_CHECK"}
    for name in tuple(os.environ):
        if name.upper() not in keep:
            monkeypatch.delenv(name)
