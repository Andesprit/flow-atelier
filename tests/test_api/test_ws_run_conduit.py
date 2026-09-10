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


DECLARED_YAML = """name: greet
description: declared inputs, one with a default
inputs:
  name: Who to greet
  tone:
    description: how warmly
    default: warm
tasks:
  - greet:
      description: greet
      task: "echo {{inputs.name}} {{inputs.tone}}"
      tool: tool:bash
      depends_on: []
"""


REFERENCED_YAML = """name: echo
description: input referenced but never declared
tasks:
  - say:
      description: say
      task: "echo {{inputs.word}}"
      tool: tool:bash
      depends_on: []
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
    seeded = [
        ("hello", HELLO_YAML),
        ("human", HITL_YAML),
        ("greet", DECLARED_YAML),
        ("echo", REFERENCED_YAML),
    ]
    for name, yaml_str in seeded:
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


def _run_to_end(app, conduit_name: str, inputs: dict, run_path) -> list[dict]:
    """Send one ``run`` envelope and drain until the flow completes or fails.

    :param app: FastAPI app under test.
    :param conduit_name: conduit to run.
    :param inputs: ``inputs`` map for the envelope.
    :param run_path: working directory for the run.
    :returns: every envelope received, last one terminal.
    """
    with TestClient(app, base_url="http://127.0.0.1", headers={"host": "127.0.0.1"}) as client:
        with client.websocket_connect("/ws/run-conduit") as ws:
            ws.send_text(
                json.dumps(
                    {
                        "type": "run",
                        "conduit_name": conduit_name,
                        "inputs": inputs,
                        "run_path": str(run_path),
                    }
                )
            )
            return _drain_until(
                ws, lambda e: e["type"] in ("flow_complete", "flow_failed")
            )


def test_ws_run_typo_of_defaulted_input_fails_instead_of_using_default(env, tmp_path):
    """A near-miss of a key with a default is refused, not silently defaulted.

    :param env: env fixture providing (atelier, app).
    :param tmp_path: pytest temp directory fixture.
    """
    atelier, app = env
    envelopes = _run_to_end(app, "greet", {"name": "world", "tonee": "cold"}, tmp_path)
    last = envelopes[-1]
    assert last["type"] == "flow_failed", envelopes
    assert "unknown inputs: ['tonee']" in last["error"]
    assert "did you mean 'tone' for 'tonee'?" in last["error"]
    assert "'greet' accepts inputs: ['name', 'tone']" in last["error"]
    assert atelier.list_flows() == []


def test_ws_run_typo_of_required_input_names_the_typo(env, tmp_path):
    """A near-miss of a required key is reported as the typo, not as missing.

    :param env: env fixture providing (atelier, app).
    :param tmp_path: pytest temp directory fixture.
    """
    _, app = env
    envelopes = _run_to_end(app, "greet", {"nmae": "world"}, tmp_path)
    last = envelopes[-1]
    assert last["type"] == "flow_failed", envelopes
    assert "run of 'greet' has unknown inputs: ['nmae']" in last["error"]
    assert "did you mean 'name' for 'nmae'?" in last["error"]
    assert "missing required" not in last["error"]


def test_ws_run_accepts_referenced_but_undeclared_input(env, tmp_path):
    """A key only referenced as ``{{inputs.word}}`` still runs, as on the CLI.

    :param env: env fixture providing (atelier, app).
    :param tmp_path: pytest temp directory fixture.
    """
    _, app = env
    envelopes = _run_to_end(app, "echo", {"word": "word-from-ws"}, tmp_path)
    assert envelopes[-1]["type"] == "flow_complete", envelopes[-1]
    assert any(
        "word-from-ws" in (e["entry"].get("stdout") or "")
        for e in envelopes
        if e["type"] == "log"
    )


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
