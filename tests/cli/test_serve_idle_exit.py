"""CLI test: `atelier serve --idle-exit` stops itself, but not mid-run.

Agents start a server in the background to show a run page. Without a way to
end, it outlives the run by days. A running flow in the directory must hold it
up; once the flow ends and nothing else uses it, it has to exit on its own.
"""
from __future__ import annotations

import asyncio
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import time
from contextlib import suppress

import httpx
import pytest
from websockets.asyncio.client import connect
from websockets.exceptions import InvalidStatus

from flow_atelier.schemas.progress import FlowStatus, Progress
from tests._shell import CLI, group_alive


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


@pytest.mark.skipif(os.name != "posix", reason="parent stdin pipe and process groups")
async def test_parent_death_stops_server_and_active_shell(tmp_path, monkeypatch):
    """Kill the pipe owner during an HTTP run and check its independent shell session."""
    monkeypatch.setenv("ATELIER_API_TOKEN", "parent-test-token")
    tools = tmp_path / "bin"
    tools.mkdir()
    for name in ("bash", "sleep"):
        (tools / name).symlink_to(shutil.which(name))
    monkeypatch.setenv("PATH", str(tools))
    monkeypatch.setenv("NO_COLOR", "1")
    monkeypatch.setenv("COLUMNS", "200")
    # The parent owns the only writer and deliberately installs no exit handler.
    parent_code = (
        "import subprocess, sys; from pathlib import Path; "
        "p = subprocess.Popen(sys.argv[1:], stdin=subprocess.PIPE, process_group=0); "
        "Path('server.pid').write_text(str(p.pid)); p.wait()"
    )
    log = tmp_path / "server.log"
    marker = tmp_path / "shell.pid"
    cli = [sys.executable, "-u", "-c", CLI]
    with log.open("w") as output:
        parent = subprocess.Popen(
            [sys.executable, "-c", parent_code, *cli,
             "serve", "--port", "0", "--exit-with-stdin", "--log-level", "warning"],
            cwd=tmp_path, stdout=output, stderr=output,
        )
    try:
        async with asyncio.timeout(20):
            while not (ready := re.search(r"running at (http://127\.0\.0\.1:\d+)", log.read_text())):
                assert parent.poll() is None, log.read_text()
                await asyncio.sleep(0.05)
        assert "No agent harness found on PATH" in log.read_text()
        async with httpx.AsyncClient(base_url=ready[1], timeout=15) as client:
            assert (await client.get("/flows")).status_code == 401
            ws_url = ready[1].replace("http://", "ws://") + "/ws/run-conduit"
            with pytest.raises(InvalidStatus):
                async with connect(ws_url):
                    pass
            async with connect(ws_url + "?token=parent-test-token") as ws:
                await ws.send("{}")
                assert '"error"' in await ws.recv()
            client.headers["Authorization"] = "Bearer parent-test-token"
            assert (await client.get("/flows")).status_code == 200
            run = asyncio.create_task(client.post("/tasks/run", json={
                "name": "sleep", "tool": "tool:bash", "run_path": str(tmp_path),
                "task": 'test -z "${ATELIER_API_TOKEN:-}" && echo $$ > shell.pid && exec sleep 90',
            }))
            try:
                async with asyncio.timeout(8):
                    while not marker.exists():
                        if run.done():
                            pytest.fail(run.result().text)
                        await asyncio.sleep(0.05)
                parent.kill()
                parent.wait(5)
                async with asyncio.timeout(12):
                    shell_pid = int(marker.read_text())
                    server_pid = int((tmp_path / "server.pid").read_text())
                    while group_alive(shell_pid) or group_alive(server_pid):
                        await asyncio.sleep(0.05)
                with pytest.raises(httpx.ConnectError):
                    await client.get("/flows")
            finally:
                run.cancel()
                await asyncio.gather(run, return_exceptions=True)
    finally:
        parent.kill()
        parent.wait()
        for pid_file in (marker, tmp_path / "server.pid"):
            if pid_file.exists():
                with suppress(ProcessLookupError):
                    os.killpg(int(pid_file.read_text()), signal.SIGKILL)
