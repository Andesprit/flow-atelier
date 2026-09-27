"""Filesystem store tests."""
import json
from unittest import mock

import pytest
import yaml

from flow_atelier.schemas.log import (
    IntermediateStep,
    LogEntry,
    StepKind,
    StepRecord,
)
from flow_atelier.schemas.progress import FlowStatus, Progress, TaskProgress, TaskStatus
from flow_atelier.services.store.filesystem import FilesystemStore
from flow_atelier.services.store.filesystem import logger as fs_logger

CONDUIT_YAML = """
name: hello
description: Say hello
tasks:
  - greet:
      description: greet
      task: "echo hi"
      tool: tool:bash
      depends_on: []
"""


@pytest.fixture
def store(tmp_path):
    """Provide a FilesystemStore seeded with a sample hello conduit.

    :param tmp_path: pytest temp directory fixture.
    """
    s = FilesystemStore(
        tmp_path / ".atelier", global_dir=tmp_path / "global_atelier"
    )
    conduit_dir = s.base_dir / "conduits" / "hello"
    conduit_dir.mkdir(parents=True)
    (conduit_dir / "conduit.yaml").write_text(CONDUIT_YAML)
    return s


def test_global_conduits_dir_created_lazily(tmp_path):
    """Verify the global conduits dir is created lazily on store init.

    :param tmp_path: pytest temp directory fixture.
    """
    global_dir = tmp_path / "g_atelier"
    FilesystemStore(tmp_path / ".atelier", global_dir=global_dir)
    assert (global_dir / "conduits").is_dir()
    assert not (global_dir / "flows").exists()


def test_global_dir_none_is_allowed(tmp_path):
    """Store works without a global dir (backwards-compatible path).

    :param tmp_path: pytest temp directory fixture.
    """
    s = FilesystemStore(tmp_path / ".atelier")
    assert s.list_conduits() == []


def test_read_conduit(store):
    """Verify read_conduit returns the parsed conduit by name.

    :param store: FilesystemStore fixture.
    """
    c = store.read_conduit("hello")
    assert c.name == "hello"
    assert c.tasks[0].name == "greet"


def test_read_conduit_malformed_yaml_raises_one_line(store):
    """Verify broken YAML raises a one-line ValueError, not a raw traceback.

    :param store: FilesystemStore fixture.
    """
    (store.base_dir / "conduits" / "hello" / "conduit.yaml").write_text(
        "name: hello\ntasks: [unclosed\n"
    )
    with pytest.raises(ValueError, match="hello: invalid YAML"):
        store.read_conduit("hello")


def test_list_conduits(store):
    """Verify list_conduits returns the names of available conduits.

    :param store: FilesystemStore fixture.
    """
    assert store.list_conduits() == ["hello"]


def test_create_flow_layout(store):
    """Verify create_flow scaffolds the expected on-disk layout.

    :param store: FilesystemStore fixture.
    """
    flow_id = store.create_flow("hello", inputs={"a": 1})
    flow_dir = store._flow_dir(flow_id)
    assert (flow_dir / "input.yaml").exists()
    assert (flow_dir / "logs.jsonl").exists()
    assert (flow_dir / "progress.json").exists()
    assert (flow_dir / "flows").is_dir()
    data = yaml.safe_load((flow_dir / "input.yaml").read_text())
    assert data == {"a": 1}


def test_list_flows(store):
    """Verify list_flows returns ids for all created flows.

    :param store: FilesystemStore fixture.
    """
    fid1 = store.create_flow("hello", {})
    fid2 = store.create_flow("hello", {})
    listed = store.list_flows()
    assert fid1 in listed and fid2 in listed


def test_nested_flow_under_parent(store):
    """Verify child flows nest under the parent flow's flows directory.

    :param store: FilesystemStore fixture.
    """
    parent = store.create_flow("hello", {})
    child = store.create_flow("hello", {}, parent_flow_id=parent)
    child_dir = store._flow_dir(child)
    assert child_dir.parent.name == "flows"
    assert child_dir.parent.parent == store._flow_dir(parent)


