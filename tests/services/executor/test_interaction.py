"""Run real engine, terminal input and ACP subprocesses through each policy."""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from flow_atelier.schemas.interaction import InteractionPolicy
from flow_atelier.services.executor.harness import DEFAULT_DONE_MARKER

REPO = Path(__file__).resolve().parents[3]
AGENT = REPO / "tests/fixtures/fake_acp_agent.py"
DONE = {"chunks": ["Finished. " + DEFAULT_DONE_MARKER]}
MODES = {"current": "bypassPermissions", "available": [
    {"id": "default", "name": "Default"},
    {"id": "bypassPermissions", "name": "Bypass"},
]}
PERMISSION = {"summary": "Run migration", "raw_input": {"command": "x" * 3000}, "options": [
    {"id": "allow", "label": "Allow once", "kind": "allow_once"},
    {"id": "deny", "label": "Reject", "kind": "reject_once"},
]}
RUN = """
import asyncio, json, sys
from pathlib import Path
from flow_atelier.core.atelier import Atelier
from flow_atelier.core.settings import AtelierSettings
config = json.loads(Path(sys.argv[1]).read_text())
atelier = Atelier(AtelierSettings(**config))
asyncio.run(atelier.run_conduit('supervised', {'goal': 'pagination'}))
"""


async def run_flow(tmp_path, policy, turns, *, decision=None, replies="", config=None,
                   supervisor_turn=None, workers=1):
    """Exercise the public facade in a process with real piped human input."""
    directory = tmp_path / ".atelier/conduits/supervised"
    directory.mkdir(parents=True)
    conduit = {"name": "supervised", "description": "Respect existing API", "tasks": [
        {"name": f"worker{i}", "description": "Implement", "task": "Build {{inputs.goal}}",
         "tool": "harness:test-worker"} for i in range(workers)
    ]}
    if policy is not None:
        conduit["interaction"] = dict(policy)
        if decision is not None or supervisor_turn is not None:
            conduit["interaction"]["supervisor"] = {
                "tool": "harness:test-supervisor", **(config or {}),
            }
    (directory / "conduit.yaml").write_text(yaml.safe_dump(conduit))
    supervisor_record = tmp_path / "supervisor-prompts.jsonl"
    worker_record = tmp_path / "worker-prompts.jsonl"
    def command(script):
        return [sys.executable, str(AGENT), "--script", json.dumps(script)]
    settings = {
        "atelier_dir": str(tmp_path / ".atelier"),
        "global_atelier_dir": str(tmp_path / "global"),
        "harnesses": {
            "test-worker": command({"turns": turns, "modes": MODES,
                                    "record_path": str(worker_record)}),
            "test-supervisor": command({"turns": [supervisor_turn or {
                "chunks": [json.dumps(decision)]}], "modes": MODES,
                "record_path": str(supervisor_record)}),
        },
    }
    config_path = tmp_path / "settings.json"
    config_path.write_text(json.dumps(settings))
    proc = await asyncio.create_subprocess_exec(
        sys.executable, "-c", RUN, str(config_path), cwd=REPO,
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(replies.encode()), 30)
    finally:
        if proc.returncode is None:
            proc.kill()
            await proc.wait()
    logs = [json.loads(line) for file in (tmp_path / ".atelier/flows").glob("*/logs.jsonl")
            for line in file.read_text().splitlines()]
    supervisor_prompts = supervisor_record.read_text() if supervisor_record.exists() else ""
    worker_prompts = worker_record.read_text() if worker_record.exists() else ""
    return proc.returncode, out.decode() + err.decode(), logs, supervisor_prompts, worker_prompts


@pytest.mark.parametrize("policy", [
    {"questions": "supervisor"}, {"permissions": "hybrid"},
    {"questions": "approve_all"}, {"permissions": "oops"},
    {"supervisor": {"tool": "tool:bash"}}, {"unknown": True},
    {"supervisor": {"tool": "harness:codex", "max_replies": 0}},
])
def test_invalid_policy_rejected(policy):
    with pytest.raises(ValidationError):
        InteractionPolicy.model_validate(policy)


async def test_legacy_stays_single_turn_and_auto_approves(tmp_path):
    code, output, logs, supervisor, worker = await run_flow(
        tmp_path, None, [{"chunks": ["Question?"], "ask_permission": PERMISSION}],
    )
    assert code == 0, output
    assert "[perm:allow]" in logs[0]["output"]
    assert "[mode_set:default]" not in logs[0]["output"]
    assert not supervisor
    assert any(e.get("source") == "permission_request" for e in logs[0]["session"])
    assert any(e.get("source") == "approve_all" for e in logs[0]["session"])
    assert len(worker.splitlines()) == 1


