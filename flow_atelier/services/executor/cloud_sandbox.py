"""Protect local cloud identities when starting product agents and their shell tests."""

import os
import shutil
import sys
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


def cloud_sandbox(
    argv: list[str], env: dict[str, str], cwd: Path, scratch: Path
) -> tuple[list[str], dict[str, str]]:
    """Wrap the actual process; its tools and shell descendants inherit the OS restrictions."""
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
    protected = sorted(p for p in paths if not paths.intersection(p.parents))
    cwd, scratch = cwd.resolve(), scratch.resolve()
    if any(cwd.is_relative_to(p) or p.is_relative_to(cwd) for p in protected):
        raise RuntimeError("Product workspace overlaps a private configuration directory")
    dropped = PUBLISHER_ENV | {"ATELIER_ERROR_KEY"} | {k for k in env if k.startswith(_PREFIXES)}
    with drop_credential_env(*dropped):
        # Preserve step output files and explicit harness environment; filter both sources.
        safe = child_environ(env)
    safe.update(
        TMPDIR=str(scratch),
        TMP=str(scratch),
        TEMP=str(scratch),
        XDG_CACHE_HOME=str(scratch / "cache"),
        npm_config_cache=str(scratch / "npm"),
    )
    writable = [cwd, scratch]
    for home in (user / ".claude", user / ".codex"):
        if home.is_dir() and not home.is_symlink():
            writable.append(home.resolve())
    if sys.platform == "darwin":
        binary = "/usr/bin/sandbox-exec"
        if not Path(binary).is_file():
            raise RuntimeError("The macOS sandbox is required for a product worker")
        rules = " ".join(f'(subpath (param "P{i}"))' for i in range(len(protected)))
        writes = " ".join(f'(subpath (param "W{i}"))' for i in range(len(writable)))
        policy = (
            "(version 1)(allow default)(deny file-write*)"
            '(allow file-write* (literal "/dev/null") (literal "/dev/tty") ' + writes + ")"
            "(deny file-read* file-write* " + rules + ")"
        )
        params = [arg for i, p in enumerate(protected) for arg in ("-D", f"P{i}={p}")]
        params += [arg for i, p in enumerate(writable) for arg in ("-D", f"W{i}={p}")]
        return [binary, *params, "-p", policy, *argv], safe
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
        for p in protected:
            if p.is_dir():
                wrapper.extend(["--tmpfs", str(p), "--remount-ro", str(p)])
            elif p.exists():
                wrapper.extend(["--ro-bind", "/dev/null", str(p)])
        return [*wrapper, "--", *argv], safe
    raise RuntimeError("Product workers need the macOS or Linux filesystem sandbox")