async def test_append_log_and_read(store):
    """Verify append_log appends entries and read returns them in order.

    :param store: FilesystemStore fixture.
    """
    fid = store.create_flow("hello", {})
    e1 = LogEntry(
        task="greet",
        tool="tool:bash",
        command="echo hi",
        stdout="hi\n",
        output="hi\n",
        exit_code=0,
        started_at="2026-04-12T10:00:00Z",
        finished_at="2026-04-12T10:00:00Z",
    )
    await store.append_log(fid, e1)
    await store.append_log(fid, e1)
    logs_path = store._flow_dir(fid) / "logs.jsonl"
    logs = [json.loads(line) for line in logs_path.read_text().splitlines()]
    assert len(logs) == 2
    assert logs[0]["task"] == "greet"
    assert [e.task for e in store.read_logs(fid)] == ["greet", "greet"]


async def test_read_logs_legacy_json_fallback(store):
    """Verify read_logs falls back to a legacy logs.json array.

    :param store: FilesystemStore fixture.
    """
    fid = store.create_flow("hello", {})
    flow_dir = store._flow_dir(fid)
    (flow_dir / "logs.jsonl").unlink()
    legacy = [
        LogEntry(
            task="greet",
            tool="tool:bash",
            command="echo hi",
            stdout="hi\n",
            output="hi\n",
            exit_code=0,
            started_at="2026-04-12T10:00:00Z",
            finished_at="2026-04-12T10:00:00Z",
        ).model_dump()
    ]
    (flow_dir / "logs.json").write_text(json.dumps(legacy))
    logs = store.read_logs(fid)
    assert len(logs) == 1
    assert logs[0].task == "greet"


def test_progress_roundtrip(store):
    """Verify write_progress and read_progress round-trip task state.

    :param store: FilesystemStore fixture.
    """
    fid = store.create_flow("hello", {})
    p = Progress(
        status=FlowStatus.running,
        tasks={"greet": TaskProgress(status=TaskStatus.completed)},
    )
    store.write_progress(fid, p)
    restored = store.read_progress(fid)
    assert restored.tasks["greet"].status == TaskStatus.completed


def test_append_input_overwrites(store):
    """Verify append_input adds new keys and overwrites existing ones.

    :param store: FilesystemStore fixture.
    """
    fid = store.create_flow("hello", {"existing": "val"})
    store.append_input(fid, "new", "added")
    store.append_input(fid, "existing", "changed")
    data = store.read_input(fid)
    assert data == {"existing": "changed", "new": "added"}


def test_read_input_returns_empty_for_non_mapping(store):
    """A non-map input.yaml (bare list/scalar) degrades to {} rather than
    handing the engine a value it would mis-merge as inputs.

    :param store: FilesystemStore fixture.
    """
    fid = store.create_flow("hello", {})
    (store._flow_dir(fid) / "input.yaml").write_text("- just\n- a\n- list\n")
    assert store.read_input(fid) == {}


def test_flow_dir_resolves_top_level_without_scan(store, monkeypatch):
    """A fresh store instance must resolve top-level flow dirs from the
    deterministic path, never via the recursive rglob fallback.

    :param store: seeded FilesystemStore fixture.
    :param monkeypatch: pytest monkeypatch fixture.
    """
    fid = store.create_flow("hello", {})
    fresh = FilesystemStore(store.base_dir)

    def _no_scan(self, pattern):
        raise AssertionError("rglob fallback must not be used for top-level flows")

    monkeypatch.setattr(type(fresh.base_dir), "rglob", _no_scan)
    assert fresh._flow_dir(fid).name == fid


def test_flow_dir_falls_back_to_scan_for_nested(store):
    """Nested child flows are still found via the recursive fallback.

    :param store: seeded FilesystemStore fixture.
    """
    parent = store.create_flow("hello", {})
    child = store.create_flow("hello", {}, parent_flow_id=parent)
    fresh = FilesystemStore(store.base_dir)
    assert fresh._flow_dir(child).parent.name == "flows"


def test_flow_dir_never_resolves_a_retained_workspace(store):
    """A run's checkouts share its id but are not a record of it.

    ``workspaces/<flow_id>`` is deliberately outside ``flows/`` so that
    :meth:`delete_flow` cannot reach it. An unrestricted scan would undo that
    the moment the record itself was gone, and delete an agent's edits.

    :param store: seeded FilesystemStore fixture.
    """
    fid = store.create_flow("hello", {})
    kept = store.workspace_dir(fid) / "writer_a"
    kept.mkdir(parents=True)
    (kept / "NOTES.md").write_text("an agent's work\n", encoding="utf-8")

    assert store.delete_flow(fid) is True
    fresh = FilesystemStore(store.base_dir)
    with pytest.raises(FileNotFoundError):
        fresh._flow_dir(fid)
    assert fresh.delete_flow(fid) is False
    assert (kept / "NOTES.md").read_text(encoding="utf-8") == "an agent's work\n"


