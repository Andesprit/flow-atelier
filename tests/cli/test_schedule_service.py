"""`atelier schedule service` and the daemon's --wait-for-lease mode."""
from __future__ import annotations

import asyncio
import os
import plistlib
import signal
import subprocess
import sys

from typer.testing import CliRunner

from flow_atelier.cli import app
from flow_atelier.cli.commands.scheduler import _lease_owner
from flow_atelier.services.scheduler import SchedulerAlreadyRunning, ScheduleStore
from flow_atelier.services.scheduler.service import Launchd, ServiceError, daemon_argv
from tests._shell import CLI


async def test_waiting_daemon_takes_over_when_the_lease_is_released(tmp_path, monkeypatch):
    store = ScheduleStore(tmp_path / "global")
    store.atelier_dir.mkdir(parents=True)
    monkeypatch.setenv("ATELIER_GLOBAL_ATELIER_DIR", str(store.atelier_dir))
    monkeypatch.setenv("NO_COLOR", "1")
    monkeypatch.chdir(tmp_path)
    lease = store.acquire_daemon_lease()
    proc = await asyncio.create_subprocess_exec(
        sys.executable, "-u", "-c", CLI, "schedule", "daemon", "--wait-for-lease",
        "--reload-interval", "0.2",
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
    )

    async def read_until(text: str) -> str:
        seen = ""
        while text not in seen:
            line = (await proc.stdout.readline()).decode()
            assert line, f"daemon exited before printing {text!r}:\n{seen}"
            seen += line
        return seen

    try:
        async with asyncio.timeout(20):
            waiting = await read_until("waiting to take over")
            assert f"scheduler already running (pid {os.getpid()})" in waiting
            await asyncio.sleep(0.5)
            assert proc.returncode is None
            lease.close()
            await read_until("scheduler running")
            # SIGINT stops it cleanly before and after the daemon installs its handlers.
            if os.name == "nt":
                proc.kill()
            else:
                proc.send_signal(signal.SIGINT)
            await proc.wait()
    finally:
        lease.close()
        if proc.returncode is None:
            proc.kill()
            await proc.wait()
    if os.name != "nt":
        assert proc.returncode == 0


class FakeLaunchctl:
    """A launchd that loads on bootstrap and unloads on bootout."""

    loaded = False

    def __call__(self, argv):
        code, out = 0, ""
        if argv[1] == "bootstrap":
            self.loaded = True
        elif argv[1] == "bootout":
            code, self.loaded = (0 if self.loaded else 3), False
        elif argv[1] == "print":
            code, out = (0, "\tstate = running\n\tpid = 99\n") if self.loaded else (113, "")
        return subprocess.CompletedProcess(argv, code, stdout=out, stderr="")


def test_install_status_and_uninstall(tmp_path, monkeypatch):
    store = tmp_path / "global"
    monkeypatch.setenv("ATELIER_GLOBAL_ATELIER_DIR", str(store))
    monkeypatch.setenv("PATH", "/agents/bin:/usr/bin")
    agent = Launchd(tmp_path / "home", FakeLaunchctl(), uid=501)
    monkeypatch.setattr("flow_atelier.cli.commands.scheduler.scheduler_service", lambda: agent)
    runner = CliRunner()

    installed = runner.invoke(app, ["schedule", "service", "install"])
    assert installed.exit_code == 0, installed.output
    plist = plistlib.loads(agent.definition.read_bytes())
    assert plist["ProgramArguments"] == daemon_argv(store)
    assert plist["EnvironmentVariables"] == {"PATH": "/agents/bin:/usr/bin"}
    assert "Scheduler service: running (pid 99)" in installed.stdout
    assert f"Logs: {store.resolve() / 'scheduler.log'}" in installed.stdout
    assert "Run this command again after you change PATH" in installed.stdout

    status = runner.invoke(app, ["schedule", "service", "status"])
    assert status.exit_code == 0
    assert "Scheduler service: running (pid 99)" in status.stdout
    assert f"Definition: {agent.definition}" in status.stdout

    removed = runner.invoke(app, ["schedule", "service", "uninstall"])
    assert removed.exit_code == 0
    assert "Scheduler service removed" in removed.stdout
    assert not agent.definition.exists()
    assert "not installed" in runner.invoke(app, ["schedule", "service", "status"]).stdout
    assert "was not installed" in runner.invoke(app, ["schedule", "service", "uninstall"]).stdout


async def test_service_daemon_watches_the_named_store_and_logs_to_a_file(tmp_path, monkeypatch):
    """The service command line alone picks the store; output lands in the log file."""
    configured = tmp_path / "configured"
    named = ScheduleStore(tmp_path / "named")
    log = tmp_path / "logs" / "scheduler.log"
    monkeypatch.setenv("ATELIER_GLOBAL_ATELIER_DIR", str(configured))
    monkeypatch.setenv("NO_COLOR", "1")
    monkeypatch.chdir(tmp_path)
    proc = await asyncio.create_subprocess_exec(
        sys.executable, "-u", "-c", CLI, "schedule", "daemon", "--wait-for-lease",
        "--global-atelier-dir", str(named.atelier_dir), "--log-file", str(log),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
    )
    try:
        async with asyncio.timeout(20):
            while "scheduler running" not in (log.read_text(encoding="utf-8") if log.exists() else ""):
                assert proc.returncode is None, await proc.stdout.read()
                await asyncio.sleep(0.05)
        try:
            named.acquire_daemon_lease().close()
            raise AssertionError("daemon does not hold the named store's lease")
        except SchedulerAlreadyRunning as exc:
            assert exc.pid == proc.pid
        assert not configured.exists()
    finally:
        if os.name == "nt":
            proc.kill()
        else:
            proc.send_signal(signal.SIGINT)
        async with asyncio.timeout(10):
            out = await proc.stdout.read()
            await proc.wait()
    assert out == b""


def test_lease_owner_names_a_scheduler_that_outlives_the_service(tmp_path):
    store = ScheduleStore(tmp_path / "store")
    assert _lease_owner(store, wait=0) is None
    store.atelier_dir.mkdir()
    assert _lease_owner(store, wait=0) is None
    with store.acquire_daemon_lease():
        assert _lease_owner(store, wait=0) == os.getpid()
    assert _lease_owner(store, wait=0) is None


def test_unsupported_platform_fails_with_guidance(monkeypatch):
    def unsupported():
        raise ServiceError("supported on macOS and Linux")

    monkeypatch.setattr("flow_atelier.cli.commands.scheduler.scheduler_service", unsupported)
    for command in ("install", "status", "uninstall"):
        result = CliRunner().invoke(app, ["schedule", "service", command])
        assert result.exit_code == 1
        assert "supported on macOS and Linux" in result.stderr
