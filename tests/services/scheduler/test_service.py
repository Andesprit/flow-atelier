"""Start-at-login scheduler service: launchd and systemd definitions and status."""
from __future__ import annotations

import os
import plistlib
import subprocess
import sys
import time
import uuid
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from flow_atelier.services.scheduler.service import (
    LAUNCHD_LABEL,
    SYSTEMD_UNIT,
    TASK_NAME,
    Launchd,
    ServiceError,
    Systemd,
    TaskScheduler,
    daemon_argv,
    scheduler_service,
    service_environment,
)
from flow_atelier.services.scheduler.store import SchedulerAlreadyRunning, ScheduleStore


class FakeManager:
    """Record service manager commands and answer them from a script."""

    def __init__(self, answers: dict[tuple[str, ...], list[tuple[int, str]]] | None = None):
        self.calls: list[list[str]] = []
        self.answers = answers or {}

    def __call__(self, argv):
        self.calls.append(list(argv))
        replies = self.answers.get(tuple(argv), [])
        code, out = replies.pop(0) if replies else (0, "")
        return subprocess.CompletedProcess(argv, code, stdout=out, stderr=out if code else "")


ARGV = ["/opt/atelier venv/bin/python", "-m", "flow_atelier.main", "schedule", "daemon", "--wait-for-lease"]
TARGET = f"gui/501/{LAUNCHD_LABEL}"


def test_daemon_argv_uses_this_interpreter_and_names_the_store(monkeypatch, tmp_path):
    store = ["--global-atelier-dir", str((tmp_path / "store").resolve())]
    monkeypatch.delattr(sys, "frozen", raising=False)
    assert daemon_argv(tmp_path / "store") == [sys.executable, "-m", "flow_atelier.main", *ARGV[3:], *store]
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    assert daemon_argv(tmp_path / "store") == [sys.executable, *ARGV[3:], *store]


def test_environment_keeps_only_path():
    assert service_environment({"PATH": "/a:/b", "SECRET_TOKEN": "x"}) == {"PATH": "/a:/b"}
    assert service_environment({}) == {}


def test_launchd_install_writes_agent_and_reloads_it(tmp_path):
    manager = FakeManager({
        ("launchctl", "bootout", TARGET): [(0, "")],
        ("launchctl", "print", TARGET): [(0, "state = running"), (113, "not found")],
    })
    agent = Launchd(tmp_path, manager, uid=501)
    store = tmp_path / "store"
    agent.install(ARGV, {"PATH": "/a:/b"}, store)

    plist = plistlib.loads(agent.definition.read_bytes())
    assert agent.definition == tmp_path / "Library/LaunchAgents" / f"{LAUNCHD_LABEL}.plist"
    assert plist["Label"] == LAUNCHD_LABEL
    assert plist["ProgramArguments"] == ARGV
    assert plist["EnvironmentVariables"] == {"PATH": "/a:/b"}
    assert plist["WorkingDirectory"] == str(tmp_path)
    assert plist["RunAtLoad"] is True
    assert plist["KeepAlive"] == {"SuccessfulExit": False}
    assert plist["StandardOutPath"] == plist["StandardErrorPath"] == str(store.resolve() / "scheduler.log")
    assert store.is_dir()
    # The old job is booted out and gone before the new one is bootstrapped.
    assert manager.calls == [
        ["launchctl", "bootout", TARGET],
        ["launchctl", "print", TARGET],
        ["launchctl", "print", TARGET],
        ["launchctl", "bootstrap", "gui/501", str(agent.definition)],
    ]


def test_launchd_install_reports_a_refused_bootstrap(tmp_path):
    manager = FakeManager({
        ("launchctl", "bootout", TARGET): [(3, "No such process")],
        ("launchctl", "bootstrap", "gui/501", str(tmp_path / "Library/LaunchAgents" / f"{LAUNCHD_LABEL}.plist")):
            [(5, "Bootstrap failed: 5: Input/output error")],
    })
    with pytest.raises(ServiceError, match="Input/output error"):
        Launchd(tmp_path, manager, uid=501).install(ARGV, {}, tmp_path / "store")


def test_launchd_status_reads_the_job_state_not_nested_ones(tmp_path):
    output = "\n".join([
        f"gui/501/{LAUNCHD_LABEL} = {{",
        "\tstate = running",
        "\tpid = 4242",
        "\tendpoints = {",
        "\t\tstate = active",
        "\t}",
        "}",
    ])
    agent = Launchd(tmp_path, FakeManager({("launchctl", "print", TARGET): [(0, output)]}), uid=501)
    agent.definition.parent.mkdir(parents=True)
    agent.definition.write_bytes(b"")
    state = agent.status()
    assert (state.installed, state.running, state.pid) == (True, True, 4242)

    waiting = Launchd(tmp_path, FakeManager({("launchctl", "print", TARGET): [(0, "\tstate = waiting")]}), uid=501)
    assert (waiting.status().running, waiting.status().pid) == (False, None)