def test_read_unknown_conduit_raises(store):
    """Verify read_conduit raises FileNotFoundError for unknown names.

    :param store: FilesystemStore fixture.
    """
    with pytest.raises(FileNotFoundError):
        store.read_conduit("nonexistent")


GLOBAL_DEPLOY_YAML = """
name: deploy
description: Global deploy
tasks:
  - step:
      description: step
      task: "echo deploying"
      tool: tool:bash
      depends_on: []
"""

PROJECT_HELLO_OVERRIDE_YAML = """
name: hello
description: Project-specific hello
tasks:
  - greet:
      description: local
      task: "echo local"
      tool: tool:bash
      depends_on: []
"""


def _write_conduit(root, name, yaml_text):
    """Write a conduit YAML file under root/conduits/name/conduit.yaml.

    :param root: root directory containing the conduits folder.
    :param name: conduit directory name.
    :param yaml_text: YAML content to write.
    """
    d = root / "conduits" / name
    d.mkdir(parents=True)
    (d / "conduit.yaml").write_text(yaml_text)


def test_read_conduit_falls_back_to_global(tmp_path):
    """Verify read_conduit falls back to the global directory.

    :param tmp_path: pytest temp directory fixture.
    """
    project = tmp_path / ".atelier"
    global_dir = tmp_path / "global_atelier"
    s = FilesystemStore(project, global_dir=global_dir)
    _write_conduit(global_dir, "deploy", GLOBAL_DEPLOY_YAML)
    c = s.read_conduit("deploy")
    assert c.name == "deploy"
    assert c.description == "Global deploy"


def test_project_conduit_shadows_global(tmp_path):
    """Verify a project-local conduit shadows a same-named global one.

    :param tmp_path: pytest temp directory fixture.
    """
    project = tmp_path / ".atelier"
    global_dir = tmp_path / "global_atelier"
    s = FilesystemStore(project, global_dir=global_dir)
    _write_conduit(global_dir, "hello", GLOBAL_DEPLOY_YAML.replace("deploy", "hello"))
    _write_conduit(project, "hello", PROJECT_HELLO_OVERRIDE_YAML)
    c = s.read_conduit("hello")
    assert c.description == "Project-specific hello"


def test_list_conduits_unions_project_and_global(tmp_path):
    """Verify list_conduits returns the union of project and global conduits.

    :param tmp_path: pytest temp directory fixture.
    """
    project = tmp_path / ".atelier"
    global_dir = tmp_path / "global_atelier"
    s = FilesystemStore(project, global_dir=global_dir)
    _write_conduit(project, "hello", PROJECT_HELLO_OVERRIDE_YAML)
    _write_conduit(global_dir, "deploy", GLOBAL_DEPLOY_YAML)
    assert s.list_conduits() == ["deploy", "hello"]


def test_list_conduits_with_source_project_shadows_global(tmp_path):
    """Verify list_conduits_with_source reports project shadowing for duplicates.

    :param tmp_path: pytest temp directory fixture.
    """
    project = tmp_path / ".atelier"
    global_dir = tmp_path / "global_atelier"
    s = FilesystemStore(project, global_dir=global_dir)
    _write_conduit(
        global_dir, "hello", GLOBAL_DEPLOY_YAML.replace("deploy", "hello")
    )
    _write_conduit(global_dir, "deploy", GLOBAL_DEPLOY_YAML)
    _write_conduit(project, "hello", PROJECT_HELLO_OVERRIDE_YAML)
    entries = s.list_conduits_with_source()
    as_dict = dict(entries)
    assert as_dict == {"hello": "project", "deploy": "global"}


def test_read_missing_from_both_raises(tmp_path):
    """Verify read_conduit raises when missing from both project and global.

    :param tmp_path: pytest temp directory fixture.
    """
    project = tmp_path / ".atelier"
    global_dir = tmp_path / "global_atelier"
    s = FilesystemStore(project, global_dir=global_dir)
    with pytest.raises(FileNotFoundError):
        s.read_conduit("nope")


# --------------------------------------------------------------- conduit_dir


