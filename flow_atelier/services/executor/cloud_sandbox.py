"""Protect local cloud identities when starting product agents and their shell tests."""

import asyncio
import errno
import os
import shutil
import sys
from contextlib import asynccontextmanager
from pathlib import Path

from flow_atelier.services.executor.base import PUBLISHER_ENV, child_environ, drop_credential_env

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


def cloud_sandbox(
    argv: list[str], env: dict[str, str], cwd: Path, scratch: Path
) -> tuple[list[str], dict[str, str]]:
    """Wrap the actual process; its tools and shell descendants inherit the OS restrictions."""
    protected = protected_paths(env)
    cwd, scratch = cwd.resolve(), scratch.resolve()
    if any(cwd.is_relative_to(p) or p.is_relative_to(cwd) for p in protected):
        raise RuntimeError("Product workspace overlaps a private configuration directory")
    dropped = PUBLISHER_ENV | {"ATELIER_ERROR_KEY"} | {k for k in env if k.startswith(_PREFIXES)}
    with drop_credential_env(*dropped):
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
    for variable, dirname, login in (
        ("CLAUDE_CONFIG_DIR", ".claude", ".credentials.json"),
        ("CODEX_HOME", ".codex", "auth.json"),
    ):
        source = Path(env.get(variable, Path.home() / dirname)) / login
        private = scratch / dirname
        private.mkdir(mode=0o700, exist_ok=True)
        if source.is_file():
            (private / login).symlink_to(source.resolve())
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
