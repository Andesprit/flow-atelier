"""CLI test: `atelier serve --idle-exit` stops itself, but not mid-run.

Agents start a server in the background to show a run page. Without a way to
end, it outlives the run by days. A running flow in the directory must hold it
up; once the flow ends and nothing else uses it, it has to exit on its own.
"""
from __future__ import annotations

import os
import socket
import subprocess
import sys
import time

from flow_atelier.schemas.progress import FlowStatus, Progress
from tests._shell import CLI


def test_serve_waits_for_a_running_flow_then_stops_when_idle(tmp_path) -> None:
    """Verify the server stays up while a flow runs and exits once it is idle.

    :param tmp_path: pytest temp directory, used as cwd and home.
    """
    flow_dir = tmp_path / ".atelier" / "flows" / "20260927_0eb21391_demo"
    flow_dir.mkdir(parents=True)
    progress = flow_dir / "progress.json"
    # This test process stands in for the flow's live runner.
    running = Progress(runner_pid=os.getpid(), runner_host=socket.gethostname())
    progress.write_text(running.model_dump_json())

    env = {
        **os.environ,
        "HOME": str(tmp_path),
        "USERPROFILE": str(tmp_path),
        "ATELIER_NO_UPDATE_CHECK": "1",
    }
    env.pop("ATELIER_API_TOKEN", None)
    # 0.01 minutes = 0.6 s of idleness.
    proc = subprocess.Popen(
        [sys.executable, "-c", CLI, "serve", "--port", "0", "--idle-exit", "0.01"],
        cwd=tmp_path,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    try:
        seen = []
        for line in proc.stdout:
            seen.append(line)
            if "running at" in line:
                break
        assert "stops after 0.01 idle minutes" in "".join(seen), seen

        time.sleep(3)  # five times the idle limit
        assert proc.poll() is None, "server stopped while a flow was running"

        progress.write_text(
            running.model_copy(update={"status": FlowStatus.completed}).model_dump_json()
        )
        out, _ = proc.communicate(timeout=20)
    finally:
        proc.kill()
    assert proc.returncode == 0, out
    assert "idle for 0.01 minutes, stopping" in out
