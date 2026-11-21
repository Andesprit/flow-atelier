"""Protect local cloud identities when starting product agents and their shell tests."""

import asyncio
import errno
import json
import os
import shutil
import stat
import sys
from collections.abc import Iterator
from contextlib import ExitStack, asynccontextmanager, contextmanager, suppress
from pathlib import Path
from tempfile import NamedTemporaryFile

from flow_atelier.schemas.conduit import split_harness_tool
from flow_atelier.services.executor.base import PUBLISHER_ENV, child_environ, drop_credential_env
from flow_atelier.services.store.events import file_lock

_PATH_ENV = (
    "CLOUDSDK_CONFIG",
    "GOOGLE_APPLICATION_CREDENTIALS",
    "CLOUDSDK_AUTH_CREDENTIAL_FILE_OVERRIDE",
    "CLOUDSDK_AUTH_ACCESS_TOKEN_FILE",
    "GH_CONFIG_DIR",
    "AWS_SHARED_CREDENTIALS_FILE",
    "AWS_CONFIG_FILE",
    "AZURE_CONFIG_DIR",
    "KUBECONFIG",
)
_PREFIXES = ("CLOUDSDK_", "GOOGLE_", "AWS_", "AZURE_", "ARM_", "KUBE")


def protected_paths(env: dict[str, str]) -> list[Path]:
    """Cloud and publisher paths shared by the native and outer sandbox policies."""
    user = Path.home()
    protected = [
        user / item
        for item in (
            ".aws",
            ".azure",
            ".kube",
            ".config/gcloud",
            ".config/gh",
            ".ssh",
            ".atelier",
        )
    ]
    if "XDG_CONFIG_HOME" in env:
        protected.append(Path(env["XDG_CONFIG_HOME"]) / "gcloud")
    protected.append(Path(os.environ.get("ATELIER_GLOBAL_ATELIER_DIR", user / ".atelier")))
    for name in _PATH_ENV:
        for item in (os.environ.get(name, ""), env.get(name, "")):
            for value in item.split(os.pathsep) if name == "KUBECONFIG" else [item]:
                if value:
                    protected.append(Path(value).expanduser())
    paths = {p.resolve() for p in protected}
    return sorted(p for p in paths if not paths.intersection(p.parents))


@asynccontextmanager
async def product_git_guard(cwd: Path | None):
    """Remove Bubblewrap's temporary .git mountpoint after the last user has exited."""
    if cwd is None or sys.platform != "linux":
        yield
        return
    import fcntl

    metadata = cwd / ".git"
    fd = os.open(cwd, os.O_RDONLY | os.O_DIRECTORY)

    async def lock(mode: int) -> None:
        while True:
            try:
                fcntl.flock(fd, mode | fcntl.LOCK_NB)
                return
            except BlockingIOError:
                await asyncio.sleep(0.05)

    try:
        await lock(fcntl.LOCK_SH)
        fresh = not os.path.lexists(metadata)
        if fresh:
            # A new target needs exclusive ownership while its synthetic mountpoint exists.
            await lock(fcntl.LOCK_EX)
            fresh = not os.path.lexists(metadata)
        try:
            yield
        finally:
            if fresh:
                try:
                    metadata.rmdir()
                except OSError as exc:
                    if exc.errno not in (errno.ENOENT, errno.ENOTEMPTY, errno.ENOTDIR):
                        raise
    finally:
        os.close(fd)


def _read_scratch_login(path: Path) -> bytes | None:
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
    try:
        with ExitStack() as resources:
            parent = os.open(path.parent, flags | os.O_DIRECTORY)
            resources.callback(os.close, parent)
            fd = os.open(path.name, flags, dir_fd=parent)
            resources.callback(os.close, fd)
            info = os.fstat(fd)
            if stat.S_ISREG(info.st_mode) and info.st_nlink == 1:
                with os.fdopen(fd, "rb", closefd=False) as file:
                    return file.read()
    except OSError as exc:
        if exc.errno not in (errno.ENOENT, errno.ENOTDIR, errno.ELOOP, errno.ENXIO):
            raise
    return None


def _launches_claude(argv: list[str]) -> bool:
    executable = Path(argv[0]).name
    if executable != "npx":
        return executable == "claude-agent-acp"
    # Match the package npx runs, never a later argument to another adapter.
    package = next((arg for arg in argv[1:] if arg not in {"-y", "--yes", "--"}), "")
    claude = "@agentclientprotocol/claude-agent-acp"
    return package == claude or package.startswith(claude + "@")


