"""End-to-end coverage for the conduits `atelier compose` writes.

Everything but the agents is real: the CLI, the engine, the ACP harness
executor and the on-disk store. Each agent is the scripted fake, registered
under its own `harness:<name>` through `ATELIER_HARNESSES`, and recording
every prompt it is handed — so routing, ordering, failure and resume are read
back from files rather than inferred.
"""
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

import pytest
from typer.testing import CliRunner

from flow_atelier.cli import app
from flow_atelier.core.atelier import Atelier
from flow_atelier.schemas.progress import FlowStatus, TaskStatus

FAKE_AGENT = Path(__file__).resolve().parents[1] / "fixtures" / "fake_acp_agent.py"
_FLOW_ID = re.compile(r"^flow_id: (\S+)$", re.MULTILINE)

BRIEF = "Move the billing job off cron. Constraint: budget=0, deadline=friday."
SAID = {
    "alpha": "ALPHA_PLAN: run it from the scheduler.",
    "beta": "BETA_REVIEW: the retry window is wrong.",
    "gamma": "GAMMA_MERGE: ship the scheduler, fix retries first.",
}


def _launch(script: dict) -> list[str]:
    """Build the argv that starts one scripted fake agent.

    :param script: the fake agent's script document.
    :returns: argv for ``ATELIER_HARNESSES``.
    """
    return [sys.executable, str(FAKE_AGENT), "--script", json.dumps(script)]


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    """An isolated project whose harnesses are scripted fake agents.

    :param tmp_path: pytest temp directory fixture.
    :param monkeypatch: pytest monkeypatch fixture.
    :returns: the workspace path.
    """
    work = tmp_path / "project"
    (work / ".atelier" / "conduits").mkdir(parents=True)
    (tmp_path / "global" / "conduits").mkdir(parents=True)
    for key in list(os.environ):
        if key.startswith("ATELIER_"):
            monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("ATELIER_GLOBAL_ATELIER_DIR", str(tmp_path / "global"))
    monkeypatch.setenv("ATELIER_NO_UPDATE_CHECK", "1")
    monkeypatch.chdir(work)
    return work


def _register(monkeypatch, tmp_path, **overrides) -> None:
    """Register alpha/beta/gamma, each recording prompts to its own file.

    :param monkeypatch: pytest monkeypatch fixture.
    :param tmp_path: the directory holding the record files.
    :param overrides: extra script keys per agent name, e.g. a barrier.
    """
    harnesses = {}
    for name, text in SAID.items():
        script = {
            "turns": [{"chunks": [text], **overrides.get(f"{name}_turn", {})}],
            "record_path": str(tmp_path / f"{name}.jsonl"),
            **overrides.get(name, {}),
        }
        harnesses[name] = _launch(script)
    monkeypatch.setenv("ATELIER_HARNESSES", json.dumps(harnesses))


def _prompts(tmp_path, name: str) -> list[str]:
    """Return each prompt one agent received, newest last.

    :param tmp_path: the directory holding the record files.
    :param name: the harness name.
    :returns: one joined text block per prompt call.
    """
    record = tmp_path / f"{name}.jsonl"
    if not record.exists():
        return []
    return [
        "\n".join(block.get("text", "") for block in json.loads(line))
        for line in record.read_text(encoding="utf-8").splitlines()
    ]


def _flow_id(output: str) -> str:
    """Extract the exact flow id a run printed.

    :param output: captured CLI output.
    :returns: the flow id.
    """
    match = _FLOW_ID.search(output)
    assert match, f"no flow_id in output:\n{output}"
    return match.group(1)


def test_sequential_handoff_routes_the_brief_and_each_result(workspace, tmp_path, monkeypatch):
    """Two agents in a chain: the brief reaches both, the result reaches the second."""
    _register(monkeypatch, tmp_path)
    runner = CliRunner()
    composed = runner.invoke(
        app,
        ["compose", "chain", "-s", "alpha=Propose a fix.", "-s", "beta=Review the fix."],
    )
    assert composed.exit_code == 0, composed.output
    assert runner.invoke(app, ["check", "chain"]).exit_code == 0

    run = runner.invoke(app, ["run", "chain", "--input", f"brief={BRIEF}"])
    assert run.exit_code == 0, run.output
    flow_id = _flow_id(run.output)

    alpha, beta = _prompts(tmp_path, "alpha"), _prompts(tmp_path, "beta")
    assert len(alpha) == 1 and len(beta) == 1
    # The first agent sees its own instruction and the brief, nothing else.
    assert "Propose a fix." in alpha[0] and BRIEF in alpha[0]
    assert SAID["beta"] not in alpha[0]
    # The second sees the brief and the first result, attributed to its step.
    assert "Review the fix." in beta[0] and BRIEF in beta[0]
    assert "--- BEGIN RESULT FROM step_1 (harness:alpha) ---" in beta[0]
    assert SAID["alpha"] in beta[0]

    # A downstream prompt carrying an upstream result cannot precede it.
    atelier = Atelier()
    progress = atelier.get_status(flow_id)
    assert progress.status is FlowStatus.completed
    assert [entry.task for entry in atelier.get_flow_logs(flow_id)] == ["step_1", "step_2"]

    saved = runner.invoke(app, ["outputs", flow_id, "--task", "step_2"])
    assert saved.exit_code == 0, saved.output
    assert SAID["beta"] in saved.stdout


