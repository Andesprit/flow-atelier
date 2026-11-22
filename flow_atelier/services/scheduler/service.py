"""Start the scheduler daemon at login with the operating system's service manager.

macOS gets a launchd user agent, Linux a systemd user unit and Windows a Task
Scheduler task. The daemon command names the schedule store it watches. Login
services on macOS and Linux do not read shell profiles, so their definitions
also record the ``PATH`` of the shell that installed them. Windows tasks get
the user's environment from the registry. The daemon runs with
``--wait-for-lease``: while ``atelier serve`` or another daemon dispatches
jobs it waits, then takes over when that process stops.
"""
from __future__ import annotations

import os
import plistlib
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path, PureWindowsPath
from xml.sax.saxutils import escape

LAUNCHD_LABEL = "com.andesprit.atelier.scheduler"
SYSTEMD_UNIT = "atelier-scheduler.service"
TASK_NAME = "Flow Atelier Scheduler"

Runner = Callable[[Sequence[str]], subprocess.CompletedProcess[str]]


class ServiceError(RuntimeError):
    """The platform has no supported service manager, or it refused a command."""


@dataclass(frozen=True)
class ServiceStatus:
    """What the service manager reports for the scheduler service.

    :param definition: path of the launchd plist or systemd unit, or the
        Windows task name.
    :param installed: whether the service manager has the definition.
    :param running: whether the service manager reports a live process.
    :param pid: the daemon's process ID while it runs, where reported.
    """

    definition: Path | str
    installed: bool
    running: bool
    pid: int | None = None


def _run(argv: Sequence[str]) -> subprocess.CompletedProcess[str]:
    """Run a service manager command and capture its output without raising."""
    try:
        # Windows tools print localized text in the console code page.
        return subprocess.run(
            list(argv), capture_output=True, text=True, errors="replace", check=False
        )
    except FileNotFoundError as exc:
        raise ServiceError(
            f"{argv[0]} not found; this system has no supported service manager"
        ) from exc


def _check(run: Runner, argv: Sequence[str]) -> subprocess.CompletedProcess[str]:
    """Run ``argv`` and raise :class:`ServiceError` with its output when it fails."""
    result = run(argv)
    if result.returncode != 0:
        output = (result.stderr or result.stdout or "").strip()
        raise ServiceError(f"{' '.join(argv)} failed: {output or f'exit {result.returncode}'}")
    return result


def daemon_argv(store_dir: Path) -> list[str]:
    """Return the command that starts this installation's scheduler daemon.

    :param store_dir: the global Atelier directory the daemon must watch.
    """
    launcher = (
        [sys.executable] if getattr(sys, "frozen", False)
        else [sys.executable, "-m", "flow_atelier.main"]
    )
    return [
        *launcher, "schedule", "daemon", "--wait-for-lease",
        "--global-atelier-dir", str(store_dir.resolve()),
    ]


def service_environment(environ: Mapping[str, str]) -> dict[str, str]:
    """Return the variables a macOS or Linux service needs from the installing shell."""
    return {"PATH": environ["PATH"]} if environ.get("PATH") else {}


def _log_path(store_dir: Path) -> Path:
    """Return the file that receives the daemon's output on macOS and Windows."""
    return store_dir.resolve() / "scheduler.log"


