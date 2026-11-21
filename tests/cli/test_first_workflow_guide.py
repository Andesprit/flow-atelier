"""Execute the README and first-workflow examples using local fixtures and a fake ACP adapter."""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from flow_atelier.services.executor.bash import to_bash_path
from flow_atelier.services.store.filesystem import FilesystemStore
from tests._shell import CLI as _CLI
from tests._shell import record_path, run_script, write_shim
from tests.installer import test_install_sh

installer_sandbox = test_install_sh.sandbox

_REPO_ROOT = Path(__file__).resolve().parents[2]
_GUIDE = _REPO_ROOT / "docs" / "first-workflow.md"
_BASH_BLOCK = re.compile(r"^```bash\n(.*?)^```$", re.MULTILINE | re.DOTALL)
# Every `atelier run` variant ends its transcript with this line.
_FLOW_ID_LINE = re.compile(r"^flow_id: (\S+)$", re.MULTILINE)
_FAKE = _REPO_ROOT / "tests" / "fixtures" / "fake_acp_agent.py"


def _launch(turn, *, resumed=None):
    script = {
        "turns": [{"chunks": [turn]}],
        "modes": {"current": "read-only", "available": [{"id": "read-only", "name": "Read Only"}]},
        "reload": "load",
    }
    if resumed:
        script["resumed_turns"] = [{"chunks": [resumed]}]
    # This adapter advertises its read-only mode on load as well as on creation.
    launch = """
import asyncio, sys
from functools import partial
sys.path.insert(0, sys.argv.pop(1))
import fake_acp_agent as fake
fake.LoadSessionResponse = partial(fake.LoadSessionResponse, modes={
    "currentModeId": "read-only", "availableModes": [{"id": "read-only", "name": "Read Only"}],
})
asyncio.run(fake._main())
"""
    return json.dumps([sys.executable, "-c", launch, str(_FAKE.parent), "--script", json.dumps(script)])


@pytest.fixture
def guide_env(tmp_path):
    """An isolated environment whose `atelier` is this checkout's CLI.

    :yields: ``(env, marker)`` — the child environment and the file the script
        writes its workspace path into.
    """
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    write_shim(bin_dir)
    marker = tmp_path / "workspace-path"

    env = {k: v for k, v in os.environ.items() if not k.startswith("ATELIER_")}
    env["ATELIER_GLOBAL_ATELIER_DIR"] = str(tmp_path / "global")
    env["ATELIER_NO_UPDATE_CHECK"] = "1"
    env["PATH"] = f"{bin_dir}{os.pathsep}{env.get('PATH', '')}"
    env["TUTORIAL_MARKER"] = to_bash_path(marker)
    yield env, marker
    # The guide deliberately never deletes its own workspace, and `mktemp -d`
    # puts it outside tmp_path, so the test is what cleans up after it.
    if marker.exists():
        shutil.rmtree(Path(marker.read_text().strip()).parent, ignore_errors=True)


def _log_shape(store: FilesystemStore, flow_id: str) -> list[tuple[str, bool]]:
    """Summarise a flow's saved log as ``(task, succeeded)`` in write order.

    :param store: store rooted at the tutorial workspace.
    :param flow_id: flow whose log to summarise.
    :returns: one pair per persisted task iteration.
    """
    return [(e.task, e.exit_code == 0) for e in store.read_logs(flow_id)]


def test_readme_points_at_a_guide_that_exists():
    """The discovery link beside the quickstart resolves to this file."""
    guide = (_REPO_ROOT / "docs" / "guide.md").read_text(encoding="utf-8")
    assert "(first-workflow.md)" in guide
    assert _GUIDE.is_file()


