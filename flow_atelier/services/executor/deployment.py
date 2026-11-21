"""Bind a product deployment to its displayed plan, human answer and release files."""

import asyncio
import hashlib
import json
import os
import re
import shutil
import signal
import stat
import subprocess
import tempfile
from contextlib import ExitStack, suppress
from pathlib import Path
from typing import Protocol

from flow_atelier import product_events
from flow_atelier.core.settings import AtelierSettings
from flow_atelier.schemas.conduit import TaskDefinition
from flow_atelier.schemas.deployment import DeploymentTarget
from flow_atelier.schemas.log import ExecutionResult
from flow_atelier.services.executor.base import ExecutorBase, FlowContext
from flow_atelier.services.executor.worker import SYSTEM_PATH

BUILD_ONLY = "Build only"
LIFETIME = 600


def _matches_reporter(path: Path, expected: bytes) -> bool:
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as file:
            info = os.fstat(fd)
            return (
                stat.S_ISREG(info.st_mode)
                and info.st_nlink == 1
                and file.read(len(expected) + 1) == expected
            )
    except OSError:
        return False


def _snapshot(project: Path, sources: list[str], destination: Path) -> str:
    """Copy the exact release inputs and hash their relative paths, modes and content."""
    manifest = {}
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
    private = {".git", ".atelier", ".aws", ".azure", ".kube", ".config", ".env"}

    def copy(parent: int, relative: Path) -> None:
        if any(part in private or part.startswith(".env.") for part in relative.parts):
            raise ValueError(f"Private release input: {relative}")
        fd = os.open(relative.name, flags, dir_fd=parent)
        try:
            info = os.fstat(fd)
            if stat.S_ISDIR(info.st_mode):
                for name in sorted(os.listdir(fd)):
                    copy(fd, relative / name)
                return
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise ValueError(f"Expected a regular release file: {relative}")
            out = destination / relative
            out.parent.mkdir(parents=True, exist_ok=True)
            with os.fdopen(fd, "rb", closefd=False) as src, out.open("wb") as dst:
                shutil.copyfileobj(src, dst)
            mode = 0o755 if info.st_mode & 0o111 else 0o644
            out.chmod(mode)
            with out.open("rb") as src:
                manifest[relative.as_posix()] = [
                    mode,
                    hashlib.file_digest(src, "sha256").hexdigest(),
                ]
        finally:
            os.close(fd)

    for name in sources:
        relative = Path(name)
        if relative.is_absolute() or not relative.parts or ".." in relative.parts:
            raise ValueError("Release inputs must stay inside the project")
        try:
            with ExitStack() as folders:
                parent = os.open(project, flags | os.O_DIRECTORY)
                folders.callback(os.close, parent)
                for part in relative.parts[:-1]:
                    parent = os.open(part, flags | os.O_DIRECTORY, dir_fd=parent)
                    folders.callback(os.close, parent)
                copy(parent, relative)
        except OSError:
            raise ValueError(f"Cannot securely read release input: {name}") from None
    if not manifest:
        raise ValueError("No release files selected")
    return hashlib.sha256(json.dumps(manifest, sort_keys=True).encode()).hexdigest()


def _gcloud() -> str:
    path = shutil.which("gcloud", path=f"{SYSTEM_PATH}:{Path.home() / 'google-cloud-sdk/bin'}")
    if not path:
        raise ValueError("Install the Google Cloud CLI for Cloud Run deployment")
    return str(Path(path).resolve())


async def _run_cloud(argv: list[str], *, cwd: Path, env: dict, timeout: int) -> str:
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            cwd=cwd,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except OSError:
        raise RuntimeError("Cannot start the Google Cloud CLI") from None
    try:
        async with asyncio.timeout(timeout):
            out, _ = await proc.communicate()
    except BaseException:
        with suppress(ProcessLookupError):
            os.killpg(proc.pid, signal.SIGKILL)
        await proc.wait()
        raise
    if proc.returncode:
        raise RuntimeError(f"Cloud Run {argv[1]} command failed (exit {proc.returncode})")
    return out.decode().strip()


class Adapter(Protocol):
    def plan(self, destination: DeploymentTarget, configured: list[DeploymentTarget]) -> dict: ...

    async def deploy(self, destination: DeploymentTarget, target: str, source: Path) -> None: ...