class Launchd:
    """A launchd agent in ``~/Library/LaunchAgents`` that starts at login."""

    install_note = "PATH saved from this shell. Run this command again after you change PATH."

    def __init__(self, home: Path, run: Runner = _run, uid: int | None = None) -> None:
        self.home = home
        self.definition = home / "Library" / "LaunchAgents" / f"{LAUNCHD_LABEL}.plist"
        self._run = run
        self._domain = f"gui/{os.getuid() if uid is None else uid}"
        self._target = f"{self._domain}/{LAUNCHD_LABEL}"

    def log_hint(self, store_dir: Path) -> str:
        """Describe where the daemon's output goes."""
        return str(_log_path(store_dir))

    def install(self, argv: Sequence[str], env: Mapping[str, str], store_dir: Path) -> None:
        """Write the agent, then load it so it starts now and at every login."""
        log = _log_path(store_dir)
        log.parent.mkdir(parents=True, exist_ok=True)
        self.definition.parent.mkdir(parents=True, exist_ok=True)
        self.definition.write_bytes(plistlib.dumps({
            "Label": LAUNCHD_LABEL,
            "ProgramArguments": list(argv),
            "EnvironmentVariables": dict(env),
            "WorkingDirectory": str(self.home),
            "RunAtLoad": True,
            "KeepAlive": {"SuccessfulExit": False},
            "StandardOutPath": str(log),
            "StandardErrorPath": str(log),
        }))
        self._unload()
        _check(self._run, ["launchctl", "bootstrap", self._domain, str(self.definition)])

    def uninstall(self) -> bool:
        """Stop and remove the agent. Return whether it was installed."""
        installed = self.definition.exists()
        self._unload()
        self.definition.unlink(missing_ok=True)
        return installed

    def status(self) -> ServiceStatus:
        """Read the agent's state from ``launchctl print``."""
        result = self._run(["launchctl", "print", self._target])
        props: dict[str, str] = {}
        if result.returncode == 0:
            # Nested sections repeat keys such as "state"; the job's own come first.
            for line in result.stdout.splitlines():
                key, sep, value = line.strip().partition(" = ")
                if sep:
                    props.setdefault(key, value)
        running = props.get("state") == "running"
        pid = int(props["pid"]) if running and props.get("pid", "").isdigit() else None
        return ServiceStatus(self.definition, self.definition.exists(), running, pid)

    def _unload(self) -> None:
        """Boot out a loaded agent and wait until launchd has released its label."""
        if self._run(["launchctl", "bootout", self._target]).returncode != 0:
            return
        # bootout returns before launchd forgets the job; bootstrap fails until it does.
        deadline = time.monotonic() + 10
        while self._run(["launchctl", "print", self._target]).returncode == 0:
            if time.monotonic() > deadline:
                raise ServiceError(f"launchd did not unload {LAUNCHD_LABEL} within 10 seconds")
            time.sleep(0.1)


def _systemd_quote(value: str, *, exec_line: bool = False) -> str:
    """Quote one systemd value. Specifiers use ``%``; command lines also expand ``$``."""
    quoted = value.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%")
    if exec_line:
        quoted = quoted.replace("$", "$$")
    return f'"{quoted}"'


class Systemd:
    """A systemd user unit that starts with the user's session."""

    install_note = Launchd.install_note

    def __init__(self, config_home: Path, run: Runner = _run) -> None:
        self.definition = config_home / "systemd" / "user" / SYSTEMD_UNIT
        self._run = run

    def log_hint(self, store_dir: Path) -> str:
        """Describe where the daemon's output goes."""
        return f"journalctl --user -u {SYSTEMD_UNIT}"

    def install(self, argv: Sequence[str], env: Mapping[str, str], store_dir: Path) -> None:
        """Write the unit, enable it for future sessions and (re)start it now."""
        self.definition.parent.mkdir(parents=True, exist_ok=True)
        lines = [
            "[Unit]",
            "Description=Flow Atelier scheduler",
            "",
            "[Service]",
            "ExecStart=" + " ".join(_systemd_quote(arg, exec_line=True) for arg in argv),
            "WorkingDirectory=%h",
            *(f"Environment={_systemd_quote(f'{key}={value}')}" for key, value in env.items()),
            "Restart=on-failure",
            "RestartSec=10",
            "",
            "[Install]",
            "WantedBy=default.target",
            "",
        ]
        self.definition.write_text("\n".join(lines), encoding="utf-8")
        _check(self._run, ["systemctl", "--user", "daemon-reload"])
        _check(self._run, ["systemctl", "--user", "enable", SYSTEMD_UNIT])
        _check(self._run, ["systemctl", "--user", "restart", SYSTEMD_UNIT])

    def uninstall(self) -> bool:
        """Stop, disable and remove the unit. Return whether it was installed."""
        installed = self.definition.exists()
        if installed:
            self._run(["systemctl", "--user", "disable", "--now", SYSTEMD_UNIT])
            self.definition.unlink()
            _check(self._run, ["systemctl", "--user", "daemon-reload"])
        return installed

    def status(self) -> ServiceStatus:
        """Read the unit's state from ``systemctl show``."""
        result = self._run([
            "systemctl", "--user", "show", SYSTEMD_UNIT, "--property=ActiveState,MainPID",
        ])
        props = dict(line.partition("=")[::2] for line in result.stdout.splitlines())
        running = result.returncode == 0 and props.get("ActiveState") == "active"
        pid = int(props["MainPID"]) if running and props.get("MainPID", "0").isdigit() else None
        return ServiceStatus(self.definition, self.definition.exists(), running, pid or None)