def test_the_guide_runs_end_to_end_and_records_what_it_claims(guide_env, tmp_path):
    """Execute the published commands, then verify the saved history."""
    env, marker = guide_env
    recovery = _GUIDE.read_text().split("## Try failure and recovery with Bash")[1]
    script = "set -eu\n" + "\n".join(_BASH_BLOCK.findall(recovery))
    script += record_path("tutorial_dir", "TUTORIAL_MARKER")
    run = run_script(script, tmp_path / "walkthrough.sh", tmp_path, env, timeout=240)
    assert run.returncode == 0, run.stdout + run.stderr
    assert '"ok": true' in run.stdout
    initial, resumed, fresh = _FLOW_ID_LINE.findall(run.stdout)
    workspace = Path(marker.read_text().strip())
    broken = json.loads((workspace / "broken.json").read_text())
    assert broken == {"flow_id": resumed, "status": "failed"}
    assert len({initial, resumed, fresh}) == 3
    store = FilesystemStore(workspace / ".atelier")
    assert sorted(store.list_flows("script-check")) == sorted([initial, resumed, fresh])

    # The first run stayed intact through the failure, the resume and the re-run.
    assert _log_shape(store, initial) == [
        ("prepare", True), ("syntax", True), ("execute", True)
    ]
    assert store.read_outputs(initial)["execute"] == "workflow works\n"

    # The recovered run: prepare once, the failed then the repaired syntax
    # check, and one execution — which only happened after the repair.
    assert _log_shape(store, resumed) == [
        ("prepare", True), ("syntax", False), ("syntax", True), ("execute", True)
    ]
    broken = store.read_logs(resumed)[1]
    assert "demo.sh" in broken.stderr, broken.stderr
    assert store.read_progress(resumed).status.value == "completed"
    assert store.read_outputs(resumed)["execute"] == "workflow works\n"

    # The fresh run did everything again, including the preparation.
    assert _log_shape(store, fresh) == [
        ("prepare", True), ("syntax", True), ("execute", True)
    ]
    assert json.loads(
        subprocess.run(
            [sys.executable, "-c", _CLI, "outputs", fresh, "--json"],
            cwd=workspace, env=env, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=60,
        ).stdout
    )["execute"] == "workflow works\n"

    # Three flows, three preparations: the counter the guide tells readers to
    # watch, and the proof the resume did not redo completed work.
    assert (workspace / "preparation.log").read_text() == "prepared\n" * 3


@pytest.mark.parametrize("action", ["answer", "cancel"])
def test_documented_pending_ask(guide_env, tmp_path, action):
    env, _ = guide_env
    env["ATELIER_CODEX_LAUNCH_CMD"] = _launch("Which caller?", resumed="CLI. [ATELIER_DONE]")
    text = _GUIDE.read_text().split("### Call from a script")[1].split("### Save")[0]
    start, answer, cancel = _BASH_BLOCK.findall(text)
    run = run_script(
        "set -eu\n" + start + (answer if action == "answer" else cancel),
        tmp_path / "ask.sh", tmp_path, env, timeout=60,
    )
    assert run.returncode == 0, run.stderr
    pending, result = map(json.loads, run.stdout.splitlines())
    assert pending == json.loads((tmp_path / "turn.json").read_text())
    assert pending["status"] == "needs_input" and pending["question"] == "Which caller?"
    assert result["flow_id"] == pending["flow_id"]
    if action == "answer":
        assert result["status"] == "completed" and result["answer"] == "CLI."
    else:
        assert result["status"] == "stopped"


