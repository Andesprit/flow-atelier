"""Fresh-process checks that everyday CLI commands skip server and agent libraries.

FastAPI/uvicorn are only for `atelier serve` and the ACP client library only
for `harness:*` tasks; importing them on every start made help, show, list and
plain shell runs pay for both.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

HEAVY = ("acp", "fastapi", "uvicorn")

# Runs the real CLI, then reports which heavy top-level packages were loaded.
_DRIVER = """
import atexit, json, runpy, sys
heavy = set(sys.argv[1].split(","))
atexit.register(lambda: sys.stderr.write(
    "LOADED=" + json.dumps(sorted({m.split(".")[0] for m in sys.modules} & heavy)) + "\\n"
))
sys.argv = ["atelier", *sys.argv[2:]]
runpy.run_module("flow_atelier.main", run_name="__main__")
"""

CONDUIT = """\
name: hello
description: Print one line
inputs:
  who: Who to greet
tasks:
  - greet:
      description: Greet someone
      tool: tool:bash
      task: "printf 'hello %s' '{{inputs.who}}'"
      depends_on: []
"""


def _run(project: Path, *args: str) -> tuple[subprocess.CompletedProcess, list[str]]:
    """Run ``atelier *args`` in a fresh interpreter inside ``project``.

    :returns: the completed process and the heavy packages it imported.
    """
    env = {
        **os.environ,
        "ATELIER_NO_UPDATE_CHECK": "1",
        "ATELIER_GLOBAL_ATELIER_DIR": str(project / "global"),
        "NO_COLOR": "1",
    }
    proc = subprocess.run(
        # The run prints terminal glyphs. Windows pipes otherwise use a legacy
        # code page on both ends, and the parent cannot decode the output.
        [sys.executable, "-X", "utf8", "-c", _DRIVER, ",".join(HEAVY), *args],
        cwd=project, env=env, capture_output=True, encoding="utf-8", errors="replace",
        stdin=subprocess.DEVNULL, timeout=240,
    )
    line = next(x for x in proc.stderr.splitlines() if x.startswith("LOADED="))
    return proc, json.loads(line.removeprefix("LOADED="))


@pytest.fixture
def project(tmp_path: Path) -> Path:
    """A project holding one shell-only conduit."""
    conduit = tmp_path / ".atelier" / "conduits" / "hello"
    conduit.mkdir(parents=True)
    (conduit / "conduit.yaml").write_text(CONDUIT)
    return tmp_path


@pytest.mark.parametrize(
    "args",
    [
        ("--help",),
        ("show", "hello", "--json"),
        ("list",),
        ("run", "hello", "--input", "who=world"),
    ],
)
def test_everyday_commands_skip_server_and_agent_libraries(project: Path, args) -> None:
    """Verify help, show, list and a shell run never import ACP, FastAPI or uvicorn."""
    proc, loaded = _run(project, *args)
    assert proc.returncode == 0, proc.stderr
    assert loaded == []
    if args[0] == "run":
        assert "hello world" in proc.stdout