class TaskScheduler:
    """A Task Scheduler task that starts at log on, inside a headless console.

    The console host's ``--headless`` mode keeps a console for the daemon and
    the agents it starts, without showing a window. Output goes to a log file
    because the headless console discards it.
    """

    install_note = "The task uses your Windows user environment, including PATH."

    def __init__(
        self, home: Path, environ: Mapping[str, str], run: Runner = _run, name: str = TASK_NAME,
    ) -> None:
        self.home = home
        self.definition = name
        self._environ = environ
        self._run = run

    def log_hint(self, store_dir: Path) -> str:
        """Describe where the daemon's output goes."""
        return str(_log_path(store_dir))

    def task_xml(self, argv: Sequence[str], store_dir: Path) -> str:
        """Return the task definition: one log on trigger for this user, no time limit."""
        user = self._environ.get("USERNAME", "")
        if self._environ.get("USERDOMAIN"):
            user = f"{self._environ['USERDOMAIN']}\\{user}"
        root = PureWindowsPath(self._environ.get("SystemRoot") or r"C:\Windows")
        conhost = root / "System32" / "conhost.exe"
        command = [*argv, "--log-file", str(_log_path(store_dir))]
        arguments = "--headless " + subprocess.list2cmdline(command)
        return f"""<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Description>Flow Atelier scheduler</Description>
  </RegistrationInfo>
  <Triggers>
    <LogonTrigger>
      <Enabled>true</Enabled>
      <UserId>{escape(user)}</UserId>
    </LogonTrigger>
  </Triggers>
  <Principals>
    <Principal id="Author">
      <UserId>{escape(user)}</UserId>
      <LogonType>InteractiveToken</LogonType>
      <RunLevel>LeastPrivilege</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <AllowStartOnDemand>true</AllowStartOnDemand>
    <Enabled>true</Enabled>
    <ExecutionTimeLimit>PT0S</ExecutionTimeLimit>
    <RestartOnFailure>
      <Interval>PT1M</Interval>
      <Count>999</Count>
    </RestartOnFailure>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>{escape(str(conhost))}</Command>
      <Arguments>{escape(arguments)}</Arguments>
      <WorkingDirectory>{escape(str(self.home))}</WorkingDirectory>
    </Exec>
  </Actions>
</Task>
"""

    def install(self, argv: Sequence[str], env: Mapping[str, str], store_dir: Path) -> None:
        """Register the task, replacing an earlier one, and start it now."""
        _log_path(store_dir).parent.mkdir(parents=True, exist_ok=True)
        self._run(["schtasks", "/End", "/TN", self.definition])
        with tempfile.TemporaryDirectory() as tmp:
            definition = Path(tmp) / "task.xml"
            # schtasks reads task XML as UTF-16; Python writes the byte order mark.
            definition.write_text(self.task_xml(argv, store_dir), encoding="utf-16")
            _check(self._run, [
                "schtasks", "/Create", "/TN", self.definition, "/XML", str(definition), "/F",
            ])
        _check(self._run, ["schtasks", "/Run", "/TN", self.definition])

    def uninstall(self) -> bool:
        """Stop and delete the task. Return whether it was installed."""
        if not self._installed():
            return False
        self._run(["schtasks", "/End", "/TN", self.definition])
        _check(self._run, ["schtasks", "/Delete", "/TN", self.definition, "/F"])
        return True

    def status(self) -> ServiceStatus:
        """Read the task's state. PowerShell reports it in English on every locale."""
        installed = self._installed()
        running = False
        if installed:
            result = self._run([
                "powershell", "-NoProfile", "-NonInteractive", "-Command",
                f"(Get-ScheduledTask -TaskName '{self.definition}').State",
            ])
            running = result.returncode == 0 and result.stdout.strip() == "Running"
        return ServiceStatus(self.definition, installed, running)

    def _installed(self) -> bool:
        return self._run(["schtasks", "/Query", "/TN", self.definition]).returncode == 0


def scheduler_service(
    platform: str = sys.platform,
    environ: Mapping[str, str] = os.environ,
    home: Path | None = None,
    run: Runner = _run,
) -> Launchd | Systemd | TaskScheduler:
    """Return the service manager backend for this platform.

    :raises ServiceError: on platforms without a supported service manager.
    """
    home = home or Path.home()
    if platform == "darwin":
        return Launchd(home, run)
    if platform.startswith("linux"):
        return Systemd(Path(environ.get("XDG_CONFIG_HOME") or home / ".config"), run)
    if platform == "win32":
        return TaskScheduler(home, environ, run)
    raise ServiceError("starting the scheduler at login is supported on macOS, Linux and Windows")