def test_conduit_dir_returns_project_path(tmp_path):
    """conduit_dir returns the project directory when the conduit lives there.

    :param tmp_path: pytest temp directory fixture.
    """
    project = tmp_path / ".atelier"
    global_dir = tmp_path / "global_atelier"
    s = FilesystemStore(project, global_dir=global_dir)
    _write_conduit(project, "hello", PROJECT_HELLO_OVERRIDE_YAML)
    assert s.conduit_dir("hello") == project / "conduits" / "hello"


def test_conduit_dir_falls_back_to_global_path(tmp_path):
    """conduit_dir returns the global directory when only the global exists.

    :param tmp_path: pytest temp directory fixture.
    """
    project = tmp_path / ".atelier"
    global_dir = tmp_path / "global_atelier"
    s = FilesystemStore(project, global_dir=global_dir)
    _write_conduit(global_dir, "deploy", GLOBAL_DEPLOY_YAML)
    assert s.conduit_dir("deploy") == global_dir / "conduits" / "deploy"


def test_conduit_dir_project_shadows_global(tmp_path):
    """conduit_dir prefers the project directory over a same-named global one.

    :param tmp_path: pytest temp directory fixture.
    """
    project = tmp_path / ".atelier"
    global_dir = tmp_path / "global_atelier"
    s = FilesystemStore(project, global_dir=global_dir)
    _write_conduit(global_dir, "hello", GLOBAL_DEPLOY_YAML.replace("deploy", "hello"))
    _write_conduit(project, "hello", PROJECT_HELLO_OVERRIDE_YAML)
    assert s.conduit_dir("hello") == project / "conduits" / "hello"


def test_conduit_dir_missing_raises(tmp_path):
    """conduit_dir raises FileNotFoundError when missing from both stores.

    :param tmp_path: pytest temp directory fixture.
    """
    project = tmp_path / ".atelier"
    global_dir = tmp_path / "global_atelier"
    s = FilesystemStore(project, global_dir=global_dir)
    with pytest.raises(FileNotFoundError):
        s.conduit_dir("nope")


# --------------------------------------------------------------- write/delete


from flow_atelier.schemas.conduit import Conduit


def _build_conduit(name: str, description: str = "d") -> Conduit:
    """Build a minimal Conduit fixture with a single echo task.

    :param name: conduit name.
    :param description: conduit description.
    """
    return Conduit.model_validate(
        {
            "name": name,
            "description": description,
            "tasks": [
                {
                    "name": "echo",
                    "description": "echo",
                    "task": "echo hi",
                    "tool": "tool:bash",
                    "depends_on": [],
                }
            ],
        }
    )


def test_write_conduit_creates_yaml_and_round_trips(tmp_path):
    """Verify write_conduit creates the YAML file and round-trips on read.

    :param tmp_path: pytest temp directory fixture.
    """
    s = FilesystemStore(tmp_path / ".atelier")
    c = _build_conduit("release_notes")
    s.write_conduit(c)
    assert (s.base_dir / "conduits" / "release_notes" / "conduit.yaml").exists()
    restored = s.read_conduit("release_notes")
    assert restored.name == "release_notes"
    assert restored.tasks[0].task == "echo hi"


def test_write_conduit_overwrites_existing(tmp_path):
    """Verify write_conduit overwrites an existing conduit's YAML.

    :param tmp_path: pytest temp directory fixture.
    """
    s = FilesystemStore(tmp_path / ".atelier")
    s.write_conduit(_build_conduit("x", description="old"))
    s.write_conduit(_build_conduit("x", description="new"))
    restored = s.read_conduit("x")
    assert restored.description == "new"


def test_delete_conduit_removes_directory(tmp_path):
    """Verify delete_conduit removes the conduit directory entirely.

    :param tmp_path: pytest temp directory fixture.
    """
    s = FilesystemStore(tmp_path / ".atelier")
    s.write_conduit(_build_conduit("x"))
    assert s.delete_conduit("x") is True
    assert not (s.base_dir / "conduits" / "x").exists()
    with pytest.raises(FileNotFoundError):
        s.read_conduit("x")


def test_delete_conduit_idempotent_returns_false(tmp_path):
    """Verify delete_conduit returns False when the conduit does not exist.

    :param tmp_path: pytest temp directory fixture.
    """
    s = FilesystemStore(tmp_path / ".atelier")
    assert s.delete_conduit("nope") is False


# --------------------------------------------------------------- delete_flow


