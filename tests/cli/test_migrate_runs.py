"""Explicit migration from an old project store to global run journals."""

import asyncio
import json
import os
import socket
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from typer.testing import CliRunner

from flow_atelier.cli import app
from flow_atelier.core.atelier import Atelier
from flow_atelier.modules import liveness
from flow_atelier.modules.workspace import create_worktrees, resolve_source_repo
from flow_atelier.schemas.log import IntermediateStep, LogEntry, StepRecord
from flow_atelier.schemas.progress import FlowStatus, PendingAsk, Progress, TaskProgress
from flow_atelier.services.api.app import FastApiServer
from flow_atelier.services.store import retention
from flow_atelier.services.store.events import RunJournal, file_lock, project_folder
from flow_atelier.services.store.filesystem import FilesystemStore


@pytest.fixture
def tree(tmp_path, monkeypatch):
    project = tmp_path / "target"
    project.mkdir()
    monkeypatch.chdir(project)
    base = tmp_path / "old-store"
    home = tmp_path / "global"
    monkeypatch.setenv("ATELIER_ATELIER_DIR", str(base))
    monkeypatch.setenv("ATELIER_GLOBAL_ATELIER_DIR", str(home))
    old = FilesystemStore(base)
    root = old.create_flow("outer", {"input": "hello"})
    child = old.create_flow("inner", {}, root)
    for run in (root, child):
        old.write_progress(run, Progress(
            status="failed", tasks={"work": TaskProgress(status="running")},
            started_at="2026-01-01T01:02:03Z", finished_at="2026-01-01T04:05:06Z",
            run_path=str(project), runner_pid=123, runner_host="old-host",
        ))
    old.write_sessions(child, {"work": {"id": "saved-session", "harness": "demo"}})
    raw = "é" * 50_000 + "\r"
    old.write_outputs(child, {"work": raw})
    asyncio.run(old.append_log(child, LogEntry(
        task="work", tool="tool:bash", output=raw, started_at="before", finished_at="after",
    )))
    asyncio.run(old.append_step(child, StepRecord(
        task="work", step=IntermediateStep(kind="tool_call", tool_name="read"),
    )))
    return old, home, project, root, child


def migrate(*args):
    return CliRunner().invoke(app, ["migrate", "runs", *args])


def contents(path):
    return {p.relative_to(path): p.read_bytes() for p in path.rglob("*") if p.is_file()}


async def api_view(run):
    transport = httpx.ASGITransport(app=FastApiServer().create_app(Atelier()))
    async with httpx.AsyncClient(transport=transport, base_url="http://localhost") as client:
        resp = await client.get(f"/flows/{run}")
        resp.raise_for_status()
        return resp.json()


def test_migrate_replays_tree_and_resumes_original_worktree(tree):
    old, home, project, root, child = tree
    subprocess.run(["git", "init", "-q", str(project)], check=True)
    subprocess.run(["git", "-c", "user.name=T", "-c", "user.email=t@test", "commit",
                    "--allow-empty", "-qm", "base"], check=True, cwd=project)
    progress = old.read_progress(root)
    progress.workspaces = create_worktrees(resolve_source_repo(project), old.workspace_dir(root), ["work"])
    checkout = Path(progress.workspaces.paths["work"])
    (checkout / "original").write_text("original worktree")
    old.write_progress(root, progress)
    recipe = old.base_dir / "conduits/outer/conduit.yaml"
    recipe.parent.mkdir(parents=True)
    recipe.write_text("name: outer\ndescription: test resume\ntasks:\n  - name: work\n"
                      "    description: read prior work\n    tool: tool:bash\n    task: cat original\n")
    legacy_dir = old.base_dir / "flows" / root
    before = contents(legacy_dir)
    runner = CliRunner()
    status = runner.invoke(app, ["status", root, "--json"])
    assert status.exit_code == 0, status.output
    api = asyncio.run(api_view(child))

    preview = migrate("--dry-run")
    assert preview.exit_code == 0, preview.output
    assert root in preview.stdout and child in preview.stdout
    assert not home.exists() and contents(legacy_dir) == before

    done = migrate()
    assert done.exit_code == 0, done.output
    assert not legacy_dir.exists()
    archive = old.base_dir / "migrated-runs" / root
    assert all((archive / path).read_bytes() == data for path, data in before.items())
    assert json.loads(runner.invoke(app, ["status", root, "--json"]).stdout) == json.loads(status.stdout)
    assert asyncio.run(api_view(child)) == api
    store = FilesystemStore(old.base_dir, home)
    assert store.parent_flow_id(child) == root
    assert store.list_child_flows(root) == [child]
    assert store.read_sessions(child)["work"]["id"] == "saved-session"
    raw = ("é" * 50_000 + "\r").encode()
    assert store.read_outputs(child)["work"].encode() == store.read_logs(child)[0].output.encode() == raw
    journal = store.journal(child)
    assert (journal.side / "steps.jsonl").read_bytes() == before[Path("flows") / child / "steps.jsonl"]
    assert [p.read_bytes() for p in (journal.side / "outputs/work").iterdir()] == [raw, raw]
    assert journal.header()["host"] == "old-host" and journal.header()["pid"] == 123
    assert store.workspace_dir(root) == old.workspace_dir(root)
    resumed = runner.invoke(app, ["run", "--resume", root])
    assert resumed.exit_code == 0, resumed.output
    assert store.read_outputs(root)["work"].strip() == "original worktree"