class CloudRun:
    def plan(self, destination: DeploymentTarget, configured: list[DeploymentTarget]) -> dict:
        resource = (destination.project, destination.region, destination.service)
        for other in configured:
            if (
                other.deploy_account == destination.deploy_account
                and (other.project, other.region, other.service) != resource
            ):
                raise ValueError("Each Cloud Run target needs its own deployment identity")
        return {
            "action": "Build and deploy this source to Cloud Run",
            **destination.model_dump(),
            "token_lifetime_seconds": LIFETIME,
        }

    async def deploy(self, destination: DeploymentTarget, target: str, source: Path) -> None:
        binary = _gcloud()
        root = source.parent
        mint_env = {
            "HOME": str(Path.home()),
            "PATH": SYSTEM_PATH,
            "CLOUDSDK_CORE_DISABLE_FILE_LOGGING": "true",
        }
        if "CLOUDSDK_CONFIG" in os.environ:
            mint_env["CLOUDSDK_CONFIG"] = os.environ["CLOUDSDK_CONFIG"]
        token = await _run_cloud(
            [
                binary,
                "auth",
                "print-access-token",
                "--impersonate-service-account=" + destination.deploy_account,
                f"--lifetime={LIFETIME}s",
                "--quiet",
            ],
            cwd=root,
            env=mint_env,
            timeout=45,
        )
        if not token or len(token) > 8192 or any(c.isspace() for c in token):
            raise RuntimeError("Cloud Run returned an invalid access token")
        clean_home = root / "deploy-home"
        clean_home.mkdir()
        env = {
            "HOME": str(clean_home),
            "PATH": SYSTEM_PATH,
            "CLOUDSDK_AUTH_ACCESS_TOKEN": token,
            "CLOUDSDK_CORE_DISABLE_FILE_LOGGING": "true",
        }
        await _run_cloud(
            [
                binary,
                "run",
                "deploy",
                destination.service,
                "--project=" + destination.project,
                "--region=" + destination.region,
                "--source=" + str(source),
                "--quiet",
                f"--update-env-vars=ATELIER_PRODUCT_TARGET={target},ATELIER_ERROR_URL={destination.report_url}",
                "--update-secrets=ATELIER_ERROR_KEY=" + destination.report_secret,
            ],
            cwd=root,
            env=env,
            timeout=LIFETIME - 15,
        )


ADAPTERS: dict[str, Adapter] = {"cloud-run": CloudRun()}


class DeploymentExecutor(ExecutorBase):
    def __init__(self, settings: AtelierSettings):
        self.settings = settings

    async def execute(self, task: TaskDefinition, resolved_command: str, context: FlowContext):
        result = await self._execute(resolved_command, context)
        return ExecutionResult(output=result, stdout=result)

    async def _execute(self, action: str, context: FlowContext) -> str:
        project = context.working_dir.resolve()
        journal = context.store.journal(context.flow_id)
        if not journal or journal.header()["conduit"] != "idea-to-product":
            raise ValueError("Product deployment requires an idea-to-product run")
        label = context.inputs.get("deployment_target", "")
        if action in {"prepare", "plan"} and not label:
            return BUILD_ONLY
        if not re.fullmatch(r"[a-z][a-z0-9-]{0,39}", label):
            raise ValueError("Invalid deployment target")
        targets = self.settings.deployment_targets
        destination = targets.get(str(project), {}).get(label)
        if not destination:
            raise ValueError(
                "Select a deployment target configured for this project in user settings"
            )
        adapter = ADAPTERS[destination.provider]
        details = adapter.plan(
            destination, [item for group in targets.values() for item in group.values()]
        )
        helper = Path(product_events.__file__).read_bytes()
        reporter = project / "atelier_events.py"
        if action == "prepare":
            try:
                with reporter.open("xb") as out:
                    out.write(helper)
            except FileExistsError:
                if not _matches_reporter(reporter, helper):
                    raise ValueError("Existing atelier_events.py differs from the bundled reporter")
            return (
                f"Deployment target {label}. Add structured INFO/WARNING logs for this target "
                "and report application errors with stable event keys. Python apps can import "
                "atelier_events.emit(severity, key, message). Other runtimes must use the same "
                "JSON and signed webhook contract in atelier_events.py. Exercise warning and "
                "error paths in behavior tests with a fake transport."
            )
        if not _matches_reporter(reporter, helper):
            raise ValueError("Bundled application reporter changed")
        with tempfile.TemporaryDirectory(prefix="atelier-deploy-") as scratch:
            source = Path(scratch).resolve() / "source"
            source.mkdir()
            digest = _snapshot(project, [*destination.sources, "atelier_events.py"], source)
            plan = json.dumps(
                {
                    **details,
                    "target": label,
                    "target_path": str(project),
                    "runtime_reporter": "atelier_events.py",
                    "source_sha256": digest,
                },
                indent=2,
                sort_keys=True,
            )
            if action == "plan":
                return plan
            if action != "deploy":
                raise ValueError("Unknown product deployment operation")
            if context.task_outputs.get("deployment_plan") != plan:
                raise ValueError("Deployment plan or source changed; review a new plan")
            gate = {}
            for entry in journal.logs():
                if entry["task"] == "deployment_approval":
                    gate = entry
            if (
                gate.get("tool") != "tool:hitl"
                or gate.get("exit_code") != 0
                or gate.get("command", "").strip() != plan
                or gate.get("output", "").strip() != "deploy_decision: DEPLOY"
            ):
                raise ValueError("The displayed deployment plan requires explicit human approval")
            journal.side.mkdir(parents=True, exist_ok=True)
            receipt = journal.side / "deployment-receipt"
            done = f"Deployed {label} source {digest}."
            try:
                with receipt.open("x") as f:
                    f.write("started")
            except FileExistsError:
                if receipt.read_text() == done:
                    return done
                raise ValueError(
                    "Previous deployment is uncertain; inspect Cloud Run before retrying"
                ) from None
            await adapter.deploy(destination, label, source)
            receipt.write_text(done)
            return done