def test_delete_flow_removes_dir_and_evicts_caches(store):
    """delete_flow removes the directory and evicts both in-memory caches.

    :param store: FilesystemStore fixture.
    """
    fid = store.create_flow("hello", {})
    store._lock_for(fid)  # populate _log_locks for this id
    flow_dir = store._flow_dir(fid)
    assert flow_dir.exists()

    assert store.delete_flow(fid) is True
    assert not flow_dir.exists()
    assert fid not in store._flow_paths
    assert fid not in store._log_locks


def test_delete_flow_unknown_returns_false(store):
    """delete_flow returns False for an unknown flow id.

    :param store: FilesystemStore fixture.
    """
    assert store.delete_flow("20260101_deadbeef_hello") is False


def test_delete_flow_removes_nested_children(store):
    """Deleting a parent removes its nested child subtree and evicts the
    child's cached path/lock entries.

    :param store: FilesystemStore fixture.
    """
    parent = store.create_flow("hello", {})
    child = store.create_flow("hello", {}, parent_flow_id=parent)
    store._lock_for(child)
    child_dir = store._flow_dir(child)
    assert child_dir.exists()

    assert store.delete_flow(parent) is True
    assert not child_dir.exists()
    assert parent not in store._flow_paths
    assert child not in store._flow_paths
    assert child not in store._log_locks


def test_read_logs_warns_on_corrupt_interior_line(store, caplog):
    """A corrupt interior log line is dropped with a warning, not silently.

    :param store: FilesystemStore fixture.
    :param caplog: pytest log-capture fixture.
    """
    fid = store.create_flow("hello", {})
    good = LogEntry(
        task="greet", tool="tool:bash", command="echo hi",
        output="hi", exit_code=0,
        started_at="2026-04-12T10:00:00Z", finished_at="2026-04-12T10:00:00Z",
    )
    line = good.model_dump_json()
    logs_path = store._flow_dir(fid) / "logs.jsonl"
    logs_path.write_text(f"{line}\n{{not json\n{line}\n")
    with caplog.at_level("WARNING"):
        entries = store.read_logs(fid)
    assert [e.task for e in entries] == ["greet", "greet"]
    assert any("unreadable line" in r.message for r in caplog.records)


def test_read_logs_silent_on_truncated_trailing_line(store, caplog):
    """A truncated trailing line (crash mid-write) is dropped without warning.

    :param store: FilesystemStore fixture.
    :param caplog: pytest log-capture fixture.
    """
    fid = store.create_flow("hello", {})
    good = LogEntry(
        task="greet", tool="tool:bash", command="echo hi",
        output="hi", exit_code=0,
        started_at="2026-04-12T10:00:00Z", finished_at="2026-04-12T10:00:00Z",
    )
    line = good.model_dump_json()
    logs_path = store._flow_dir(fid) / "logs.jsonl"
    logs_path.write_text(f"{line}\n{{truncat")
    with caplog.at_level("WARNING"):
        entries = store.read_logs(fid)
    assert [e.task for e in entries] == ["greet"]
    assert not any("unreadable line" in r.message for r in caplog.records)


