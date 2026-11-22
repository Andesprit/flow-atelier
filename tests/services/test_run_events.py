"""Replay parity between project-local records and the versioned journal."""

import json
import os
import stat

import pytest

from flow_atelier.schemas.log import IntermediateStep, LogEntry, StepRecord
from flow_atelier.schemas.progress import PendingAsk, Progress, TaskProgress
from flow_atelier.services.store.events import project_folder
from flow_atelier.services.store.filesystem import FilesystemStore
from flow_atelier.services.store.migrate import migrate_tree


def assert_private_tree(root):
    assert stat.S_IMODE(root.stat().st_mode) == 0o700
    for path in root.rglob("*"):
        assert stat.S_IMODE(path.stat().st_mode) == (0o700 if path.is_dir() else 0o600), path


@pytest.mark.skipif(os.name != "posix", reason="POSIX permission bits")
@pytest.mark.parametrize("storage", ["global", "local", "migrated"])
async def test_run_history_created_with_owner_only_permissions(tmp_path, storage):
    project = tmp_path.resolve()
    base = project / ".atelier"
    home = project / "user"
    mask = os.umask(0)
    try:
        store = FilesystemStore(base, global_dir=home if storage == "global" else None)
        run = store.create_flow("demo", {"private": "input"}, project=project)
        with store.lock_writer(run) as acquired:
            assert acquired
            store.write_progress(run, Progress(status="completed"))
            store.write_outputs(run, {"task": "x" * 100_001})
            store.write_sessions(run, {"task": {"id": "s"}})
            store.append_input(run, "another", "input")
            await store.append_log(
                run, LogEntry(task="task", tool="tool:bash", started_at="a", finished_at="b")
            )
            await store.append_step(
                run, StepRecord(task="task", step=IntermediateStep(kind="tool_call"))
            )
        if storage == "migrated":
            assert migrate_tree(store, home, run, dry_run=False) == [run]
    finally:
        os.umask(mask)
    assert_private_tree(base / "flows" if storage == "local" else home / "projects")


@pytest.mark.skipif(os.name != "posix", reason="POSIX permission bits")
def test_existing_public_history_gets_private_ancestor(tmp_path):
    project = tmp_path.resolve()
    home = project / "user"
    side = journal(home, project, "20260101_deadbeef_demo", None, {"private": "input"}, Progress())
    root = home / "projects"
    root.chmod(0o755)
    store = FilesystemStore(project / ".atelier", global_dir=home)
    assert root.stat().st_mode & 0o777 == 0o700
    assert store.read_input(side.name) == {"private": "input"}


@pytest.mark.skipif(os.name != "posix", reason="POSIX permission bits")
def test_migration_preview_leaves_legacy_permissions_untouched(tmp_path):
    root = tmp_path.resolve()
    base = root / ".atelier"
    home = root / "user"
    store = FilesystemStore(base)
    run = store.create_flow("demo", {})
    store.write_progress(run, Progress(status="completed"))
    history = base / "flows"
    history.chmod(0o755)
    assert migrate_tree(FilesystemStore(base), home, run, dry_run=True) == [run]
    assert stat.S_IMODE(history.stat().st_mode) == 0o755
    assert not home.exists()


def journal(home, project, run, parent, inputs, progress, *events):
    folder = home / "projects" / project_folder(project)
    folder.mkdir(parents=True, exist_ok=True)
    data = dict(
        flow_id=run,
        parent_flow_id=parent,
        project=str(project),
        store=str(project / ".atelier"),
        conduit=run.split("_", 2)[2],
        host="test",
        pid=42,
        steps="steps.jsonl",
        inputs=inputs,
        progress=progress.model_dump(mode="json"),
    )
    rows = [("created", data), *events]
    (folder / f"{run}.jsonl").write_bytes(
        b"".join(
            (json.dumps(dict(version=1, type=kind, data=payload)) + "\n").encode()
            for kind, payload in rows
        )
    )
    return folder / run


async def test_nested_legacy_and_event_replay(tmp_path):
    project = (tmp_path / "a-b").resolve()
    other = tmp_path / "a_b"
    home = tmp_path / "home"
    assert project_folder(project) == project_folder(other)
    legacy = FilesystemStore(project / ".atelier")
    inputs = {"message": "begin"}
    root = legacy.create_flow("outer", {})
    child = legacy.create_flow("ask", inputs, root)
    progress = Progress(
        tasks={"first": TaskProgress(), "chat": TaskProgress()},
        pending_ask=PendingAsk(
            request_id="q",
            question="now?",
            session_id="s",
            tool="harness:demo",
            permissions="edits",
            timeout=3,
        ),
    )
    output = ("é" * 50000 + "\r").encode()
    step = StepRecord(task="chat", step=IntermediateStep(kind="tool_call", tool_name="read"))
    log = LogEntry(task="chat", tool="harness:demo", started_at="a", finished_at="b")
    sessions = {"chat": {"id": "s", "harness": "demo"}}
    legacy.write_progress(child, progress)
    legacy.write_outputs(child, {"chat": output.decode()})
    legacy.write_sessions(child, sessions)
    await legacy.append_log(child, log)
    await legacy.append_step(child, step)

    # A second project can have the same encoded folder.
    foreign = "20260101_deadbeef_other"
    journal(home, other, foreign, None, {}, Progress())
    journal(home, project, root, None, {}, Progress())
    side = journal(
        home,
        project,
        child,
        root,
        inputs,
        Progress(),
        ("progress", progress.model_dump(mode="json")),
        ("sessions", sessions),
        ("log", {"values": log.model_dump(mode="json"), "files": {}}),
        ("outputs", {"values": {}, "files": {"chat": "outputs/chat/1"}}),
    )
    (side / "outputs/chat").mkdir(parents=True)
    (side / "outputs/chat/1").write_bytes(output)
    (side / "steps.jsonl").write_text(step.model_dump_json() + "\n")
    # The two formats are normally in different stores during a migration.
    legacy.base_dir.rename(tmp_path / "legacy")
    legacy = FilesystemStore(tmp_path / "legacy")
    reader = FilesystemStore(project / ".atelier", global_dir=home)
    assert reader.list_project_flows(project) == [root]
    assert reader.list_project_flows(other) == [foreign]
    assert reader.list_child_flows(root) == legacy.list_child_flows(root) == [child]
    assert reader.parent_flow_id(child) == legacy.parent_flow_id(child) == root
    for read in ("read_input", "read_logs", "read_progress", "read_sessions"):
        assert getattr(reader, read)(child) == getattr(legacy, read)(child)
    assert reader.read_steps(child)[0] == legacy.read_steps(child)[0] == [step]
    assert list(reader.read_progress(child).tasks) == ["first", "chat"]
    assert (
        reader.read_outputs(child)["chat"].encode()
        == legacy.read_outputs(child)["chat"].encode()
        == output
    )


def test_refuses_ambiguous_id_and_escaping_output(tmp_path):
    run = "20260101_deadbeef_demo"
    home = tmp_path / "home"
    paths = [tmp_path / "one", tmp_path / "two"]
    reader = FilesystemStore(paths[0] / ".atelier", global_dir=home)
    for path in paths:
        journal(
            home,
            path,
            run,
            None,
            {},
            Progress(),
            ("outputs", {"values": {}, "files": {"chat": "../outside"}}),
        )
    with pytest.raises(ValueError, match="ambiguous"):
        reader.read_outputs(run)
    (home / "projects" / project_folder(paths[1]) / f"{run}.jsonl").unlink()
    with pytest.raises(ValueError, match="output"):
        reader.read_outputs(run)
