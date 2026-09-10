"""/ws/run-conduit end-to-end tests."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from flow_atelier.core.atelier import Atelier
from flow_atelier.core.settings import AtelierSettings
from flow_atelier.services.api.app import FastApiServer

FAKE_AGENT = Path(__file__).resolve().parents[1] / "fixtures" / "fake_acp_agent.py"

HELLO_YAML = """name: hello
description: Say hello
tasks:
  - greet:
      description: greet
      task: "echo hello-from-ws"
      tool: tool:bash
      depends_on: []
"""


HITL_YAML = """name: human
description: HITL
tasks:
  - ask:
      description: gate
      task: "Provide your answer"
      tool: tool:hitl
      depends_on: []
      inputs:
        choice: "Pick a value"
"""


@pytest.fixture
def env(tmp_path, monkeypatch):
    """Build an Atelier + FastAPI app pair with seeded conduits.

    :param tmp_path: pytest temp directory fixture.
    :param monkeypatch: pytest monkeypatch fixture.
    """
    global_dir = tmp_path / ".atelier-global"
    monkeypatch.setenv("ATELIER_GLOBAL_ATELIER_DIR", str(global_dir))
    atelier = Atelier(base_dir=tmp_path / ".atelier")
    for name, yaml_str in [("hello", HELLO_YAML), ("human", HITL_YAML)]:
        (atelier.store.global_dir / "conduits" / name).mkdir(
            parents=True, exist_ok=True
        )
        (
            atelier.store.global_dir / "conduits" / name / "conduit.yaml"
        ).write_text(yaml_str)
    app = FastApiServer().create_app(atelier)
    return atelier, app


def _drain_until(ws, predicate, *, max_messages: int = 50):
    """Read envelopes from a websocket until ``predicate`` matches.

    :param ws: websocket client.
    :param predicate: callable invoked per envelope; stop when it returns truthy.
    :param max_messages: cap on envelopes to read before raising.
    """
    collected: list[dict] = []
    for _ in range(max_messages):
        msg = ws.receive_text()
        envelope = json.loads(msg)
        collected.append(envelope)
        if predicate(envelope):
            return collected
    raise AssertionError(
        f"never received target envelope; got: {collected}"
    )


def test_ws_happy_path_emits_started_log_and_complete(env, tmp_path):
    """Verify the WS happy path emits started, log, and flow_complete envelopes.

    :param env: env fixture providing (atelier, app).
    :param tmp_path: pytest temp directory fixture.
    """
    _, app = env
    with TestClient(app, base_url="http://127.0.0.1", headers={"host": "127.0.0.1"}) as client:
        with client.websocket_connect("/ws/run-conduit") as ws:
            ws.send_text(
                json.dumps(
                    {
                        "type": "run",
                        "conduit_name": "hello",
                        "inputs": {},
                        "run_path": str(tmp_path),
                    }
                )
            )
            envelopes = _drain_until(
                ws, lambda e: e["type"] == "flow_complete"
            )

    types = [e["type"] for e in envelopes]
    assert "started" in types
    assert "log" in types
    assert types[-1] == "flow_complete"
    log_envelopes = [e for e in envelopes if e["type"] == "log"]
    assert any(
        "hello-from-ws" in (entry["entry"].get("stdout") or "")
        for entry in log_envelopes
    )


def test_ws_runs_a_project_scoped_conduit(tmp_path, monkeypatch):
    """A conduit in the *project* store is runnable over the socket.

    Regression guard: the per-flow Atelier used to be re-rooted at the global
    dir, so anything under ``./.atelier/conduits`` — everything ``atelier
    init`` and the Designer create — failed with "conduit not found".

    :param tmp_path: pytest temp directory fixture.
    :param monkeypatch: pytest monkeypatch fixture.
    """
    monkeypatch.setenv(
        "ATELIER_GLOBAL_ATELIER_DIR", str(tmp_path / ".atelier-global")
    )
    atelier = Atelier(base_dir=tmp_path / ".atelier")
    project_conduit = atelier.store.base_dir / "conduits" / "hello"
    project_conduit.mkdir(parents=True, exist_ok=True)
    (project_conduit / "conduit.yaml").write_text(HELLO_YAML)
    assert atelier.store.conduit_source("hello") == "project"

    app = FastApiServer().create_app(atelier)
    with TestClient(app, base_url="http://127.0.0.1", headers={"host": "127.0.0.1"}) as client:
        with client.websocket_connect("/ws/run-conduit") as ws:
            ws.send_text(
                json.dumps(
                    {
                        "type": "run",
                        "conduit_name": "hello",
                        "inputs": {},
                        "run_path": str(tmp_path),
                    }
                )
            )
            envelopes = _drain_until(
                ws, lambda e: e["type"] in ("flow_complete", "flow_failed")
            )

    assert envelopes[-1]["type"] == "flow_complete", envelopes[-1]


def test_ws_hitl_round_trip_completes_after_answer(env, tmp_path):
    """Verify the WS hitl round-trip completes after an answer is sent.

    :param env: env fixture providing (atelier, app).
    :param tmp_path: pytest temp directory fixture.
    """
    _, app = env
    with TestClient(app, base_url="http://127.0.0.1", headers={"host": "127.0.0.1"}) as client:
        with client.websocket_connect("/ws/run-conduit") as ws:
            ws.send_text(
                json.dumps(
                    {
                        "type": "run",
                        "conduit_name": "human",
                        "inputs": {},
                        "run_path": str(tmp_path),
                    }
                )
            )
            # Drain until we see the hitl_request.
            envelopes = _drain_until(
                ws, lambda e: e["type"] == "hitl_request"
            )
            flow_id = envelopes[-1]["flow_id"]
            assert flow_id  # server-assigned flow id
            ws.send_text(
                json.dumps(
                    {
                        "type": "hitl_answer",
                        "flow_id": flow_id,
                        "answers": {"choice": "blue"},
                    }
                )
            )
            envelopes += _drain_until(
                ws, lambda e: e["type"] == "flow_complete"
            )

    assert envelopes[-1]["type"] == "flow_complete"


def test_ws_unknown_message_type_emits_error_envelope(env):
    """Verify an unknown WS message type emits an error envelope.

    :param env: env fixture providing (atelier, app).
    """
    _, app = env
    with TestClient(app, base_url="http://127.0.0.1", headers={"host": "127.0.0.1"}) as client:
        with client.websocket_connect("/ws/run-conduit") as ws:
            ws.send_text(json.dumps({"type": "explode", "flow_id": "T-3"}))
            envelope = json.loads(ws.receive_text())
            assert envelope["type"] == "error"


def test_ws_run_unknown_conduit_emits_flow_failed(env, tmp_path):
    """Verify running an unknown conduit emits flow_failed or error.

    :param env: env fixture providing (atelier, app).
    :param tmp_path: pytest temp directory fixture.
    """
    _, app = env
    with TestClient(app, base_url="http://127.0.0.1", headers={"host": "127.0.0.1"}) as client:
        with client.websocket_connect("/ws/run-conduit") as ws:
            ws.send_text(
                json.dumps(
                    {
                        "type": "run",
                        "conduit_name": "ghost",
                        "inputs": {},
                        "run_path": str(tmp_path),
                    }
                )
            )
            envelopes = _drain_until(
                ws, lambda e: e["type"] in ("flow_failed", "error")
            )
    assert envelopes[-1]["type"] in ("flow_failed", "error")


def test_ws_rejects_bad_token(env):
    """With an api_token configured, a WS without the right ?token= is rejected
    before the handshake is accepted (closed with 1008).

    :param env: env fixture providing (atelier, app).
    """
    atelier, _ = env
    app = FastApiServer().create_app(atelier, api_token="s3cret")
    with TestClient(app, base_url="http://127.0.0.1", headers={"host": "127.0.0.1"}) as client:
        # The token check runs before accept(), so Starlette turns the
        # pre-accept close into a disconnect raised at connect time.
        with pytest.raises(WebSocketDisconnect) as missing:
            with client.websocket_connect("/ws/run-conduit"):
                pass
        assert missing.value.code == 1008

        with pytest.raises(WebSocketDisconnect) as wrong:
            with client.websocket_connect("/ws/run-conduit?token=wrong"):
                pass
        assert wrong.value.code == 1008


# ── interactive harness (agent_message / agent_input_request) ─────────────

INTERACTIVE_YAML = """name: chat
description: interactive harness
tasks:
  - ask:
      description: ask the agent
      task: "Pick a colour"
      tool: harness:fake
      depends_on: []
      interactive: true