class TestReadSteps:
    """Live step records must tail correctly while the flow is still writing."""

    @staticmethod
    def _record(text: str) -> StepRecord:
        """Build a StepRecord carrying ``text`` as thinking.

        :param text: thinking text to embed.
        """
        return StepRecord(
            task="a",
            iteration=1,
            step=IntermediateStep(kind=StepKind.thinking, text=text),
        )

    async def test_offset_returns_only_new_records(self, tmp_path) -> None:
        """``offset`` lets a poller consume only what it has not seen.

        :param tmp_path: pytest temp directory fixture.
        """
        store = FilesystemStore(tmp_path / ".atelier")
        flow_id = store.create_flow("c", {})
        for i in range(3):
            await store.append_step(flow_id, self._record(f"s{i}"))

        first, offset = store.read_steps(flow_id)
        assert [r.step.text for r in first] == ["s0", "s1", "s2"]

        # Nothing new yet.
        assert store.read_steps(flow_id, offset) == ([], offset)

        await store.append_step(flow_id, self._record("s3"))
        fresh, offset = store.read_steps(flow_id, offset)
        assert [r.step.text for r in fresh] == ["s3"]
        assert store.read_steps(flow_id, offset) == ([], offset)

    async def test_offset_skips_the_bytes_already_consumed(self, tmp_path) -> None:
        """A poll resuming from ``offset`` must not re-read the earlier bytes.

        ``steps.jsonl`` has no rotation, so pulling it through in full on
        every poll gets steadily more expensive as a flow runs. Proven by
        poisoning the consumed prefix: a reader that seeks never sees it, a
        reader that starts from zero has to complain about it.

        :param tmp_path: pytest temp directory fixture.
        """
        store = FilesystemStore(tmp_path / ".atelier")
        flow_id = store.create_flow("c", {})
        await store.append_step(flow_id, self._record("consumed"))
        _, offset = store.read_steps(flow_id)
        await store.append_step(flow_id, self._record("tail"))

        path = tmp_path / ".atelier" / "flows" / flow_id / "steps.jsonl"
        raw = bytearray(path.read_bytes())
        raw[0 : offset - 1] = b"X" * (offset - 1)  # keep the newline in place
        path.write_bytes(bytes(raw))

        with mock.patch.object(fs_logger, "warning") as warned:
            fresh, _ = store.read_steps(flow_id, offset)
        assert [r.step.text for r in fresh] == ["tail"]
        assert not warned.called, "resumed poll re-read the consumed prefix"

        # Control: from byte zero the poisoned prefix is unmissable.
        with mock.patch.object(fs_logger, "warning") as warned:
            store.read_steps(flow_id)
        assert warned.called

    async def test_corrupt_line_is_skipped_not_fatal(self, tmp_path) -> None:
        """One unreadable complete line must not hide every step after it.

        Stopping at the first bad line keeps a poller's position exact, but
        a genuinely corrupt line is never completed, so the reader would
        stall there permanently and silently lose the rest of the flow.

        :param tmp_path: pytest temp directory fixture.
        """
        store = FilesystemStore(tmp_path / ".atelier")
        flow_id = store.create_flow("c", {})
        await store.append_step(flow_id, self._record("before"))

        path = tmp_path / ".atelier" / "flows" / flow_id / "steps.jsonl"
        with path.open("a", encoding="utf-8") as f:
            f.write("{not json at all}\n")
        await store.append_step(flow_id, self._record("after"))

        records, offset = store.read_steps(flow_id)
        assert [r.step.text for r in records] == ["before", "after"]
        # And the position stepped over the bad line rather than parking on it.
        assert offset == path.stat().st_size
        assert store.read_steps(flow_id, offset) == ([], offset)

    async def test_non_ascii_step_text_round_trips(self, tmp_path) -> None:
        """Steps are written UTF-8, so they must be read back UTF-8.

        Reading with the locale default decodes as cp1252 on a stock Windows
        console and raises on the first smart quote an agent emits.

        :param tmp_path: pytest temp directory fixture.
        """
        store = FilesystemStore(tmp_path / ".atelier")
        flow_id = store.create_flow("c", {})
        await store.append_step(flow_id, self._record("café — 你好"))

        records, _ = store.read_steps(flow_id)
        assert [r.step.text for r in records] == ["café — 你好"]

    async def test_partial_trailing_line_is_retried_not_skipped(
        self, tmp_path
    ) -> None:
        """A half-written final line must not desync a poller's position.

        The file is appended to while it is being read, so a torn trailing
        line is the normal case, not corruption. It has to be withheld until
        complete — counting it as consumed would drop the step forever.

        :param tmp_path: pytest temp directory fixture.
        """
        store = FilesystemStore(tmp_path / ".atelier")
        flow_id = store.create_flow("c", {})
        await store.append_step(flow_id, self._record("complete"))

        path = tmp_path / ".atelier" / "flows" / flow_id / "steps.jsonl"
        torn = json.dumps(self._record("late").model_dump())
        head, tail = torn[:20], torn[20:]
        with path.open("a", encoding="utf-8") as f:
            f.write(head)  # torn mid-write

        seen, offset = store.read_steps(flow_id)
        assert [r.step.text for r in seen] == ["complete"]

        # The poller advances only past complete lines, so once the rest of
        # the write lands the withheld record is picked up — once, not twice.
        with path.open("a", encoding="utf-8") as f:
            f.write(tail + "\n")
        fresh, offset = store.read_steps(flow_id, offset)
        assert [r.step.text for r in fresh] == ["late"]
        assert store.read_steps(flow_id, offset) == ([], offset)