@contextmanager
def product_login(
    tool: str, argv: list[str], env: dict[str, str], scratch: Path,
) -> Iterator[str | None]:
    """Yield the Claude token or provision a file-backed login for the selected agent."""
    if sys.platform not in {"darwin", "linux"}:
        yield
        return
    name, _, _ = split_harness_tool(tool)
    if name in {"harness:claude-code", "harness:claude-acp"} or _launches_claude(argv):
        if token := env.get("CLAUDE_CODE_OAUTH_TOKEN"):
            yield token
            return
        if sys.platform == "darwin":
            raise ValueError(
                "For Claude product steps on macOS, run claude setup-token "
                "and set CLAUDE_CODE_OAUTH_TOKEN to the token it prints"
            )
        variable, dirname, filename = "CLAUDE_CONFIG_DIR", ".claude", ".credentials.json"
    elif name in {"harness:codex", "harness:codex-acp"}:
        variable, dirname, filename = "CODEX_HOME", ".codex", "auth.json"
    else:
        yield
        return

    source = Path(env.get(variable) or Path.home() / dirname) / filename
    real = source.resolve()
    if not real.is_file():
        yield
        return
    original = real.read_bytes()
    copy = scratch / dirname / filename
    copy.parent.mkdir(mode=0o700, exist_ok=True)
    copy.write_bytes(original)
    copy.chmod(0o600)
    try:
        yield
    finally:
        refreshed = _read_scratch_login(copy)
        if refreshed is not None and refreshed != original:
            if not isinstance(json.loads(refreshed), dict):
                raise ValueError("Invalid refreshed agent credentials")
            # The user can log out and remove the login directory during the step.
            with (
                suppress(FileNotFoundError),
                file_lock(real.with_suffix(".atelier.lock"), wait=True),
            ):
                if source.resolve() == real and real.is_file() and real.read_bytes() == original:
                    with NamedTemporaryFile(dir=real.parent, delete_on_close=False) as temp:
                        temp.write(refreshed)
                        temp.close()
                        os.replace(temp.name, real)


def cloud_sandbox(
    argv: list[str], env: dict[str, str], cwd: Path, scratch: Path
) -> tuple[list[str], dict[str, str]]:
    """Wrap the actual process; its tools and shell descendants inherit the OS restrictions."""
    protected = protected_paths(env)
    cwd, scratch = cwd.resolve(), scratch.resolve()
    if any(cwd.is_relative_to(p) or p.is_relative_to(cwd) for p in protected):
        raise RuntimeError("Product workspace overlaps a private configuration directory")
    dropped = PUBLISHER_ENV | {"ATELIER_ERROR_KEY"} | {k for k in env if k.startswith(_PREFIXES)}
    with drop_credential_env("CLAUDE_CODE_OAUTH_TOKEN", *dropped):
        # Preserve step output files and explicit harness environment; filter both sources.
        safe = child_environ(env)
    safe.update(
        HOME=str(scratch),
        TMPDIR=str(scratch),
        TMP=str(scratch),
        TEMP=str(scratch),
        XDG_CACHE_HOME=str(scratch / "cache"),
        npm_config_cache=str(scratch / "npm"),
    )
    writable = [cwd, scratch]
    for variable, dirname in (("CLAUDE_CONFIG_DIR", ".claude"), ("CODEX_HOME", ".codex")):
        private = scratch / dirname
        private.mkdir(mode=0o700, exist_ok=True)
        if variable in env:
            safe[variable] = str(private)
    # Protect repository metadata even when the checkout lies inside a writable workspace.
    metadata = cwd / ".git"
    if sys.platform == "darwin":
        binary = "/usr/bin/sandbox-exec"
        if not Path(binary).is_file():
            raise RuntimeError("The macOS sandbox is required for a product worker")
        rules = " ".join(f'(subpath (param "P{i}"))' for i in range(len(protected)))
        writes = " ".join(f'(subpath (param "W{i}"))' for i in range(len(writable)))
        policy = (
            "(version 1)(allow default)(deny file-write*)"
            '(allow file-write* (literal "/dev/null") (literal "/dev/tty") ' + writes + ")"
            '(deny file-write* (subpath (param "GIT")))'
            "(deny file-read* file-write* " + rules + ")"
        )
        params = [arg for i, p in enumerate(protected) for arg in ("-D", f"P{i}={p}")]
        params += [arg for i, p in enumerate(writable) for arg in ("-D", f"W{i}={p}")]
        return [binary, *params, "-D", f"GIT={metadata}", "-p", policy, *argv], safe
    if sys.platform == "linux":
        binary = shutil.which("bwrap", path="/usr/bin:/bin")
        if not binary:
            raise RuntimeError("Install Bubblewrap to run a cloud-isolated product worker")
        wrapper = [
            binary,
            "--die-with-parent",
            "--new-session",
            "--unshare-pid",
            "--ro-bind",
            "/",
            "/",
            "--proc",
            "/proc",
            "--dev",
            "/dev",
            "--cap-drop",
            "ALL",
        ]
        for p in writable:
            wrapper.extend(["--bind", str(p), str(p)])
        if metadata.exists() or metadata.is_symlink():
            wrapper.extend(["--ro-bind", str(metadata), str(metadata)])
        else:
            wrapper.extend(["--tmpfs", str(metadata), "--remount-ro", str(metadata)])
        for p in protected:
            if p.is_dir():
                wrapper.extend(["--tmpfs", str(p), "--remount-ro", str(p)])
            elif p.exists():
                wrapper.extend(["--ro-bind", "/dev/null", str(p)])
        return [*wrapper, "--", *argv], safe
    raise RuntimeError("Product workers need the macOS or Linux filesystem sandbox")