async def test_conduit_human_policy_enables_replies(tmp_path):
    code, output, logs, supervisor, worker = await run_flow(
        tmp_path, {"questions": "human"}, [{"chunks": ["Which pattern?"]}, DONE],
        replies="Existing pattern\n",
    )
    assert code == 0, output
    assert "Existing pattern" in worker
    assert not supervisor
    assert any(e.get("source") == "human" for e in logs[0]["session"])


async def test_supervisor_receives_complete_history_and_answers_without_human(tmp_path):
    code, output, logs, supervisor, worker = await run_flow(
        tmp_path, {"questions": "supervisor"},
        [{"chunks": ["First question?"]}, {"chunks": ["Second question?"]}, DONE],
        decision={"action": "answer", "reply": "Use the existing convention"}, workers=2,
    )
    assert code == 0, output
    assert len(logs) == 2
    assert "Use the existing convention" in worker
    prompts = [json.loads(line)[0]["text"] for line in supervisor.splitlines()]
    assert len(prompts) == 4
    assert all("Respect existing API" in p and "Build pagination" in p for p in prompts)
    second = [p for p in prompts if "Second question?" in p]
    assert len(second) == 2
    assert all("First question?" in p and "Use the existing convention" in p for p in second)
    for log in logs:
        assert sum(e.get("source") == "supervisor" for e in log["session"]) == 2


@pytest.mark.parametrize("decision", [
    {"action": "escalate", "question": "Which API version?"},
    {"action": "unknown"}, {"action": "answer", "reply": ""},
])
async def test_hybrid_escalates_to_human(tmp_path, decision):
    code, output, logs, _, worker = await run_flow(
        tmp_path, {"questions": "hybrid"}, [{"chunks": ["Which version?"]}, DONE],
        decision=decision, replies="Keep v1\n",
    )
    assert code == 0, output
    assert "Keep v1" in worker
    assert any(e.get("action") == "escalate" for e in logs[0]["session"])


async def test_supervisor_mode_rejects_escalation(tmp_path):
    code, output, logs, _, worker = await run_flow(
        tmp_path, {"questions": "supervisor"}, [{"chunks": ["Question?"]}, DONE],
        decision={"action": "escalate", "question": "Help?"}, replies="Do it\n",
    )
    assert code != 0
    assert "does not permit escalation" in logs[0]["stderr"]
    assert len(worker.splitlines()) == 1


@pytest.mark.parametrize("mode,decision,replies,expected", [
    ("human", None, "deny\n", "deny"),
    ("human", None, "cancel\n", ""),
    ("supervisor", {"action": "answer", "option_id": "deny"}, "", "deny"),
    ("hybrid", {"action": "escalate", "question": "Apply migration?"}, "allow\n", "allow"),
])
async def test_permissions_route_exact_choices_and_disable_bypass(
    tmp_path, mode, decision, replies, expected,
):
    code, output, logs, supervisor, _ = await run_flow(
        tmp_path, {"permissions": mode}, [{**DONE, "ask_permission": PERMISSION}],
        decision=decision, replies=replies,
    )
    assert code == 0, output
    assert f"[perm:{expected}]" in logs[0]["output"]
    assert "[mode_set:default]" in logs[0]["output"]
    assert "[mode_set:bypassPermissions]" not in logs[0]["output"]
    if supervisor:
        assert "x" * 3000 in supervisor  # Complete payload, not UI's truncated snippet.


async def test_invalid_permission_cannot_become_success(tmp_path):
    code, output, logs, _, _ = await run_flow(
        tmp_path, {"permissions": "supervisor"}, [{**DONE, "ask_permission": PERMISSION}],
        decision={"action": "answer", "option_id": "invented"},
    )
    assert code != 0
    assert "offered option" in logs[0]["stderr"]
    assert "[perm:]" in logs[0]["output"]


@pytest.mark.parametrize("mode", ["supervisor", "hybrid"])
@pytest.mark.parametrize("config", [{"max_replies": 1}, {"max_context_chars": 1}])
async def test_limits_fail_or_escalate_without_truncation(tmp_path, mode, config):
    code, output, logs, supervisor, worker = await run_flow(
        tmp_path, {"questions": mode},
        [{"chunks": ["First?"]}, {"chunks": ["Second?"]}, DONE],
        decision={"action": "answer", "reply": "Go ahead"}, config=config,
        replies="Human answer\nAnother answer\n",
    )
    assert (code == 0) == (mode == "hybrid"), output
    if mode == "hybrid":
        assert "Human answer" in worker
    if "max_context_chars" in config:
        assert not supervisor


async def test_supervisor_timeout_escalates(tmp_path):
    code, output, _, _, worker = await run_flow(
        tmp_path, {"questions": "hybrid"}, [{"chunks": ["Question?"]}, DONE],
        supervisor_turn={"delay_before": 5, "chunks": ["{}"]},
        config={"timeout": 1}, replies="Human fallback\n",
    )
    assert code == 0, output
    assert "Human fallback" in worker
