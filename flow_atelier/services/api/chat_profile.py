"""Sandbox the pinned ACP adapters and turn off their general-purpose tools."""

from __future__ import annotations

import json
import os
import shutil
import sys
from contextlib import contextmanager
from pathlib import Path

from flow_atelier.services.api.chat_cli import sandbox
from flow_atelier.services.executor.acp_registry import INSTALL_DIRNAME, installed_executable
from flow_atelier.services.executor.login_files import replace_private, write_back_login

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


def check_login(executor):
    if (
        sys.platform == "darwin"
        and Path(executor.launch_cmd[0]).name == "claude-agent-acp"
        and not os.environ.get("CLAUDE_CODE_OAUTH_TOKEN")
    ):
        raise ValueError("Set CLAUDE_CODE_OAUTH_TOKEN from claude setup-token before starting chat")


@contextmanager
def _login(source: Path | None, destination: Path):
    if destination.parent.resolve() != destination.parent:
        raise ValueError("Invalid adapter credential directory")
    destination.parent.mkdir(mode=0o700, exist_ok=True)
    if source is None or not source.is_file():
        destination.unlink(missing_ok=True)
        yield
        return
    real = source.resolve()
    original = real.read_bytes()
    replace_private(destination, original)
    try:
        yield
    finally:
        # The adapter has exited. Only accept a regular private file in this scratch home.
        if (
            destination.resolve() == destination
            and destination.is_file()
            and destination.stat().st_nlink == 1
        ):
            write_back_login(source, real, original, destination.read_bytes())


@contextmanager
def launch(executor, workspace: str):
    check_login(executor)
    root = Path(workspace).resolve()
    binary = Path(executor.launch_cmd[0])
    node = shutil.which("node")
    if not node:
        raise ValueError("The installed ACP adapter requires Node.js")
    modules = binary.parent.parent.resolve()
    roots = {modules, Path(node).resolve()}
    ca = Path("/etc/ssl/cert.pem")
    if sys.platform == "darwin":
        # Homebrew's Node links other formulae under the same Cellar.
        cellar = Path(node).resolve()
        if "Cellar" in cellar.parts:
            cellar = Path(*cellar.parts[: cellar.parts.index("Cellar") + 1])
            roots.add(cellar)
            brew_ca = cellar.parent / "etc/ca-certificates/cert.pem"
            if brew_ca.is_file():
                ca = brew_ca
    env = dict(
        PATH=os.pathsep.join([str(Path(node).resolve().parent), os.defpath]),
        HOME=str(root),
        TMPDIR=str(root),
        LANG="en_US.UTF-8",
        OPENSSL_CONF=os.devnull,
        CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC="1",
        CLAUDE_CODE_DISABLE_AUTO_MEMORY="1",
    )
    if sys.platform == "darwin" and ca.is_file():
        ca = ca.resolve()
        roots.add(ca)
        env.update(NODE_EXTRA_CA_CERTS=str(ca), SSL_CERT_FILE=str(ca))
    meta = {}
    if binary.name == "codex-acp":
        home = root / ".codex"
        if home.resolve() != home:
            raise ValueError("Invalid adapter home")
        home.mkdir(mode=0o700, exist_ok=True)
        source = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex") / "auth.json"
        credential = home / "auth.json"
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
        replace_private(
            home / "config.toml",
            (
                'sandbox_mode = "read-only"\nweb_search = "disabled"\n[features]\n'
                + "".join(f"{key} = false\n" for key in config["features"])
            ).encode(),
        )
        env.update(
            CODEX_HOME=str(home),
            INITIAL_AGENT_MODE="read-only",
            NO_BROWSER="1",
            CODEX_CONFIG=json.dumps(config),
        )
    else:
        source = None
        if token := os.environ.get("CLAUDE_CODE_OAUTH_TOKEN"):
            env["CLAUDE_CODE_OAUTH_TOKEN"] = token
        else:
            source = (
                Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude")
                / ".credentials.json"
            )
        credential = root / ".claude/.credentials.json"
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
    with _login(source, credential):
        yield argv, env, meta
