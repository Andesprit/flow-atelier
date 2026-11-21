"""Foreground machine results at the CLI process boundary, using shell tasks."""
from __future__ import annotations

import json
import signal
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
import yaml

from flow_atelier.core.atelier import Atelier
from tests._shell import INTERRUPTIBLE_CLI

CLI = [sys.executable, "-m", "flow_atelier.main"]


@pytest.fixture
def recipe(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("ATELIER_SERVE_URL", "offline://test")
    path = Path(".atelier/conduits/job/conduit.yaml")
    path.parent.mkdir(parents=True)

    def write(script: str) -> None:
        path.write_text(yaml.safe_dump({
            "name": "job", "description": "test", "inputs": {"label": "run label"},
            "tasks": [{"name": "work", "description": "test", "tool": "tool:bash", "task": script}],
        }))

    write("echo {{inputs.label}}")
    return write


def invoke(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [*CLI, *args, "--json"], stdin=subprocess.DEVNULL, capture_output=True,
        encoding="utf-8", timeout=30,
    )


def test_failed_flow_can_be_resumed_and_run_again(recipe):
    recipe("exit 3")
    failed = invoke("run", "job", "-i", "label=one")
    assert failed.returncode == 1, failed.stderr
    fid, = Atelier().list_flows()
    assert json.loads(failed.stdout) == {"flow_id": fid, "status": "failed"}
    assert f"atelier diagnose {fid}" in failed.stderr
    assert "atelier serve --port 0 --no-scheduler" in failed.stderr
    assert json.loads(invoke("status", fid).stdout)["status"] == "failed"

    recipe("echo {{inputs.label}}")
    resumed = invoke("run", "--resume", fid)
    assert resumed.returncode == 0, resumed.stderr
    assert json.loads(resumed.stdout) == {"flow_id": fid, "status": "completed"}
    rejected = invoke("run", "--resume", fid)
    assert rejected.returncode == 1
    assert rejected.stdout == ""
    assert "can only resume" in json.loads(rejected.stderr)["error"]["message"]

    again = invoke("run", "--again", fid, "-i", "label=two")
    assert again.returncode == 0, again.stderr
    new_id, = set(Atelier().list_flows()) - {fid}
    assert json.loads(again.stdout) == {"flow_id": new_id, "status": "completed"}
    assert json.loads(invoke("outputs", new_id).stdout)["work"].strip() == "two"


@pytest.mark.parametrize(("args", "code", "exit_code"), [
    (["job"], "missing_inputs", 2),
    (["job", "-i", "label"], "invalid_arguments", 2),
    (["job", "-i", "typo=one"], "invalid_arguments", 1),
    (["absent"], "unknown_conduit", 1),
    (["--again", "missing"], "unknown_flow", 1),
])
def test_rejected_run_has_one_error_and_no_result(recipe, args, code, exit_code):
    Path(".env").write_text("ATELIER_AGENT_PERMISSIONS=full\n")
    result = invoke("run", *args)
    assert result.returncode == exit_code
    assert result.stdout == ""
    error = json.loads(result.stderr)["error"]
    assert error["code"] == code
    assert any("ignored ATELIER_AGENT_PERMISSIONS" in line for line in error["diagnostics"])
    assert not Path(".atelier/flows").exists()


def test_engine_rejection_before_allocation_returns_only_an_error(recipe):
    path = Path(".atelier/conduits/job/conduit.yaml")
    conduit = yaml.safe_load(path.read_text())
    conduit["tasks"][0]["depends_on"] = ["work"]
    path.write_text(yaml.safe_dump(conduit))
    result = invoke("run", "job", "-i", "label=one")
    assert result.returncode == 1
    assert result.stdout == ""
    error = json.loads(result.stderr)["error"]
    assert error["code"] == "invalid_conduit"
    assert "work -> work" in error["message"]
    assert Atelier().list_flows() == []


def test_nested_failure_returns_parent_id(recipe):
    recipe("exit 1")
    path = Path(".atelier/conduits/parent/conduit.yaml")
    path.parent.mkdir()
    path.write_text(yaml.safe_dump({
        "name": "parent", "description": "test", "tasks": [{
            "name": "child", "description": "test", "tool": "tool:conduit",
            "task": "job", "inputs": {"label": "nested"},
        }],
    }))
    failed = invoke("run", "parent")
    assert failed.returncode == 1, failed.stderr
    parent_id, = Atelier().list_flows()
    assert parent_id.endswith("_parent")
    assert json.loads(failed.stdout) == {"flow_id": parent_id, "status": "failed"}


def test_overlapping_runs_return_their_own_outputs(recipe):
    recipe("""
        touch {{inputs.label}}.ready
        for attempt in {1..200}; do
            if [ -f left.ready ] && [ -f right.ready ]; then echo {{inputs.label}}; exit 0; fi
            sleep 0.05
        done
        exit 1
    """)
    labels = ["left", "right"]
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda label: invoke("run", "job", "-i", f"label={label}"), labels))
    for label, result in zip(labels, results, strict=True):
        assert result.returncode == 0, result.stderr
        record = json.loads(result.stdout)
        fid = record["flow_id"]
        status = json.loads(invoke("status", fid).stdout)
        assert record == {"flow_id": status["flow_id"], "status": "completed"}
        assert status["status"] == "completed"
        assert json.loads(invoke("outputs", fid).stdout)["work"].strip() == label


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX signal handling")
@pytest.mark.parametrize(("sig", "status", "exit_code"), [
    (signal.SIGTERM, "stopped", 143), (signal.SIGINT, "failed", 130),
])
def test_interrupted_run_returns_a_result_and_streams_progress(recipe, tmp_path, sig, status, exit_code):
    recipe("touch working; exec sleep 30")
    progress = tmp_path / "live.stderr"
    with progress.open("w") as err, subprocess.Popen(
        [sys.executable, "-c", INTERRUPTIBLE_CLI, "run", "job", "-i", "label=stop", "--json"],
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=err, text=True,
    ) as process:
        try:
            deadline = time.monotonic() + 15
            while not Path("working").exists() and time.monotonic() < deadline:
                assert process.poll() is None, progress.read_text()
                time.sleep(0.05)
            assert Path("working").exists()
            fid, = Atelier().list_flows()
            assert f"starting flow {fid}" in progress.read_text()
            process.send_signal(sig)
            stdout, _ = process.communicate(timeout=10)
            assert process.returncode == exit_code, progress.read_text()
            assert json.loads(stdout) == {"flow_id": fid, "status": status}
            assert json.loads(invoke("status", fid).stdout)["status"] == status
        finally:
            if process.poll() is None:
                process.kill()
