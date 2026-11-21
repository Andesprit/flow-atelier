"""Disposable Codex workspace-write session for untrusted reports and transcripts."""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
from contextlib import AsyncExitStack, suppress
from pathlib import Path
from typing import Any

from flow_atelier.services.executor.cloud_sandbox import protected_paths

# The launcher does not inherit application, cloud, publisher or notification credentials.
SYSTEM_PATH = "/usr/bin:/bin:/usr/sbin:/sbin:/usr/local/bin:/opt/homebrew/bin"
OFF = (
    "apps",
    "plugins",
    "hooks",
    "multi_agent",
    "browser_use",
    "computer_use",
    "in_app_browser",
    "image_generation",
    "code_mode",
    "remote_control",
    "tool_suggest",
)


class WorkerUnavailable(RuntimeError):
    """The trusted launcher could not establish the restricted execution profile."""


async def _stop(proc: asyncio.subprocess.Process) -> None:
    with suppress(ProcessLookupError):
        os.killpg(proc.pid, signal.SIGKILL)
    await proc.wait()


class WorkerSandbox:
    """One checkout with restricted tools and a separate private launcher home."""

    def __init__(self, workspace: Path, *, read_only: bool = False) -> None:
        self.read_only = read_only
        self.workspace = workspace.resolve(strict=True)
        self._stack = AsyncExitStack()
        self._sequence = 0

    async def __aenter__(self) -> WorkerSandbox:
        binary = shutil.which("codex", path=f"{SYSTEM_PATH}:{Path.home() / '.local/bin'}")
        if not binary or sys.platform not in {"linux", "darwin"}:
            raise WorkerUnavailable("Codex with OS sandbox support is required")
        self.binary = str(Path(binary).absolute())
        try:
            root = Path(self._stack.enter_context(tempfile.TemporaryDirectory())).resolve()
            if root.is_relative_to(self.workspace):
                raise WorkerUnavailable("Scratch workspace would expose launcher credentials")
            self.control = root / "launcher"
            self.control.mkdir()
            home = root / "home"
            home.mkdir()
            login = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex")) / "auth.json"
            private_dirs = (Path.home() / ".claude", Path.home() / ".codex", login.parent)
            if any(
                p.resolve().is_relative_to(self.workspace)
                or self.workspace.is_relative_to(p.resolve())
                for p in private_dirs
            ):
                raise WorkerUnavailable("Scratch workspace overlaps an agent credential directory")
            self.files = {
                ":minimal": "read",
                "/tmp": "deny",
                "/private/tmp": "deny",
                "/var/tmp": "deny",
                str(self.control): "read",
                str(self.workspace): "read" if self.read_only else "write",
                str(home): "deny",
                **{str(path.resolve()): "deny" for path in private_dirs},
            }
            if self.read_only:
                for path in protected_paths(dict(os.environ)):
                    if self.workspace.is_relative_to(path):
                        raise WorkerUnavailable("Product workspace overlaps a private directory")
                    self.files[str(path)] = "deny"
            if login.is_file():
                real_login = login.resolve()
                if real_login.is_relative_to(self.workspace):
                    raise WorkerUnavailable("Scratch workspace would expose the agent login")
                self.files[str(real_login)] = "deny"
                (home / "auth.json").symlink_to(real_login)
            self.env = {"HOME": str(home), "CODEX_HOME": str(home), "PATH": SYSTEM_PATH}
            self.tool_tmp = self.control if self.read_only else Path(
                tempfile.mkdtemp(prefix=".worker-", dir=self.workspace)
            )
            config = [
                'default_permissions = "worker"',
                'approval_policy = "never"',
                'web_search = "disabled"',
                "allow_login_shell = false",
                'shell_environment_policy.inherit = "none"',
                "shell_environment_policy.set.PATH = " + json.dumps(SYSTEM_PATH),
                "shell_environment_policy.set.TMPDIR = " + json.dumps(str(self.tool_tmp)),
                "[permissions.worker.network]",
                "enabled = false",
                "[permissions.worker.filesystem]",
                *(f'{json.dumps(path)} = "{access}"' for path, access in self.files.items()),
                "[features]",
                *(f"{key} = false" for key in OFF),
            ]
            (home / "config.toml").write_text("\n".join(config) + "\n", encoding="utf-8")
            self.proc = await asyncio.create_subprocess_exec(
                self.binary,
                "app-server",
                "--stdio",
                cwd=self.control,
                env=self.env,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
                limit=4 * 1024 * 1024,
            )
            self._stack.push_async_callback(_stop, self.proc)
            async with asyncio.timeout(30):
                await self._rpc("initialize", {"clientInfo": {"name": "atelier", "version": "1"}})
                config = (await self._rpc("config/read", {"cwd": str(self.control)}))["config"]
                self._check_config(config)
                started = await self._rpc(
                    "thread/start", {"cwd": str(self.control), "ephemeral": True}
                )
                policy = started["sandbox"]
                if self.read_only:
                    valid = policy.get("type") == "readOnly"
                else:
                    valid = (
                        policy.get("type") == "workspaceWrite"
                        and policy.get("writableRoots") == [str(self.workspace)]
                        and policy.get("excludeSlashTmp")
                        and policy.get("excludeTmpdirEnvVar")
                    )
                if (not valid or policy.get("networkAccess") is not False
                        or started.get("approvalPolicy") != "never"):
                    raise WorkerUnavailable("Codex did not apply the restricted workspace policy")
                self.thread = started["thread"]["id"]
                servers = await self._rpc("mcpServerStatus/list", {})
                if servers.get("data") != [] or servers.get("nextCursor"):
                    raise WorkerUnavailable("Worker has an MCP server")
            return self
        except BaseException:
            await self._stack.aclose()
            raise

    def _check_config(self, config: dict) -> None:
        profile = config.get("permissions", {}).get("worker", {})
        files = {k: v for k, v in profile.get("filesystem", {}).items() if v is not None}
        if (
            config.get("default_permissions") != "worker"
            or files != self.files
            or profile.get("extends")
            or {k: v for k, v in profile.get("network", {}).items() if v is not None}
            != {"enabled": False}
            or config.get("web_search") != "disabled"
            or config.get("mcp_servers")
            or any(config.get("features", {}).get(key) is not False for key in OFF)
        ):
            raise WorkerUnavailable(
                "Codex lacks the required filesystem, network or tool restrictions"
            )

    async def __aexit__(self, *exc: object) -> None:
        await self._stack.aclose()

    async def stop_agent(self) -> None:
        """Stop an unfinished turn before collecting files with the restricted command runner."""
        await _stop(self.proc)

    async def _send(self, message: dict) -> None:
        self.proc.stdin.write(json.dumps(message).encode() + b"\n")
        await self.proc.stdin.drain()

    async def _message(self) -> dict:
        while line := await self.proc.stdout.readline():
            message = json.loads(line)
            if "method" not in message or "id" not in message:
                return message
            # No approvals, tool callbacks, credentials or extra permissions from the host.
            await self._send({"id": message["id"], "error": {"code": -32601, "message": "Denied"}})
        raise WorkerUnavailable("Codex worker exited")

    async def _rpc(self, method: str, params: dict) -> dict[str, Any]:
        self._sequence += 1
        await self._send({"id": self._sequence, "method": method, "params": params})
        while True:
            message = await self._message()
            if message.get("id") == self._sequence:
                if "result" not in message:
                    raise WorkerUnavailable(f"Codex refused {method}")
                return message["result"]

    async def turn(
        self, prompt: str, *, timeout: float = 900, model: str | None = None,
        effort: str | None = None,
    ) -> str:
        """Send data only after confirming the active thread's permissions and empty MCP list."""
        answer = ""
        location = f"Use {self.workspace} as the working directory for every command."
        if self.read_only:
            location += " Temporary files are unavailable in this read-only session."
        async with asyncio.timeout(timeout):
            await self._rpc(
                "turn/start",
                {
                    "threadId": self.thread,
                    **({"model": model} if model else {}),
                    **({"effort": effort} if effort else {}),
                    "input": [
                        {"type": "text", "text": f"{location}\n{prompt}"},
                    ],
                },
            )
            while True:
                message = await self._message()
                if message.get("method") == "item/completed":
                    item = message["params"]["item"]
                    if item.get("type") == "agentMessage":
                        answer = item["text"]
                if message.get("method") == "turn/completed":
                    if message["params"]["turn"]["status"] != "completed":
                        raise RuntimeError("Restricted worker turn failed")
                    return answer

    async def command(self, argv: list[str]) -> subprocess.CompletedProcess:
        """Independently verify the worker's changes inside the same OS sandbox."""
        with tempfile.TemporaryFile() as log:
            proc = await asyncio.create_subprocess_exec(
                self.binary,
                "sandbox",
                "--include-managed-config",
                "-P",
                "worker",
                "-C",
                self.control,
                "--",
                "/bin/sh",
                "-c",
                'cd "$1" && shift && exec "$@"',
                "verify",
                self.workspace,
                "/usr/bin/env",
                "-i",
                f"PATH={SYSTEM_PATH}",
                f"TMPDIR={self.tool_tmp}",
                "PYTHONDONTWRITEBYTECODE=1",
                *argv,
                cwd=self.control,
                env=self.env,
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
            try:
                async with asyncio.timeout(300):
                    await proc.wait()
            finally:
                await _stop(proc)
            log.seek(max(0, log.tell() - 8192))
            return subprocess.CompletedProcess(
                argv, proc.returncode, log.read().decode(errors="replace")
            )
