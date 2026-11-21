"""Cross-project isolation and compatibility with older journal locations."""

import errno
import os
import socket
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from flow_atelier.core.atelier import Atelier
from flow_atelier.schemas.progress import Progress
from flow_atelier.services.store import events
from flow_atelier.services.store.events import AmbiguousFlowId, RunRecordError, project_folder
from flow_atelier.services.store.retention import cleanup_runs


def test_bad_header_does_not_block_other_projects(tmp_path, monkeypatch, caplog):
    monkeypatch.chdir(tmp_path)
    atlas = Atelier()
    store = atlas.store
    broken = store.create_flow("broken", {}, project=tmp_path / "x-y")
    path = store.journal(broken).path
    root = store.create_flow("ok", {}, project=tmp_path / "x_y")
    child = store.create_flow("inner", {}, root, project=tmp_path / "x_y")
    for run in (root, child):
        store.write_progress(run, Progress(status="completed", finished_at="2000-01-01T00:00:00Z"))
    path.write_bytes(b"oops\n")
    assert [f.flow_id for f in atlas.list_prior_flows()] == [root]
    assert store.list_child_flows(root) == [child]
    assert atlas.get_flow_view(child).parent_flow_id == root
    assert len([m for m in caplog.messages if str(path) in m]) == 1
    with pytest.raises(RunRecordError):
        store.read_progress(broken)
    cleanup_runs(atlas.settings.global_atelier_dir, 1)
    assert not store.list_project_flows(tmp_path / "x_y")
    assert path.exists()


def test_historical_folder_and_changed_project_symlink(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    store = Atelier().store
    project = tmp_path / "original"
    project.mkdir()
    run = store.create_flow("demo", {}, project=project)
    project.rename(tmp_path / "moved")
    project.symlink_to(tmp_path / "moved", target_is_directory=True)
    assert store.journal(run).header()["project"] == str(project)
    assert store.list_project_flows(project) == [run]


def test_old_and_new_long_project_names(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    store = Atelier().store
    path = tmp_path / ("a" * 65) / ("b" * 65) / ("c" * 65)
    encoded = "".join(c if c.isalnum() else "-" for c in str(path))
    assert len(project_folder(path).encode()) < len(encoded.encode())
    run = store.create_flow("demo", {}, project=path)
    journal = store.journal(run)
    # Use a short synthetic absolute path to keep the legacy component under NAME_MAX.
    base = Path(path.anchor) / ("q" * 80) / ("r" * 90)
    short_run = store.create_flow("other", {}, project=base)
    other = store.journal(short_run)
    old = other.path.parent.parent / "".join(c if c.isalnum() else "-" for c in str(base))
    old.mkdir()
    other.path.rename(old / other.path.name)
    assert store.list_project_flows(base) == [short_run]
    assert store.journal(short_run).path.parent == old
    assert store.journal(run).path == journal.path


def test_cached_id_detects_new_duplicate_and_disappearance(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    store = Atelier().store
    run = store.create_flow("demo", {}, project=tmp_path / "a")
    first = store.journal(run)
    store.create_flow("demo", {}, flow_id=run, project=tmp_path / "b")
    with pytest.raises(AmbiguousFlowId):
        store.journal(run)
    first.path.unlink()
    assert store.journal(run).header()["project"] == str(tmp_path / "b")


def test_exclusive_create_without_hardlinks(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    store = Atelier().store

    def unsupported(*args):
        raise OSError(errno.EOPNOTSUPP, "unsupported")

    monkeypatch.setattr(events.os, "link", unsupported)
    run = store.create_flow("demo", {"one": 1}, project=tmp_path)
    with pytest.raises(FileExistsError):
        store.create_flow("demo", {}, flow_id=run, project=tmp_path)
    assert store.read_input(run) == {"one": 1}


@pytest.mark.parametrize("global_record", [False, True])
def test_delete_refuses_a_leased_descendant_without_partial_removal(
    tmp_path, monkeypatch, global_record
):
    monkeypatch.chdir(tmp_path)
    store = Atelier().store
    target = tmp_path if global_record else None
    root = store.create_flow("outer", {}, project=target)
    first = store.create_flow("one", {}, root, project=target)
    second = store.create_flow("two", {}, root, project=target)
    with store.lock_writer(second) as held:
        assert held
        with pytest.raises(ValueError, match="active writer"):
            store.delete_flow(root)
        assert set(store.list_child_flows(root)) == {first, second}
    assert store.delete_flow(root)


def test_retention_of_crashed_runs(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    atlas = Atelier()
    old = datetime.now(UTC) - timedelta(days=32)
    young = datetime.now(UTC) - timedelta(days=20)
    # An unreaped PID would still appear alive on POSIX.
    child = subprocess.Popen([sys.executable, "-c", "pass"])
    child.wait()

    def record(name, beat, pid):
        run = atlas.store.create_flow(name, {}, project=tmp_path)
        atlas.store.write_progress(
            run,
            Progress(
                started_at=old.isoformat(),
                heartbeat_at=beat,
                runner_pid=pid,
                runner_host=socket.gethostname(),
            ),
        )
        return run

    dead = record("dead", None, child.pid)
    stale = record("stale", old, None)
    recent = record("recent", young, None)
    owned = record("owned", old, os.getpid())
    with atlas.store.lock_writer(owned):
        cleanup_runs(atlas.settings.global_atelier_dir, 30)
    assert atlas.store.journal(dead) is None and atlas.store.journal(stale) is None
    assert atlas.store.journal(recent) and atlas.store.journal(owned)


def test_windows_blocking_lock_retries_contention(monkeypatch):
    from types import SimpleNamespace

    calls = []

    def locking(fd, mode, length):
        calls.append((fd, mode, length))
        if len(calls) <= 12:
            raise PermissionError(errno.EACCES, "locked")

    monkeypatch.setitem(sys.modules, "msvcrt", SimpleNamespace(locking=locking, LK_NBLCK=2))
    import time

    monkeypatch.setattr(time, "sleep", lambda seconds: None)
    events._windows_lock(7, wait=True)
    assert calls == [(7, 2, 1)] * 13
