"""Publish only a locally verified commit to its run-owned draft branch."""

import json
import os
import re
import shlex
import shutil
import subprocess
import time
from pathlib import Path

from flow_atelier.services.executor.worker import SYSTEM_PATH

REPOSITORY = r"[A-Za-z0-9-]+/[A-Za-z0-9_.-]+"


def git(directory: Path, *args: str, data: bytes | None = None) -> bytes:
    """Run fixed git plumbing with user hooks, filters and credentials out of scope."""
    env = {
        "PATH": SYSTEM_PATH,
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_AUTHOR_NAME": "Atelier",
        "GIT_COMMITTER_NAME": "Atelier",
        "GIT_AUTHOR_EMAIL": "atelier@localhost",
        "GIT_COMMITTER_EMAIL": "atelier@localhost",
    }
    result = subprocess.run(
        ["git", "-c", f"core.hooksPath={os.devnull}", "-C", str(directory), *args],
        env=env,
        input=data or b"",
        capture_output=True,
        check=False,
        timeout=120,
    )
    if result.returncode:
        raise RuntimeError(f"git {args[0]} failed (exit {result.returncode})")
    return result.stdout


def _remote(argv: list[str], env: dict[str, str], cwd: Path, *, timeout: float = 120) -> str:
    result = subprocess.run(
        argv,
        cwd=cwd,
        env=env,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    if result.returncode:
        raise RuntimeError(
            f"GitHub publication failed ({Path(argv[0]).name}, exit {result.returncode})"
        )
    return result.stdout.strip()


def publish_draft(snapshot: Path, repository: str, branch: str) -> str:
    """Only push the verified branch and create or recover its draft PR."""
    if not re.fullmatch(r"atelier/errors/[0-9]{8}_[0-9a-f]{8}_error-to-pr", branch):
        raise ValueError("Invalid error PR destination")
    return _publish(snapshot, repository, branch, meeting=False)


def publish_meeting_draft(
    snapshot: Path,
    repository: str,
    task_id: str,
    branch: str,
    deadline: float | None = None,
) -> str:
    """Publish only this task's exact branch to a private repository."""
    if not re.fullmatch(r"[0-9a-f]{64}", task_id) or branch != f"atelier/meetings/{task_id}":
        raise ValueError("Invalid meeting PR destination")
    return _publish(snapshot, repository, branch, meeting=True, deadline=deadline)


def _publish(
    snapshot: Path,
    repository: str,
    branch: str,
    *,
    meeting: bool,
    deadline: float | None = None,
) -> str:
    if not re.fullmatch(REPOSITORY, repository) or repository.endswith(("/.", "/..")):
        raise ValueError("Invalid PR destination")
    if git(snapshot, "config", "--get", "atelier.repository").decode().strip() != repository:
        raise ValueError("Verified repository differs from publication target")
    gh = shutil.which("gh", path=SYSTEM_PATH)
    if not gh:
        raise ValueError("GitHub CLI is required for draft publication")
    env = {
        "HOME": str(Path.home()),
        "PATH": SYSTEM_PATH,
        "GH_HOST": "github.com",
        "GH_PROMPT_DISABLED": "1",
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
    }

    def remote(argv: list[str]) -> str:
        if deadline is None:
            return _remote(argv, env, snapshot)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("Meeting publication deadline reached")
        return _remote(argv, env, snapshot, timeout=min(remaining, 120))

    if meeting:
        visibility = remote([gh, "api", f"repos/{repository}", "--jq", ".visibility"])
        if visibility != "private":
            raise ValueError("Meeting drafts require a private GitHub repository")
    sha = git(snapshot, "rev-parse", "--verify", "refs/heads/" + branch).decode().strip()
    parent = git(snapshot, "rev-parse", sha + "^").decode().strip()
    existing = json.loads(
        remote(
            [
                gh,
                "pr",
                "list",
                "--repo",
                repository,
                "--head",
                branch,
                "--base",
                "main",
                "--state",
                "all",
                "--json",
                "url,headRefOid,isCrossRepository" + (",isDraft" if meeting else ""),
            ],
        )
    )
    if existing:
        if (
            len(existing) != 1
            or existing[0]["headRefOid"] != sha
            or existing[0]["isCrossRepository"]
            or (meeting and existing[0].get("isDraft") is not True)
        ):
            raise ValueError("Conflicting existing pull request")
        url = existing[0]["url"]
    else:
        main = remote(
            [gh, "api", f"repos/{repository}/git/ref/heads/main", "--jq", ".object.sha"],
        )
        if main != parent:
            raise ValueError("Remote main differs from the verified parent")
        ref = "refs/heads/" + branch
        remote(
            [
                "git",
                "-c",
                f"core.hooksPath={os.devnull}",
                "-c",
                "credential.helper=",
                "-c",
                f"credential.helper=!{shlex.quote(gh)} auth git-credential",
                "push",
                f"--force-with-lease={ref}:",
                f"https://github.com/{repository}.git",
                f"{sha}:{ref}",
            ],
        )
        url = remote(
            [
                gh,
                "pr",
                "create",
                "--repo",
                repository,
                "--draft",
                "--base",
                "main",
                "--head",
                branch,
                "--title",
                "Meeting task draft" if meeting else "Fix reported application error",
                "--body",
                (
                    f"Draft for meeting task {branch.split('/')[-1]}."
                    if meeting
                    else f"Run {branch.split('/')[-1]} reproduced the error on main. "
                    "The regression test and project suite passed on this branch."
                ),
            ],
        )
    if not re.fullmatch(rf"https://github\.com/{re.escape(repository)}/pull/[1-9][0-9]*", url):
        raise ValueError("GitHub returned an unexpected PR URL")
    return url