"""


@pytest.fixture
def interactive_app(tmp_path, _isolate_global_atelier_dir):
    """Build an app whose ``harness:fake`` agent asks one question.

    The scripted agent ends its first turn without the done marker, which
    is what drives the harness into ``PromptSink.request_input``. Its second
    turn — only reachable in the same ACP session, since the turn list lives
    in the spawned process — emits the marker and finishes the run.

    :param tmp_path: pytest temp directory fixture.
    :param _isolate_global_atelier_dir: isolated global atelier dir fixture.
    """
    script = json.dumps(
        {
            "turns": [
                {"chunks": ["Which colour? "], "stop": "end_turn"},
                {"chunks": ["Blue it is. [ATELIER_DONE]"], "stop": "end_turn"},
            ]
        }
    )
    atelier = Atelier(
        settings=AtelierSettings(
            atelier_dir=tmp_path / ".atelier",
            global_atelier_dir=_isolate_global_atelier_dir,
            harnesses={"fake": [sys.executable, str(FAKE_AGENT), "--script", script]},
        )
    )
    conduit_dir = atelier.store.base_dir / "conduits" / "chat"
    conduit_dir.mkdir(parents=True, exist_ok=True)
    (conduit_dir / "conduit.yaml").write_text(INTERACTIVE_YAML)
    return FastApiServer().create_app(atelier)


def test_ws_interactive_harness_round_trip_completes(interactive_app, tmp_path):
    """An interactive task asks over WS, gets an answer, and completes.

    :param interactive_app: app fixture with a scripted interactive harness.
    :param tmp_path: pytest temp directory fixture.
    """
    with TestClient(
        interactive_app, base_url="http://127.0.0.1", headers={"host": "127.0.0.1"}
    ) as client:
        with client.websocket_connect("/ws/run-conduit") as ws:
            ws.send_text(
                json.dumps(
                    {
                        "type": "run",
                        "conduit_name": "chat",
                        "inputs": {},
                        "run_path": str(tmp_path),
                    }
                )
            )
            envelopes = _drain_until(
                ws,
                lambda e: e["type"] in ("agent_input_request", "flow_failed"),
                max_messages=80,
            )
            request = envelopes[-1]
            assert request["type"] == "agent_input_request", envelopes
            assert request["task"] == "ask"
            assert request["request_id"]
            assert request["prompt"]

            # The question itself reached the client as streamed prose.
            messages = [e for e in envelopes if e["type"] == "agent_message"]
            assert any("Which colour?" in e["text"] for e in messages), envelopes
            assert all(e["task"] == "ask" for e in messages)

            ws.send_text(
                json.dumps(
                    {
                        "type": "agent_input_answer",
                        "flow_id": request["flow_id"],
                        "request_id": request["request_id"],
                        "answer": "blue",
                    }
                )
            )
            envelopes += _drain_until(
                ws,
                lambda e: e["type"] in ("flow_complete", "flow_failed"),
                max_messages=80,
            )

    assert envelopes[-1]["type"] == "flow_complete", envelopes[-1]
    logs = [e for e in envelopes if e["type"] == "log"]
    output = "".join(e["entry"].get("output") or "" for e in logs)
    # Both turns are in one buffer, so the session continued rather than restarting.
    assert "Which colour?" in output
    assert "Blue it is." in output
    assert "[ATELIER_DONE]" not in output


def test_ws_agent_input_answer_with_unknown_request_id_errors(env):
    """An answer for a request nobody is waiting on gets an error envelope.

    :param env: env fixture providing (atelier, app).
    """
    _, app = env
    with TestClient(app, base_url="http://127.0.0.1", headers={"host": "127.0.0.1"}) as client:
        with client.websocket_connect("/ws/run-conduit") as ws:
            ws.send_text(
                json.dumps(
                    {
                        "type": "agent_input_answer",
                        "flow_id": "T-ghost",
                        "request_id": "nope",
                        "answer": "blue",
                    }
                )
            )
            envelope = json.loads(ws.receive_text())
    assert envelope["type"] == "error"
    assert envelope["flow_id"] == "T-ghost"


def test_ws_accepts_valid_token(env, tmp_path):
    """With an api_token configured, ?token=<token> connects and runs.

    :param env: env fixture providing (atelier, app).
    :param tmp_path: pytest temp directory fixture.
    """
    atelier, _ = env
    app = FastApiServer().create_app(atelier, api_token="s3cret")
    with TestClient(app, base_url="http://127.0.0.1", headers={"host": "127.0.0.1"}) as client:
        with client.websocket_connect("/ws/run-conduit?token=s3cret") as ws:
            ws.send_text(
                json.dumps(
                    {
                        "type": "run",
                        "conduit_name": "hello",
                        "inputs": {},
                        "run_path": str(tmp_path),
                    }
                )
            )
            envelopes = _drain_until(ws, lambda e: e["type"] == "flow_complete")
    assert envelopes[-1]["type"] == "flow_complete"


def test_ws_hybrid_permission_escalates_and_preserves_request_identity(tmp_path):
    """Exercise supervisor escalation through the production WebSocket sink."""
    import yaml

    def launch(script):
        return [sys.executable, str(FAKE_AGENT), "--script", json.dumps(script)]

    atelier = Atelier(settings=AtelierSettings(
        atelier_dir=tmp_path / '.atelier', global_atelier_dir=tmp_path / 'global',
        harnesses={
            'worker': launch({'turns': [{'ask_permission': {
                'summary': 'Publish package', 'options': [
                    {'id': 'allow', 'label': 'Allow', 'kind': 'allow_once'},
                    {'id': 'deny', 'label': 'Reject', 'kind': 'reject_once'},
                ],
            }, 'chunks': ['Finished [ATELIER_DONE]']}]}),
            'supervisor': launch({'turns': [{'chunks': [json.dumps({
                'action': 'escalate', 'question': 'Publish this version?',
            })]}]}),
        },
    ))
    folder = atelier.store.base_dir / 'conduits' / 'chat'
    folder.mkdir(parents=True)
    (folder / 'conduit.yaml').write_text(yaml.safe_dump({
        'name': 'chat', 'description': 'Review publication',
        'interaction': {'permissions': 'hybrid', 'supervisor': {'tool': 'harness:supervisor'}},
        'tasks': [{'name': 'worker', 'description': 'Publish', 'task': 'Publish the package',
                   'tool': 'harness:worker'}],
    }))
    app = FastApiServer().create_app(atelier)
    with TestClient(app, base_url='http://127.0.0.1', headers={'host': '127.0.0.1'}) as client:
        with client.websocket_connect('/ws/run-conduit') as ws:
            ws.send_json({'type': 'run', 'conduit_name': 'chat', 'inputs': {},
                          'run_path': str(tmp_path)})
            events = _drain_until(ws, lambda e: e['type'] in (
                'agent_input_request', 'flow_failed'), max_messages=80)
            request = events[-1]
            assert request['type'] == 'agent_input_request', events
            assert request['task'] == 'worker'
            assert 'Publish this version?' in request['prompt']
            assert 'Publish package' in request['prompt']
            assert 'deny: Reject' in request['prompt']
            ws.send_json({'type': 'agent_input_answer', 'flow_id': request['flow_id'],
                          'request_id': request['request_id'], 'answer': 'deny'})
            events += _drain_until(ws, lambda e: e['type'] in (
                'flow_complete', 'flow_failed'), max_messages=80)
    assert events[-1]['type'] == 'flow_complete', events
    decisions = [e['step']['text'] for e in events
                 if e['type'] == 'step' and e['step']['kind'] == 'interaction']
    assert any('supervisor escalate:' in text for text in decisions)
    assert 'human answer: deny' in decisions
