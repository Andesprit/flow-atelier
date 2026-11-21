"""Invoke the installed Atelier CLI, with a separate OS boundary for recipe trials."""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import signal
import subprocess
import sys
import tarfile
from pathlib import Path
from tempfile import TemporaryDirectory, TemporaryFile, mkdtemp

from flow_atelier.cli.updater import is_frozen_binary

PACKAGE = Path(__file__).resolve().parents[2]


def _command() -> list[str]:
    if is_frozen_binary():
        return [sys.executable]
    # -I keeps a target repository's sitecustomize.py and flow_atelier.py out of the broker.
    return [
        sys.executable,
        "-I",
        "-c",
        f"import sys; sys.path.insert(0, {str(PACKAGE.parent)!r}); "
        "from flow_atelier.main import app; app()",
    ]


def sandbox(root: Path, roots: set[Path], *, model: bool = False) -> list[str]:
    """Only scratch is writable. Only an authenticated, tool-free adapter gets model networking."""
    root = root.resolve()
    if sys.platform == "darwin":
        rules = " ".join(f"(subpath {json.dumps(str(p.resolve()))})" for p in roots | {root})
        work = json.dumps(str(root))
        profile = f"""(version 1)(deny default)(import "system.sb")
            (allow process-exec process-fork sysctl-read)
            (allow signal (target self))
            (allow file-read-metadata)
            (allow file-read-data (literal {json.dumps(str(PACKAGE.parent))}))
            (allow file-read* file-map-executable {rules}
                (subpath "/bin")(subpath "/usr/bin")(subpath "/usr/lib")(subpath "/System"))
            (allow file-write* (subpath {work}))(allow file-read* file-write* (literal "/dev/null"))
        """
        if model:
            profile += """(allow network-outbound (remote tcp "*:443"))
                (allow network-outbound (literal "/private/var/run/mDNSResponder"))
                (allow system-socket (socket-domain AF_SYSTEM))
                (allow mach-lookup (global-name "com.apple.dnssd.service")
                                   (global-name "com.apple.securityd")
                                   (global-name "com.apple.trustd.agent"))
                (allow file-read* (subpath "/private/etc/ssl"))"""
        return ["/usr/bin/sandbox-exec", "-p", profile]
    if sys.platform == "linux" and (bwrap := shutil.which("bwrap")):
        args = [
            bwrap,
            "--die-with-parent",
            "--unshare-all",
            "--new-session",
            "--proc",
            "/proc",
            "--dev",
            "/dev",
            "--tmpfs",
            "/tmp",
        ]
        if model:
            args.append("--share-net")
        system = ["/usr", "/bin", "/lib", "/lib64", "/etc/ld.so.cache", "/etc/localtime"]
        if model:
            system.extend(["/etc/ssl", "/etc/resolv.conf", "/etc/hosts", "/etc/nsswitch.conf"])
        for p in sorted(roots | {Path(s) for s in system}):
            if p.exists():
                args.extend(["--ro-bind", str(p), str(p)])
        return [*args, "--bind", str(root), str(root), "--chdir", str(root), "--"]
    raise ValueError("Isolation requires macOS sandbox-exec or Linux bubblewrap")


async def cli(
    project: Path, global_dir: Path, *args: str, data: str = "", trial: bool = False
) -> str:
    env = dict(
        os.environ,
        ATELIER_GLOBAL_ATELIER_DIR=str(global_dir),
        ATELIER_ATELIER_DIR=str(project / ".atelier"),
        ATELIER_NO_UPDATE_CHECK="1",
    )
    prefix = []
    if trial:
        roots = {Path(sys.prefix), Path(sys.base_prefix), PACKAGE}
        if is_frozen_binary():
            roots.update([Path(sys.executable), Path(sys._MEIPASS)])
        prefix = sandbox(project, roots)
        env = dict(
            PATH=os.defpath,
            HOME=str(project),
            TMPDIR=str(project),
            ATELIER_GLOBAL_ATELIER_DIR=str(global_dir),
            ATELIER_ATELIER_DIR=str(project / ".atelier"),
            ATELIER_NO_UPDATE_CHECK="1",
            PYTHONDONTWRITEBYTECODE="1",
        )
    process = await asyncio.create_subprocess_exec(
        *prefix,
        *_command(),
        *args,
        cwd=project,
        env=env,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        start_new_session=os.name == "posix",
    )
    try:
        async with asyncio.timeout(120 if trial else 3600):
            out, err = await process.communicate(data.encode())
    finally:
        if process.returncode is None:
            if os.name == "posix":
                os.killpg(process.pid, signal.SIGKILL)
            else:
                process.kill()
            await process.wait()
    if process.returncode:
        raise ValueError((err or out).decode(errors="replace")[-2000:] or "CLI failed")
    return out.decode().strip()


async def try_recipe(project: Path, name: str, definition: dict, inputs: dict) -> None:
    """Test the submitted definition against tracked HEAD in a disposable, networkless checkout."""
    with TemporaryDirectory(prefix="atelier-trial-") as directory:
        root = Path(directory).resolve()

        def checkout():
            with TemporaryFile() as archive_file:
                export = subprocess.run(
                    ["git", "-C", str(project), "archive", "--format=tar", "HEAD^{tree}"],
                    stdout=archive_file,
                    stderr=subprocess.PIPE,
                    timeout=60,
                )
                if export.returncode:
                    raise ValueError("Recipe trial requires a repository with a committed HEAD")
                archive_file.seek(0)
                with tarfile.open(fileobj=archive_file, mode="r|") as tar:
                    for member in tar:
                        parts = Path(member.name).parts
                        if parts[0] in {".env", ".git"} or (
                            parts[0] == ".atelier" and parts[:2] != (".atelier", "conduits")
                        ):
                            continue
                        if member.isfile() or member.isdir():
                            tar.extract(member, root, filter="data")

        await asyncio.to_thread(checkout)
        store = Path(mkdtemp(prefix=".trial-store-", dir=root))
        await cli(root, store, "save-recipe", name, data=json.dumps(definition), trial=True)
        await cli(root, store, "run", name, "--json", *input_args(inputs), trial=True)


def input_args(inputs: dict[str, str]) -> list[str]:
    return [f"--input={k}={v}" for k, v in inputs.items()]