def test_launchd_status_when_not_loaded(tmp_path):
    agent = Launchd(tmp_path, FakeManager({("launchctl", "print", TARGET): [(113, "Could not find service")]}), uid=501)
    state = agent.status()
    assert (state.installed, state.running, state.pid) == (False, False, None)


def test_launchd_uninstall_is_idempotent(tmp_path):
    manager = FakeManager({
        ("launchctl", "bootout", TARGET): [(0, ""), (3, "No such process")],
        ("launchctl", "print", TARGET): [(113, "not found")],
    })
    agent = Launchd(tmp_path, manager, uid=501)
    agent.definition.parent.mkdir(parents=True)
    agent.definition.write_bytes(b"")
    assert agent.uninstall() is True
    assert not agent.definition.exists()
    assert agent.uninstall() is False


def test_systemd_unit_quotes_arguments_and_environment(tmp_path):
    manager = FakeManager()
    unit = Systemd(tmp_path / "config", manager)
    unit.install([*ARGV, "100%$HOME"], {"PATH": "/a b:/c", "ATELIER_GLOBAL_ATELIER_DIR": "/s"}, tmp_path)

    text = unit.definition.read_text()
    assert unit.definition == tmp_path / "config/systemd/user" / SYSTEMD_UNIT
    assert (
        'ExecStart="/opt/atelier venv/bin/python" "-m" "flow_atelier.main" "schedule" '
        '"daemon" "--wait-for-lease" "100%%$$HOME"'
    ) in text
    assert 'Environment="PATH=/a b:/c"' in text
    assert 'Environment="ATELIER_GLOBAL_ATELIER_DIR=/s"' in text
    assert "WorkingDirectory=%h" in text
    assert "Restart=on-failure" in text
    assert "WantedBy=default.target" in text
    assert manager.calls == [
        ["systemctl", "--user", "daemon-reload"],
        ["systemctl", "--user", "enable", SYSTEMD_UNIT],
        ["systemctl", "--user", "restart", SYSTEMD_UNIT],
    ]


def test_systemd_install_reports_a_missing_user_manager(tmp_path):
    manager = FakeManager({
        ("systemctl", "--user", "daemon-reload"): [(1, "Failed to connect to bus: No medium found")],
    })
    with pytest.raises(ServiceError, match="Failed to connect to bus"):
        Systemd(tmp_path, manager).install(ARGV, {}, tmp_path)


def test_systemd_status_and_uninstall(tmp_path):
    show = ("systemctl", "--user", "show", SYSTEMD_UNIT, "--property=ActiveState,MainPID")
    manager = FakeManager({show: [(0, "ActiveState=active\nMainPID=77"), (0, "ActiveState=inactive\nMainPID=0")]})
    unit = Systemd(tmp_path, manager)
    unit.definition.parent.mkdir(parents=True)
    unit.definition.write_text("")
    state = unit.status()
    assert (state.installed, state.running, state.pid) == (True, True, 77)
    assert unit.uninstall() is True
    assert not unit.definition.exists()
    assert ["systemctl", "--user", "disable", "--now", SYSTEMD_UNIT] in manager.calls
    state = unit.status()
    assert (state.installed, state.running, state.pid) == (False, False, None)
    assert unit.uninstall() is False


def test_platform_selection(tmp_path):
    assert isinstance(scheduler_service("darwin", {}, tmp_path), Launchd)
    linux = scheduler_service("linux", {"XDG_CONFIG_HOME": str(tmp_path / "xdg")}, tmp_path)
    assert isinstance(linux, Systemd)
    assert linux.definition == tmp_path / "xdg/systemd/user" / SYSTEMD_UNIT
    assert scheduler_service("linux", {}, tmp_path).definition == tmp_path / ".config/systemd/user" / SYSTEMD_UNIT
    windows = scheduler_service("win32", {}, tmp_path)
    assert isinstance(windows, TaskScheduler)
    assert windows.definition == TASK_NAME
    with pytest.raises(ServiceError, match="macOS, Linux and Windows"):
        scheduler_service("freebsd14", {}, tmp_path)


WINDOWS_ENV = {"USERNAME": "ana", "USERDOMAIN": "DESK", "SystemRoot": r"C:\Windows"}
NS = {"t": "http://schemas.microsoft.com/windows/2004/02/mit/task"}


def test_task_xml_runs_the_daemon_headless_at_this_users_log_on(tmp_path):
    task = TaskScheduler(tmp_path / "home", WINDOWS_ENV, FakeManager())
    argv = [r"C:\Users\ana\.atelier\bin\atelier.exe", "schedule", "daemon", "--wait-for-lease",
            "--global-atelier-dir", r"C:\Users\ana\My Store & co"]
    root = ET.fromstring(task.task_xml(argv, tmp_path / "store"))

    assert root.findtext("t:Triggers/t:LogonTrigger/t:UserId", namespaces=NS) == r"DESK\ana"
    assert root.findtext("t:Principals/t:Principal/t:UserId", namespaces=NS) == r"DESK\ana"
    assert root.findtext("t:Principals/t:Principal/t:LogonType", namespaces=NS) == "InteractiveToken"
    assert root.findtext("t:Principals/t:Principal/t:RunLevel", namespaces=NS) == "LeastPrivilege"
    assert root.findtext("t:Settings/t:ExecutionTimeLimit", namespaces=NS) == "PT0S"
    assert root.findtext("t:Settings/t:DisallowStartIfOnBatteries", namespaces=NS) == "false"
    assert root.findtext("t:Actions/t:Exec/t:Command", namespaces=NS) == r"C:\Windows\System32\conhost.exe"
    assert root.findtext("t:Actions/t:Exec/t:WorkingDirectory", namespaces=NS) == str(tmp_path / "home")
    log = str((tmp_path / "store").resolve() / "scheduler.log")
    assert root.findtext("t:Actions/t:Exec/t:Arguments", namespaces=NS) == (
        "--headless " + subprocess.list2cmdline([*argv, "--log-file", log])
    )