class TestLogEncoding:
    """`logs.jsonl` is appended as UTF-8, so it must be read back as UTF-8.

    Reading with the locale default decodes as cp1252 on a stock Windows
    console and raises on the first non-ASCII byte a task's output contains.
    """

    async def test_non_ascii_log_entry_round_trips(self, tmp_path) -> None:
        """Verify a log entry with non-ASCII output survives a write/read cycle.

        :param tmp_path: pytest temp directory fixture.
        """
        store = FilesystemStore(tmp_path / ".atelier")
        flow_id = store.create_flow("c", {})
        await store.append_log(
            flow_id,
            LogEntry(
                task="a",
                tool="tool:bash",
                command="echo",
                exit_code=0,
                output="café — 你好 — ✓",
                stdout="café — 你好 — ✓",
                stderr="",
                started_at="2026-04-12T10:00:00Z",
                finished_at="2026-04-12T10:00:00Z",
            ),
        )
        assert [e.output for e in store.read_logs(flow_id)] == ["café — 你好 — ✓"]

    def test_legacy_json_log_is_read_as_utf8(self, tmp_path) -> None:
        """Verify the pre-JSONL fallback path also decodes as UTF-8.

        :param tmp_path: pytest temp directory fixture.
        """
        store = FilesystemStore(tmp_path / ".atelier")
        flow_id = store.create_flow("c", {})
        flow_dir = tmp_path / ".atelier" / "flows" / flow_id
        (flow_dir / "logs.jsonl").unlink()
        (flow_dir / "logs.json").write_text(
            json.dumps(
                [
                    {
                        "task": "a",
                        "tool": "tool:bash",
                        "command": "echo",
                        "exit_code": 0,
                        "output": "café — 你好",
                        "stdout": "café — 你好",
                        "stderr": "",
                        "started_at": "2026-04-12T10:00:00Z",
                        "finished_at": "2026-04-12T10:00:00Z",
                    }
                ]
            ),
            encoding="utf-8",
        )
        assert [e.output for e in store.read_logs(flow_id)] == ["café — 你好"]


class TestFlowIdIsAPathComponent:
    """A flow id reaches the store from a URL segment; it must not traverse."""

    @pytest.mark.parametrize(
        "flow_id",
        [
            "..",
            ".",
            "",
            "../conduits",
            "20250101_aaaaaaaa_../../etc",   # well-formed prefix, escaping tail
            "20250101_aaaaaaaa_..%2F..",
            "*",                              # a glob would make rglob walk the store
            "no_such_flow",
        ],
    )
    def test_unsafe_flow_id_is_not_found(self, store, flow_id):
        """_flow_dir refuses anything that isn't a well-formed flow id.

        :param store: FilesystemStore fixture.
        :param flow_id: candidate identifier that must be rejected.
        """
        with pytest.raises(FileNotFoundError):
            store._flow_dir(flow_id)

    def test_traversal_cannot_read_a_neighbouring_flows_file(self, store):
        """A dot-segment must not reach logs.jsonl one level above flows/.

        :param store: FilesystemStore fixture.
        """
        (store.base_dir / "logs.jsonl").write_text(
            json.dumps(
                {
                    "task": "t",
                    "tool": "tool:bash",
                    "command": "c",
                    "exit_code": 0,
                    "output": "SECRET",
                    "stdout": "SECRET",
                    "stderr": "",
                    "started_at": "2026-04-12T10:00:00Z",
                    "finished_at": "2026-04-12T10:00:00Z",
                }
            )
            + "\n"
        )
        with pytest.raises(FileNotFoundError):
            store.read_logs("..")

    def test_generated_ids_still_resolve(self, store):
        """The guard must not reject the ids the store itself generates.

        :param store: FilesystemStore fixture.
        """
        flow_id = store.create_flow("hello", {})
        assert store._flow_dir(flow_id).is_dir()

    def test_create_flow_rejects_an_escaping_id(self, store):
        """A caller-supplied id must not mkdir its way out of the store.

        :param store: FilesystemStore fixture.
        """
        with pytest.raises(ValueError, match="unsafe flow id"):
            store.create_flow("hello", {}, flow_id="20250101_aaaaaaaa_../../pwned")
        assert not (store.base_dir.parent.parent / "pwned").exists()

    def test_create_flow_rejects_an_unsafe_conduit_name(self, store):
        """An unsafe conduit name yields an unsafe generated id; reject it.

        :param store: FilesystemStore fixture.
        """
        with pytest.raises(ValueError, match="unsafe flow id"):
            store.create_flow("../../pwned", {})