def test_interrupted_publish_retries_twice(tree):
    old, home, project, root, child = tree
    journal_dir = home / "projects" / project_folder(project)
    crash = """
import os, sys
from pathlib import Path
from flow_atelier.cli import app
destination = Path(sys.argv.pop())
link = os.link
def interrupt(src, dst, *args, **kwargs):
    if Path(dst).parent == destination and list(destination.glob("*.jsonl")):
        os._exit(86)
    return link(src, dst, *args, **kwargs)
os.link = interrupt
app()
"""
    proc = subprocess.run([sys.executable, "-c", crash, "migrate", "runs", str(journal_dir)])
    assert proc.returncode == 86
    assert len(list(journal_dir.glob("*.jsonl"))) == 1
    assert (old.base_dir / "flows" / root / "flows" / child).exists()
    retention.cleanup_runs(home, 0)
    assert len(list(journal_dir.glob("*.jsonl"))) == 1
    done = migrate()
    assert done.exit_code == 0, done.output
    assert not (home / ".migrate-runs/pending").exists()
    saved = contents(home)
    assert migrate().exit_code == 0 and contents(home) == saved
    assert FilesystemStore(old.base_dir, home).read_outputs(child)["work"].endswith("\r")


@pytest.mark.parametrize("problem", ["conflict", "writer", "pid", "logs", "steps", "input"])
def test_named_failure_preserves_whole_tree(tree, problem):
    old, home, project, root, child = tree
    source = old.base_dir / "flows" / root / "flows" / child
    if problem == "conflict":
        old2 = FilesystemStore(old.base_dir, home)
        old2.create_flow("inner", {}, flow_id=child, project=project)
    elif problem == "pid":
        old.write_progress(child, Progress(runner_pid=os.getpid(), runner_host=socket.gethostname()))
    elif problem in ("logs", "steps"):
        with (source / f"{problem}.jsonl").open("ab") as stream:
            stream.write(b"broken\n")
    elif problem == "input":
        (source / "input.yaml").write_text("not a mapping")

    def check():
        for args in (("--dry-run",), ()):
            result = migrate(*args)
            assert result.exit_code != 0 and child in result.output, result.output
            assert source.exists()
            assert not (home / "projects" / project_folder(project) / f"{root}.jsonl").exists()

    if problem == "writer":
        with old.lock_writer(child) as acquired:
            assert acquired
            check()
    else:
        check()


def test_migrated_workspace_rejects_redirect(tree):
    old, home, project, root, _ = tree
    assert migrate().exit_code == 0
    path = home / "projects" / project_folder(project) / f"{root}.jsonl"
    lines = path.read_text().splitlines()
    header = json.loads(lines[0])
    header["data"]["legacy_workspace_root"] = str(project)
    lines[0] = json.dumps(header)
    path.write_text("\n".join(lines) + "\n")
    with pytest.raises(ValueError, match="workspace"):
        FilesystemStore(old.base_dir, home).workspace_dir(root)


def test_replay_refuses_changed_blob_before_publication(tree, monkeypatch):
    old, home, project, root, child = tree
    append = RunJournal.append

    def damage(self, kind, data):
        append(self, kind, data)
        if self.path.stem == child and kind == "outputs":
            blob, = (self.side / "outputs/work").iterdir()
            blob.write_bytes(b"damaged")

    monkeypatch.setattr(RunJournal, "append", damage)
    result = migrate()
    assert result.exit_code != 0 and child in result.output and "replay" in result.output
    assert (old.base_dir / "flows" / root / "flows" / child).is_dir()
    assert not (home / "projects").exists()