def test_parallel_workers_overlap_and_synthesis_waits(workspace, tmp_path, monkeypatch):
    """Both workers are in flight at once; the synthesizer sees both results."""
    gate = {"dir": str(tmp_path / "gate"), "size": 2, "timeout": 30}
    _register(
        monkeypatch,
        tmp_path,
        alpha_turn={"barrier": gate},
        beta_turn={"barrier": gate},
    )
    runner = CliRunner()
    composed = runner.invoke(
        app,
        [
            "compose", "panel", "--parallel",
            "-s", "alpha=Check correctness.",
            "-s", "beta=Check security.",
            "--synthesize", "gamma=Merge both reviews.",
        ],
    )
    assert composed.exit_code == 0, composed.output

    run = runner.invoke(app, ["run", "panel", "--input", f"brief={BRIEF}"])
    # Neither worker can leave its turn until the other has entered it, so a
    # completed run is proof they were running at the same time.
    assert run.exit_code == 0, run.output
    flow_id = _flow_id(run.output)

    for name in ("alpha", "beta"):
        prompt = _prompts(tmp_path, name)[0]
        assert BRIEF in prompt
        assert "RESULT FROM" not in prompt

    synthesis = _prompts(tmp_path, "gamma")
    assert len(synthesis) == 1
    assert "--- BEGIN RESULT FROM step_1 (harness:alpha) ---" in synthesis[0]
    assert "--- BEGIN RESULT FROM step_2 (harness:beta) ---" in synthesis[0]
    assert SAID["alpha"] in synthesis[0] and SAID["beta"] in synthesis[0]

    atelier = Atelier()
    assert atelier.get_status(flow_id).status is FlowStatus.completed
    assert [e.task for e in atelier.get_flow_logs(flow_id)][-1] == "synthesis"
    assert atelier.get_outputs(flow_id)["synthesis"] == SAID["gamma"]


def test_a_failed_agent_stops_the_chain_and_resume_keeps_the_work(
    workspace, tmp_path, monkeypatch
):
    """The second agent is logged out: nothing downstream runs, and a resume
    finishes without re-running the first."""
    _register(monkeypatch, tmp_path, beta={"fail_session": "not logged in"})
    runner = CliRunner()
    assert runner.invoke(
        app,
        [
            "compose", "chain",
            "-s", "alpha=Propose a fix.",
            "-s", "beta=Review the fix.",
            "-s", "gamma=Write it up.",
        ],
    ).exit_code == 0

    failed = runner.invoke(app, ["run", "chain", "--input", f"brief={BRIEF}"])
    assert failed.exit_code == 1, failed.output
    flow_id = _flow_id(failed.output)

    progress = Atelier().get_status(flow_id)
    assert progress.status is FlowStatus.failed
    assert progress.tasks["step_1"].status is TaskStatus.completed
    assert progress.tasks["step_2"].status is TaskStatus.failed
    # The step after the failure never received a prompt.
    assert progress.tasks["step_3"].status is not TaskStatus.completed
    assert _prompts(tmp_path, "gamma") == []
    assert len(_prompts(tmp_path, "alpha")) == 1

    # Repair: the same agent, now able to open a session.
    _register(monkeypatch, tmp_path)
    resumed = runner.invoke(app, ["run", "--resume", flow_id])
    assert resumed.exit_code == 0, resumed.output
    assert _flow_id(resumed.output) == flow_id

    # The completed step was not asked again; the repaired one ran with the
    # result it already had, and the chain finished.
    assert len(_prompts(tmp_path, "alpha")) == 1
    assert SAID["alpha"] in _prompts(tmp_path, "beta")[-1]
    assert SAID["beta"] in _prompts(tmp_path, "gamma")[0]

    atelier = Atelier()
    assert atelier.get_status(flow_id).status is FlowStatus.completed
    outputs = atelier.get_outputs(flow_id)
    assert outputs["step_1"] == SAID["alpha"] and outputs["step_3"] == SAID["gamma"]
