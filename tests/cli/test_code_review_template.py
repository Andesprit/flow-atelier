"""End-to-end coverage for the `code-review` starter `atelier create` writes.

Runs the generated conduit through the real CLI, the real engine and the real
``tool:bash`` executor against a disposable Git repository. Only the agent is
faked: ``ATELIER_CLAUDE_LAUNCH_CMD`` points ``harness:claude-code`` at the
scripted ACP agent, which records every prompt it is handed.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from typer.testing import CliRunner

from flow_atelier.cli import app
from flow_atelier.core.atelier import Atelier
from flow_atelier.schemas.progress import FlowStatus, TaskStatus

FAKE_AGENT = Path(__file__).resolve().parents[1] / "fixtures" / "fake_acp_agent.py"
REVIEW_TEXT = "Finding: src/app.py:1 drops the return value."

# Staged content that would misbehave if the patch were re-templated or handed
# to a shell instead of being passed through as review material.
BOOBY_TRAP = 'print("$(touch pwned) `touch pwned2` {{inputs.example}}")'


def _git(repo: Path, *args: str) -> str:
    """Run a git command inside ``repo`` and return its stdout.

    :param repo: repository working directory.
    :param args: git arguments.
    :returns: captured stdout.
    """
    return subprocess.run(
        ["git", *args], cwd=repo, check=True, capture_output=True, text=True
    ).stdout


@pytest.fixture
def repo(tmp_path, monkeypatch):
    """A fresh Git repo with the `code-review` starter already scaffolded.

    :param tmp_path: pytest temp directory fixture.
    :param monkeypatch: pytest monkeypatch fixture.
    :returns: the repository path.
    """
    work = tmp_path / "repo"
    work.mkdir()
    (tmp_path / "global" / "conduits").mkdir(parents=True)
    for key in list(os.environ):
        if key.startswith(("ATELIER_", "GIT_")):
            monkeypatch.delenv(key, raising=False)
    # Without a ceiling, a temp dir nested inside someone's checkout would make
    # the "not a repository" case silently diff that outer repo instead.
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path))
    monkeypatch.setenv("GIT_AUTHOR_NAME", "Test")
    monkeypatch.setenv("GIT_AUTHOR_EMAIL", "test@example.invalid")
    monkeypatch.setenv("GIT_COMMITTER_NAME", "Test")
    monkeypatch.setenv("GIT_COMMITTER_EMAIL", "test@example.invalid")
    monkeypatch.setenv("ATELIER_GLOBAL_ATELIER_DIR", str(tmp_path / "global"))
    monkeypatch.setenv("ATELIER_NO_UPDATE_CHECK", "1")
    monkeypatch.setenv(
        "ATELIER_CLAUDE_LAUNCH_CMD",
        json.dumps(
            [
                sys.executable,
                str(FAKE_AGENT),
                "--script",
                json.dumps(
                    {
                        "turns": [{"chunks": [REVIEW_TEXT]}],
                        "record_path": str(tmp_path / "prompts.jsonl"),
                    }
                ),
            ]
        ),
    )
    _git(work, "init", "-q", "-b", "main", ".")
    monkeypatch.chdir(work)
    assert CliRunner().invoke(
        app, ["create", "review", "--template", "code-review"]
    ).exit_code == 0
    return work


def _prompts(repo: Path) -> str:
    """Return every prompt the fake agent was handed, as one string.

    :param repo: the repository path (its parent holds the record file).
    :returns: concatenated prompt text; empty when the agent never ran.
    """
    record = repo.parent / "prompts.jsonl"
    if not record.exists():
        return ""
    return "\n".join(
        block.get("text", "")
        for line in record.read_text().splitlines()
        for block in json.loads(line)
    )


def _flow_id(output: str) -> str:
    """Extract the flow id `atelier run` printed.

    :param output: captured CLI output.
    :returns: the flow id.
    """
    for line in output.splitlines():
        if "flow_id:" in line:
            return line.split("flow_id:", 1)[1].strip()
    raise AssertionError(f"no flow_id in output:\n{output}")


def test_only_the_staged_patch_reaches_the_agent(repo):
    """Staged work is reviewed; unstaged and untracked work is not."""
    (repo / "app.py").write_text("committed\n")
    (repo / "other.py").write_text("committed\n")
    _git(repo, "add", "app.py", "other.py")
    _git(repo, "commit", "-qm", "base")

    (repo / "app.py").write_text(f"{BOOBY_TRAP}\n")
    _git(repo, "add", "app.py")
    (repo / "other.py").write_text("UNSTAGED_EDIT\n")
    (repo / "scratch.py").write_text("UNTRACKED_FILE\n")
    before = _git(repo, "status", "--porcelain")

    result = CliRunner().invoke(app, ["run", "review"])
    assert result.exit_code == 0, result.output

    prompts = _prompts(repo)
    assert BOOBY_TRAP in prompts
    assert "UNSTAGED_EDIT" not in prompts
    assert "UNTRACKED_FILE" not in prompts
    # Passed through as data: no shell ran it, and the engine's single
    # substitution pass did not re-expand the patch's own template text.
    assert not (repo / "pwned").exists() and not (repo / "pwned2").exists()
    assert "{{inputs.example}}" in prompts
    # The workflow reads the repository; it never stages or commits for you.
    assert _git(repo, "status", "--porcelain") == before

    flow_id = _flow_id(result.output)
    progress = Atelier().get_status(flow_id)
    assert progress.status is FlowStatus.completed
    assert progress.tasks["diff"].status is TaskStatus.completed
    assert progress.tasks["review"].status is TaskStatus.completed

    logs = Atelier().get_flow_logs(flow_id)
    assert [entry.task for entry in logs] == ["diff", "review"]

    saved = CliRunner().invoke(app, ["outputs", flow_id, "--task", "review"])
    assert saved.exit_code == 0, saved.output
    assert REVIEW_TEXT in saved.stdout


def test_empty_index_skips_the_review(repo):
    """An empty patch completes the diff and never calls the agent."""
    (repo / "app.py").write_text("committed\n")
    _git(repo, "add", "app.py")
    _git(repo, "commit", "-qm", "base")
    (repo / "app.py").write_text("unstaged only\n")

    result = CliRunner().invoke(app, ["run", "review"])
    assert result.exit_code == 0, result.output

    progress = Atelier().get_status(_flow_id(result.output))
    assert progress.status is FlowStatus.completed
    assert progress.tasks["diff"].status is TaskStatus.completed
    assert progress.tasks["review"].status is TaskStatus.skipped
    assert _prompts(repo) == ""


def test_first_commit_is_reviewable(repo):
    """Staged work on an unborn branch still produces a patch to review."""
    (repo / "app.py").write_text("FIRST_COMMIT_CONTENT\n")
    _git(repo, "add", "app.py")

    result = CliRunner().invoke(app, ["run", "review"])
    assert result.exit_code == 0, result.output
    assert "FIRST_COMMIT_CONTENT" in _prompts(repo)


def test_outside_a_repository_the_flow_fails(repo, monkeypatch):
    """No repository means no patch: the run fails and the agent is untouched."""
    outside = repo.parent / "not-a-repo"
    target = outside / ".atelier" / "conduits" / "review"
    target.mkdir(parents=True)
    (target / "conduit.yaml").write_text(
        (repo / ".atelier" / "conduits" / "review" / "conduit.yaml").read_text()
    )
    monkeypatch.chdir(outside)

    result = CliRunner().invoke(app, ["run", "review"])
    assert result.exit_code == 1, result.output
    progress = Atelier().get_status(_flow_id(result.output))
    assert progress.status is FlowStatus.failed
    assert progress.tasks["diff"].status is TaskStatus.failed
    assert progress.tasks["review"].status is not TaskStatus.completed
    assert _prompts(repo) == ""


def test_check_reports_an_unavailable_harness(repo, monkeypatch):
    """With no agent on PATH, `check` fails with the reason, not a traceback."""
    monkeypatch.delenv("ATELIER_CLAUDE_LAUNCH_CMD", raising=False)
    monkeypatch.setattr(
        "flow_atelier.services.executor.harness.shutil.which",
        lambda _binary: None,
    )
    result = CliRunner().invoke(app, ["check", "review"])
    assert result.exit_code == 1
    assert "harness:claude-code" in result.output
    assert "Traceback" not in result.output


def test_check_and_plan_pass_with_a_ready_harness(repo):
    """The generated conduit validates and renders its two-wave plan."""
    checked = CliRunner().invoke(app, ["check", "review"])
    assert checked.exit_code == 0, checked.output
    assert "requires --input" not in checked.output

    planned = CliRunner().invoke(app, ["plan", "review"])
    assert planned.exit_code == 0, planned.output
    assert "diff" in planned.output and "review" in planned.output