def test_legacy_log_array_and_detached_question(tree):
    old, home, project, root, child = tree
    source = old.base_dir / "flows" / root / "flows" / child
    log = json.loads((source / "logs.jsonl").read_bytes())
    (source / "logs.json").write_text(json.dumps([log]))
    (source / "logs.jsonl").unlink()
    old.write_progress(child, Progress.model_validate({
        "pending_ask": {"request_id": "q", "question": "Continue?", "session_id": "s",
                        "tool": "harness:demo", "permissions": "edits", "timeout": 5},
    }))
    result = migrate()
    assert result.exit_code == 0, result.output
    store = FilesystemStore(old.base_dir, home)
    assert store.journal(child).header()["project"] == str(project)
    assert store.read_progress(child).pending_ask.request_id == "q"
    assert store.read_logs(child)[0].model_dump(mode="json") == log


def test_migrated_heartbeat_and_retention_preserve_tree_and_checkout(tree, monkeypatch):
    old, home, _, root, child = tree
    now = datetime(2030, 1, 1, tzinfo=UTC)
    monkeypatch.setattr(retention, "_now", lambda: now)
    monkeypatch.setattr(liveness, "time", lambda: now.timestamp())
    progress = old.read_progress(child)
    progress.heartbeat_at = now - timedelta(minutes=3)
    progress.finished_at = None
    progress.status = FlowStatus.running
    progress.runner_pid = None
    progress.pending_ask = PendingAsk(request_id="q", question="Continue?", session_id="s",
        tool="harness:demo", permissions="edits", timeout=5)
    old.write_progress(child, progress)
    checkout = old.workspace_dir(root) / "task"
    checkout.mkdir(parents=True)
    (checkout / "keep").write_text("edits")

    done = migrate()
    assert done.exit_code == 0, done.output
    store = FilesystemStore(old.base_dir, home)
    saved = store.read_progress(child)
    assert saved == progress
    assert liveness.display_status(saved) == "needs_input"
    retention.cleanup_runs(home, 0)
    assert store.list_child_flows(root) == [child]

    saved.pending_ask = None
    store.write_progress(child, saved)
    assert liveness.display_status(store.read_progress(child)) == "crashed"
    retention.cleanup_runs(home, 30)
    assert store.list_child_flows(root) == [child]
    saved.status = FlowStatus.completed
    saved.finished_at = (now - timedelta(days=29)).isoformat()
    store.write_progress(child, saved)
    retention.cleanup_runs(home, 30)
    assert store.list_child_flows(root) == [child]
    now += timedelta(days=2)
    retention.cleanup_runs(home, 30)
    assert store.list_project_flows() == []
    assert (checkout / "keep").read_text() == "edits"
    assert (old.base_dir / "migrated-runs" / root / "flows" / child).is_dir()


def test_cleanup_and_migration_cannot_publish_at_the_same_time(tree):
    old, home, _, root, child = tree
    projects = home / "projects"
    projects.mkdir(parents=True)
    with file_lock(projects / ".cleanup.lock", wait=False) as acquired:
        assert acquired
        result = migrate()
        assert result.exit_code != 0 and "cleanup" in result.output
        assert (old.base_dir / "flows" / root / "flows" / child).exists()
        assert not list(projects.glob("*/*.jsonl"))
    assert migrate().exit_code == 0


def test_live_legacy_pid_with_stale_heartbeat_is_not_migrated(tree):
    old, home, _, root, child = tree
    old.write_progress(child, Progress(runner_pid=os.getpid(), runner_host=socket.gethostname(),
        heartbeat_at=datetime.now(UTC) - timedelta(hours=1)))
    result = migrate()
    assert result.exit_code != 0 and child in result.output
    assert (old.base_dir / "flows" / root).is_dir()
    assert not (home / "projects").exists()


def test_archive_rename_failure_reports_publication_and_history_remains_usable(tree, monkeypatch):
    old, home, project, root, child = tree
    source = old.base_dir / "flows" / root
    rename = Path.rename
    def refuse(path, target):
        if path == source:
            raise PermissionError("source busy")
        return rename(path, target)
    with monkeypatch.context() as patch:
        patch.setattr(Path, "rename", refuse)
        result = migrate()
    assert result.exit_code != 0
    assert "published; source not archived (source busy); rerun atelier migrate runs" in result.output
    other = FilesystemStore(old.base_dir, home).create_flow("other", {}, project=project)
    rows = CliRunner().invoke(app, ["list", "flows", "--json"])
    assert rows.exit_code == 0 and other in rows.stdout
    assert migrate().exit_code == 0
    assert FilesystemStore(old.base_dir, home).list_child_flows(root) == [child]