def test_task_install_registers_from_utf16_xml_and_starts_it(tmp_path):
    seen = {}

    class Manager(FakeManager):
        def __call__(self, argv):
            if argv[1] == "/Create":
                xml = Path(argv[argv.index("/XML") + 1])
                seen["xml"] = xml.read_bytes()
            return super().__call__(argv)

    manager = Manager({("schtasks", "/End", "/TN", TASK_NAME): [(1, "not running")]})
    task = TaskScheduler(tmp_path, WINDOWS_ENV, manager)
    task.install(ARGV, {"PATH": "ignored"}, tmp_path / "store")

    assert seen["xml"].startswith(b"\xff\xfe")
    ET.fromstring(seen["xml"].decode("utf-16"))
    assert [c[:4] for c in manager.calls] == [
        ["schtasks", "/End", "/TN", TASK_NAME],
        ["schtasks", "/Create", "/TN", TASK_NAME],
        ["schtasks", "/Run", "/TN", TASK_NAME],
    ]
    assert manager.calls[1][-1] == "/F"
    assert not Path(manager.calls[1][5]).exists()
    assert (tmp_path / "store").is_dir()


def test_task_install_reports_a_refused_registration(tmp_path):
    class Manager(FakeManager):
        def __call__(self, argv):
            self.calls.append(list(argv))
            code = 1 if argv[1] == "/Create" else 0
            return subprocess.CompletedProcess(argv, code, stdout="", stderr="ERROR: Access is denied." if code else "")

    with pytest.raises(ServiceError, match="Access is denied"):
        TaskScheduler(tmp_path, WINDOWS_ENV, Manager()).install(ARGV, {}, tmp_path / "store")


def test_task_status_and_uninstall(tmp_path):
    query = ("schtasks", "/Query", "/TN", TASK_NAME)
    state = ("powershell", "-NoProfile", "-NonInteractive", "-Command",
             f"(Get-ScheduledTask -TaskName '{TASK_NAME}').State")
    manager = FakeManager({
        query: [(0, ""), (0, ""), (0, ""), (1, "ERROR: cannot find"), (1, "")],
        state: [(0, "Running\r\n"), (0, "Ready\r\n")],
    })
    task = TaskScheduler(tmp_path, WINDOWS_ENV, manager)
    running = task.status()
    assert (running.definition, running.installed, running.running, running.pid) == (TASK_NAME, True, True, None)
    ready = task.status()
    assert (ready.installed, ready.running) == (True, False)
    assert task.uninstall() is True
    assert ["schtasks", "/End", "/TN", TASK_NAME] in manager.calls
    assert ["schtasks", "/Delete", "/TN", TASK_NAME, "/F"] in manager.calls
    gone = task.status()
    assert (gone.installed, gone.running) == (False, False)
    assert task.uninstall() is False


@pytest.mark.skipif(
    os.name != "nt" or not os.environ.get("CI"),
    reason="registers a real Task Scheduler task; runs on Windows CI only",
)
def test_task_scheduler_runs_the_daemon_and_stops_it(tmp_path):
    store = tmp_path / "store"
    task = TaskScheduler(Path.home(), os.environ, name=f"{TASK_NAME} test {uuid.uuid4().hex[:8]}")
    log = store / "scheduler.log"
    try:
        task.install(daemon_argv(store), {}, store)
        deadline = time.monotonic() + 90
        while "scheduler running" not in (log.read_text(encoding="utf-8") if log.exists() else ""):
            assert time.monotonic() < deadline, log.read_text(encoding="utf-8") if log.exists() else "no log"
            time.sleep(0.5)
        with pytest.raises(SchedulerAlreadyRunning):
            ScheduleStore(store).acquire_daemon_lease()
        assert task.status().running
        assert task.uninstall() is True
        deadline = time.monotonic() + 30
        while True:
            try:
                ScheduleStore(store).acquire_daemon_lease().close()
                break
            except SchedulerAlreadyRunning as exc:
                assert time.monotonic() < deadline, f"daemon pid {exc.pid} outlived its task"
                time.sleep(0.5)
    finally:
        task.uninstall()


def test_missing_service_manager_is_a_service_error():
    from flow_atelier.services.scheduler.service import _run

    with pytest.raises(ServiceError, match="not found"):
        _run([str(Path("/nonexistent/systemctl"))])
