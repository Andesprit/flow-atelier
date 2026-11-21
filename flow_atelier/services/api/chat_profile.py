"""Sandbox the pinned ACP adapters and turn off their general-purpose tools."""

from __future__ import annotations

import json
import os
import shutil
import sys
from pathlib import Path

from flow_atelier.services.api.chat_cli import sandbox
from flow_atelier.services.executor.acp_registry import INSTALL_DIRNAME, installed_executable

PACKAGES = ("@agentclientprotocol/claude-agent-acp@0.62.0", "@agentclientprotocol/codex-acp@1.1.7")


def supported(atelier, executor) -> bool:
    if not (sys.platform == "darwin" or (sys.platform == "linux" and shutil.which("bwrap"))):
        return False
    for package in PACKAGES:
        binary = installed_executable(
            atelier.settings.global_atelier_dir / INSTALL_DIRNAME, package
        )
        if binary and executor.launch_cmd == [str(binary)]:
            return True
    return False


def _login(source: Path, destination: Path, roots: set[Path]):
    if source.is_file() and not destination.exists():
        destination.parent.mkdir(mode=0o700, exist_ok=True)
        destination.symlink_to(source.resolve())
    if destination.is_symlink():
        roots.add(source.resolve())


def launch(executor, workspace: str):
    root = Path(workspace).resolve()
    binary = Path(executor.launch_cmd[0])
    node = shutil.which("node")
    if not node:
        raise ValueError("The installed ACP adapter requires Node.js")
    modules = binary.parent.parent.resolve()
    roots = {modules, Path(node).resolve()}
    if sys.platform == "darwin":
        # Homebrew's Node links other formulae under the same Cellar.
        cellar = Path(node).resolve()
        if "Cellar" in cellar.parts:
            roots.add(Path(*cellar.parts[: cellar.parts.index("Cellar") + 1]))
    env = dict(
        PATH=os.pathsep.join([str(Path(node).resolve().parent), os.defpath]),
        HOME=str(root),
        TMPDIR=str(root),
        LANG="en_US.UTF-8",
        OPENSSL_CONF=os.devnull,
        CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC="1",
        CLAUDE_CODE_DISABLE_AUTO_MEMORY="1",
    )
    meta = {}
    if binary.name == "codex-acp":
        home = root / ".codex"
        home.mkdir(exist_ok=True)
        _login(
            Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex") / "auth.json",
            home / "auth.json",
            roots,
        )
        # Configure both app-server startup and the adapter's thread/start request.
        disabled = (
            "shell_tool unified_exec code_mode code_mode_host apps browser_use computer_use "
            "hooks image_generation in_app_browser multi_agent plugins remote_plugin"
        )
        config = {
            "features": dict.fromkeys(disabled.split(), False),
            "web_search": "disabled",
            "sandbox_mode": "read-only",
            "mcp_servers": {},
        }
        (home / "config.toml").write_text(
            'sandbox_mode = "read-only"\nweb_search = "disabled"\n[features]\n'
            + "".join(f"{key} = false\n" for key in config["features"]),
            encoding="utf-8",
        )
        env.update(
            CODEX_HOME=str(home),
            INITIAL_AGENT_MODE="read-only",
            NO_BROWSER="1",
            CODEX_CONFIG=json.dumps(config),
        )
    else:
        # Linux file login and macOS Keychain login stay in the trusted adapter.
        if token := os.environ.get("CLAUDE_CODE_OAUTH_TOKEN"):
            env["CLAUDE_CODE_OAUTH_TOKEN"] = token
        creds = (
            Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude")
            / ".credentials.json"
        )
        _login(creds, root / ".claude/.credentials.json", roots)
        options = dict(
            tools=[],
            allowedTools=[],
            disallowedTools=["mcp__*"],
            mcpServers={},
            strictMcpConfig=True,
            settingSources=[],
            plugins=[],
            skills=[],
            settings={"disableAllHooks": True},
            extraArgs={"disable-slash-commands": None},
        )
        meta = {"disableBuiltInTools": True, "claudeCode": {"options": options}}
    argv = [*sandbox(root, roots, model=True), str(Path(node).resolve()), str(binary.resolve())]
    return argv, env, meta