def test_one_step_run_and_log_stream(guide_env, tmp_path):
    env, _ = guide_env
    env["ATELIER_CODEX_LAUNCH_CMD"] = _launch("No finding.")
    text = _GUIDE.read_text().split("### Save the assignment")[1].split("## Try")[0]
    compose, missing_brief, schedule = _BASH_BLOCK.findall(text)
    run = run_script("set -eu\n" + compose, tmp_path / "compose.sh", tmp_path, env, timeout=60)
    assert run.returncode == 0, run.stderr
    completion = json.loads((tmp_path / "run.json").read_text())
    assert completion["status"] == "completed"
    records = [json.loads(line) for line in run.stdout.splitlines() if line.startswith("{")]
    assert records[-1] == {"kind": "end", **completion}
    assert any(row.get("output") == "No finding." for row in records)
    store = FilesystemStore(tmp_path / ".atelier")
    assert store.read_outputs(completion["flow_id"]) == {"step_1": "No finding."}
    rejected = run_script(missing_brief, tmp_path / "reject.sh", tmp_path, env, 30)
    assert rejected.returncode == 0, rejected.stderr
    assert (tmp_path / "result.json").read_bytes() == b""
    assert json.loads(rejected.stdout)["error"]["code"] == "missing_inputs"

    registered = run_script("set -eu\n" + schedule, tmp_path / "schedule.sh", tmp_path, env, 30)
    assert registered.returncode == 0, registered.stdout + registered.stderr
    listing = json.loads(registered.stdout.split("\n", 1)[1])
    job, = listing["schedules"]
    assert job["name"] == "weekly-commit-review"
    assert job["conduit"] == "commit-review" and Path(job["working_dir"]).samefile(tmp_path)
    assert (Path(env["ATELIER_GLOBAL_ATELIER_DIR"]) / "schedules").is_dir()


def test_readme_agent_and_shell_examples(guide_env, tmp_path):
    env, _ = guide_env
    # init discovers binaries on PATH. Both known agents launch through fake ACP.
    bin_dir = tmp_path / "bin"
    for name in ("claude", "codex"):
        launcher = bin_dir / (name + ".cmd" if os.name == "nt" else name)
        launcher.write_text("exit 1\n")
        launcher.chmod(0o755)
    env["ATELIER_CLAUDE_LAUNCH_CMD"] = env["ATELIER_CODEX_LAUNCH_CMD"] = _launch(
        "No finding. [ATELIER_DONE]"
    )
    _install, hello, ask, _serve, pair = _BASH_BLOCK.findall((_REPO_ROOT / "README.md").read_text())
    first_ask, = _BASH_BLOCK.findall(_GUIDE.read_text().split("### Call from a script")[0])
    assert ask == first_ask
    run = run_script("set -eu\n" + hello + ask + pair, tmp_path / "start.sh", tmp_path, env, 120)
    assert run.returncode == 0, run.stdout + run.stderr
    assert "hello world" in run.stdout
    store = FilesystemStore(tmp_path / ".atelier")
    for name in ("hello", "ask", "pair"):
        flow_id, = store.list_flows(name)
        assert store.read_progress(flow_id).status.value == "completed"


def test_readme_dashboard_command_disables_scheduler(tmp_path, monkeypatch):
    import uvicorn
    from typer.testing import CliRunner

    from flow_atelier.cli.main import app

    async def serve_without_socket(server):
        server.started = True

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(uvicorn.Server, "serve", serve_without_socket)
    serve, = [b for b in _BASH_BLOCK.findall((_REPO_ROOT / "README.md").read_text()) if "atelier serve" in b]
    run = CliRunner().invoke(app, serve.split()[1:])
    assert run.exit_code == 0 and "Scheduler disabled" in run.stdout


@pytest.mark.skipif(os.name == "nt", reason="The README installer is for Unix")
def test_readme_installer_and_current_shell_path(installer_sandbox, tmp_path):
    home, env = installer_sandbox
    env["INSTALL_SCRIPT"] = str(_REPO_ROOT / "install.sh")
    install = _BASH_BLOCK.findall((_REPO_ROOT / "README.md").read_text())[0]
    # Serve the checkout's installer to the first curl. Its own curl uses the fake release.
    script = """set -eu -o pipefail
curl() {
  [ "$1" = "-fsSL" ]
  [ "$2" = "https://raw.githubusercontent.com/Andesprit/flow-atelier/main/install.sh" ]
  cat "$INSTALL_SCRIPT"
}
""" + install + "command -v atelier\n"
    run = run_script(script, tmp_path / "readme-install.sh", tmp_path, env, 60)
    assert run.returncode == 0, run.stdout + run.stderr
    assert Path(run.stdout.splitlines()[-1]).samefile(home / ".atelier/bin/atelier")
