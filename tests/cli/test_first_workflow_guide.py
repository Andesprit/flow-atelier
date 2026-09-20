"""Runs `docs/first-workflow.md` for real, so the guide cannot drift.

The guide promises a specific sequence of observable facts: a success, a
failure that stops the tasks downstream, a resume that keeps completed work,
and an `--again` that redoes it. This test extracts the guide's own ```bash
blocks, executes them in order in one shell with error checking on, and then
reads the saved flows back to confirm each of those claims. It asserts on the
recorded history, never on Rich borders, durations or random flow ids.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from flow_atelier.services.executor.bash import to_bash_path
from flow_atelier.services.store.filesystem import FilesystemStore
from tests._shell import CLI as _CLI
from tests._shell import record_path, run_script, write_shim

_REPO_ROOT = Path(__file__).resolve().parents[2]
_GUIDE = _REPO_ROOT / "docs" / "first-workflow.md"
_BASH_BLOCK = re.compile(r"^```bash\n(.*?)^```$", re.MULTILINE | re.DOTALL)
# Every `atelier run` variant ends its transcript with this line.
_FLOW_ID_LINE = re.compile(r"^flow_id: (\S+)$", re.MULTILINE)
# The guide walks through: create the workspace, write the conduit, write the
# script, check+plan, run, read back, break, fail, diagnose, resume, again,
# final read-back, and the explicit-flow-id one-liner it closes with.
_EXPECTED_BLOCKS = 13


def _guide_script() -> str:
    """Return the guide's bash blocks, concatenated in document order.

    :returns: one shell script, with error checking enabled up front.
    """
    blocks = [m.group(1) for m in _BASH_BLOCK.finditer(_GUIDE.read_text(encoding="utf-8"))]
    assert len(blocks) >= _EXPECTED_BLOCKS, (
        f"only {len(blocks)} bash blocks found in {_GUIDE.name}; "
        "the walkthrough is incomplete or the fences changed"
    )
    body = "\n".join(blocks)
    for required in ("atelier run --resume latest", "atelier run --again"):
        assert required in body, f"the guide no longer runs `{required}`"
    # -e proves the copyable blocks survive a reader's own error checking; the
    # intentionally-failing step has to handle itself, not rely on leniency.
    return "set -eu\n" + body + "\n" + record_path("tutorial_dir", "TUTORIAL_MARKER")


@pytest.fixture
def guide_env(tmp_path):
    """An isolated environment whose `atelier` is this checkout's CLI.

    :yields: ``(env, marker)`` — the child environment and the file the script
        writes its workspace path into.
    """
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    write_shim(bin_dir)
    marker = tmp_path / "workspace-path"

    env = {k: v for k, v in os.environ.items() if not k.startswith("ATELIER_")}
    env["ATELIER_GLOBAL_ATELIER_DIR"] = str(tmp_path / "global")
    env["ATELIER_NO_UPDATE_CHECK"] = "1"
    env["PATH"] = f"{bin_dir}{os.pathsep}{env.get('PATH', '')}"
    env["TUTORIAL_MARKER"] = to_bash_path(marker)
    yield env, marker
    # The guide deliberately never deletes its own workspace, and `mktemp -d`
    # puts it outside tmp_path, so the test is what cleans up after it.
    if marker.exists():
        shutil.rmtree(Path(marker.read_text().strip()).parent, ignore_errors=True)


def _log_shape(store: FilesystemStore, flow_id: str) -> list[tuple[str, bool]]:
    """Summarise a flow's saved log as ``(task, succeeded)`` in write order.

    :param store: store rooted at the tutorial workspace.
    :param flow_id: flow whose log to summarise.
    :returns: one pair per persisted task iteration.
    """
    return [(e.task, e.exit_code == 0) for e in store.read_logs(flow_id)]


def test_readme_points_at_a_guide_that_exists():
    """The discovery link beside the quickstart resolves to this file."""
    readme = (_REPO_ROOT / "README.md").read_text(encoding="utf-8")
    assert "(docs/first-workflow.md)" in readme
    assert _GUIDE.is_file()


def test_the_guide_runs_end_to_end_and_records_what_it_claims(guide_env, tmp_path):
    """Execute the published commands, then verify the saved history."""
    env, marker = guide_env
    run = run_script(
        _guide_script(), tmp_path / "walkthrough.sh", tmp_path, env, timeout=240
    )
    assert run.returncode == 0, f"stdout:\n{run.stdout}\nstderr:\n{run.stderr}"
    out = run.stdout

    # The deliberate failure happened, and a silent success could not pass as one.
    assert "UNEXPECTED" not in out, out
    assert "the run failed on purpose" in out
    # The guide's stated intermediate observations, as the commands printed them.
    assert '"ok": true' in out
    assert '"status": "failed"' in out
    assert '"status": "cancelled"' in out

    # run / run --resume / run --again all end with `flow_id:`; the middle two
    # are the same run, the last is a new one.
    ids = _FLOW_ID_LINE.findall(out)
    assert len(ids) == 4, f"expected four run transcripts, got {ids}"
    initial, failed, resumed, fresh = ids
    assert failed == resumed, "--resume must finish the same flow, not start one"
    assert len({initial, failed, fresh}) == 3, "--again must allocate a new flow"

    workspace = Path(marker.read_text().strip())
    assert " " in str(workspace), "the workspace path should contain a space"
    store = FilesystemStore(workspace / ".atelier")
    assert sorted(store.list_flows("script-check")) == sorted(ids[:2] + [fresh])

    # The first run stayed intact through the failure, the resume and the re-run.
    assert _log_shape(store, initial) == [
        ("prepare", True), ("syntax", True), ("execute", True)
    ]
    assert store.read_outputs(initial)["execute"] == "workflow works\n"

    # The recovered run: prepare once, the failed then the repaired syntax
    # check, and one execution — which only happened after the repair.
    assert _log_shape(store, resumed) == [
        ("prepare", True), ("syntax", False), ("syntax", True), ("execute", True)
    ]
    broken = store.read_logs(resumed)[1]
    assert "demo.sh" in broken.stderr, broken.stderr
    assert store.read_progress(resumed).status.value == "completed"
    assert store.read_outputs(resumed)["execute"] == "workflow works\n"

    # The fresh run did everything again, including the preparation.
    assert _log_shape(store, fresh) == [
        ("prepare", True), ("syntax", True), ("execute", True)
    ]
    assert json.loads(
        subprocess.run(
            [sys.executable, "-c", _CLI, "outputs", fresh, "--json"],
            cwd=workspace, env=env, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=60,
        ).stdout
    )["execute"] == "workflow works\n"

    # Three flows, three preparations: the counter the guide tells readers to
    # watch, and the proof the resume did not redo completed work.
    assert (workspace / "preparation.log").read_text() == "prepared\n" * 3
