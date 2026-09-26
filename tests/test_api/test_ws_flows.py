"""/ws/flows/<flow_id>: the run page's live feed."""
from __future__ import annotations

import asyncio
import json

import pytest
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from flow_atelier.core.atelier import Atelier
from flow_atelier.schemas.log import IntermediateStep, StepKind, StepRecord
from flow_atelier.schemas.progress import Progress, TaskProgress, TaskStatus
from flow_atelier.services.api.app import FastApiServer
from flow_atelier.services.api.flow_watch import FlowWatcher

STREAM_YAML = """name: stream
description: prints lines
tasks:
  - count:
      description: count
      task: "echo tick"
      tool: tool:bash
      depends_on: []
"""

HOST = {"host": "127.0.0.1"}


@pytest.fixture
def running(tmp_path, monkeypatch):
    """Seed a running flow whose only task is mid-round.

    :param tmp_path: pytest temp directory fixture.
    :param monkeypatch: pytest monkeypatch fixture.
    :returns: tuple of the Atelier and the flow id.
    """
    monkeypatch.setenv("ATELIER_GLOBAL_ATELIER_DIR", str(tmp_path / ".atelier-global"))
    atelier = Atelier(base_dir=tmp_path / ".atelier")
    conduit_dir = atelier.store.base_dir / "conduits" / "stream"
    conduit_dir.mkdir(parents=True)
    (conduit_dir / "conduit.yaml").write_text(STREAM_YAML)
    flow_id = atelier.store.create_flow("stream", {})
    atelier.store.write_progress(
        flow_id,
        Progress(current_tasks=["count"], tasks={"count": TaskProgress(status=TaskStatus.running)}),
    )
    return atelier, flow_id


def _print(atelier: Atelier, flow_id: str, text: str) -> None:
    """Record one line the task printed, as the shell runner does.

    :param atelier: the flow's Atelier.
    :param flow_id: flow identifier.
    :param text: the printed line.
    """
    asyncio.run(
        atelier.store.append_step(
            flow_id,
            StepRecord(task="count", step=IntermediateStep(kind=StepKind.stdout, text=text)),
        )
    )


def _receive(ws) -> dict:
    """Read one envelope.

    :param ws: websocket client.
    :returns: the decoded envelope.
    """
    return json.loads(ws.receive_text())


@pytest.mark.timeout(20)
def test_pushes_each_new_line_of_the_watched_task(running):
    """After a snapshot, lines the task prints arrive on their own."""
    atelier, flow_id = running
    app = FastApiServer().create_app(atelier)
    _print(atelier, flow_id, "tick 1")
    with TestClient(app, base_url="http://127.0.0.1", headers=HOST) as client:
        with client.websocket_connect(f"/ws/flows/{flow_id}") as ws:
            first = _receive(ws)
            assert first["type"] == "flow"
            assert [t["name"] for t in first["flow"]["tasks"]] == ["count"]

            ws.send_text(json.dumps({"type": "watch", "task": "count"}))
            snapshot = _receive(ws)
            assert snapshot["type"] == "task_log"
            (only,) = snapshot["log"]["rounds"]
            assert [line["text"] for line in only["lines"]] == ["tick 1"]

            _print(atelier, flow_id, "tick 2")
            update = _receive(ws)
            assert update["type"] == "task_update"
            (patch,) = update["update"]["rounds"]
            assert [line["text"] for line in patch["append"]] == ["tick 2"]


@pytest.fixture
def nesting(tmp_path, monkeypatch):
    """Seed a running flow whose only task is a tool:conduit mid-round.

    :param tmp_path: pytest temp directory fixture.
    :param monkeypatch: pytest monkeypatch fixture.
    :returns: tuple of the Atelier and the flow id.
    """
    monkeypatch.setenv("ATELIER_GLOBAL_ATELIER_DIR", str(tmp_path / ".atelier-global"))
    atelier = Atelier(base_dir=tmp_path / ".atelier")
    conduit_dir = atelier.store.base_dir / "conduits" / "outer"
    conduit_dir.mkdir(parents=True)
    (conduit_dir / "conduit.yaml").write_text(
        "name: outer\ndescription: nests\ntasks:\n  - nest:\n      description: nest\n"
        "      task: stream\n      tool: tool:conduit\n      depends_on: []\n"
    )
    flow_id = atelier.store.create_flow("outer", {})
    atelier.store.write_progress(
        flow_id,
        Progress(current_tasks=["nest"], tasks={"nest": TaskProgress(status=TaskStatus.running)}),
    )
    return atelier, flow_id


def test_a_sub_run_names_the_run_and_step_that_started_it(nesting):
    """Only a sub-run's map points back to a parent."""
    atelier, flow_id = nesting
    child = atelier.store.create_flow("stream", {}, flow_id)
    atelier.store.write_progress(child, Progress(invoking_task="nest"))

    view = atelier.get_flow_view(child)

    assert (view.parent_flow_id, view.parent_task) == (flow_id, "nest")
    assert atelier.get_flow_view(flow_id).parent_flow_id is None


def test_watcher_catches_a_sub_run_tagged_just_after_its_folder(nesting):
    """The engine names the invoking task a moment after the sub-run's folder appears."""
    atelier, flow_id = nesting
    watcher = FlowWatcher(atelier, flow_id)
    watcher.watch("nest")
    child = atelier.store.create_flow("stream", {}, flow_id)
    assert watcher.poll() == []

    atelier.store.write_progress(
        child, Progress(started_at="2026-09-25T14:00:00Z", invoking_task="nest")
    )
    (update,) = watcher.poll()
    assert update["update"]["rounds"][0]["child_flow_id"] == child


@pytest.mark.timeout(20)
def test_links_a_sub_run_as_soon_as_it_starts(nesting):
    """A tool:conduit round gains its sub-run's link while it runs."""
    atelier, flow_id = nesting
    app = FastApiServer().create_app(atelier)
    with TestClient(app, base_url="http://127.0.0.1", headers=HOST) as client:
        with client.websocket_connect(f"/ws/flows/{flow_id}") as ws:
            assert _receive(ws)["type"] == "flow"
            ws.send_text(json.dumps({"type": "watch", "task": "nest"}))
            (only,) = _receive(ws)["log"]["rounds"]
            assert (only["status"], only["child_flow_id"]) == ("running", None)

            child = atelier.store.create_flow("stream", {}, flow_id)
            atelier.store.write_progress(
                child, Progress(started_at="2026-09-25T14:00:00Z", invoking_task="nest")
            )
            update = _receive(ws)
            assert update["type"] == "task_update"
            (patch,) = update["update"]["rounds"]
            assert patch["child_flow_id"] == child


@pytest.mark.timeout(20)
def test_pushes_the_map_when_progress_changes(running):
    """A task finishing reaches the page without it asking."""
    atelier, flow_id = running
    app = FastApiServer().create_app(atelier)
    with TestClient(app, base_url="http://127.0.0.1", headers=HOST) as client:
        with client.websocket_connect(f"/ws/flows/{flow_id}") as ws:
            assert _receive(ws)["type"] == "flow"
            atelier.store.write_progress(
                flow_id,
                Progress(tasks={"count": TaskProgress(status=TaskStatus.completed)}),
            )
            pushed = _receive(ws)
            assert pushed["type"] == "flow"
            assert pushed["flow"]["tasks"][0]["status"] == "completed"


@pytest.mark.timeout(20)
def test_unknown_flow_and_task_are_reported(running):
    """An unknown flow ends the socket; an unknown task leaves it usable."""
    atelier, flow_id = running
    app = FastApiServer().create_app(atelier)
    with TestClient(app, base_url="http://127.0.0.1", headers=HOST) as client:
        with client.websocket_connect("/ws/flows/no_such_flow") as ws:
            assert _receive(ws) == {"type": "error", "message": "flow not found: no_such_flow"}
        with client.websocket_connect(f"/ws/flows/{flow_id}") as ws:
            _receive(ws)
            ws.send_text(json.dumps({"type": "watch", "task": "nope"}))
            assert _receive(ws) == {"type": "error", "message": "task not found: nope"}
            ws.send_text(json.dumps({"type": "watch", "task": "count"}))
            assert _receive(ws)["type"] == "task_log"


def test_token_is_required_when_configured(running):
    """With an API token set, the socket needs it in ?token=."""
    atelier, flow_id = running
    app = FastApiServer().create_app(atelier, api_token="s3cret")
    with TestClient(app, base_url="http://127.0.0.1", headers=HOST) as client:
        with pytest.raises(WebSocketDisconnect):
            with client.websocket_connect(f"/ws/flows/{flow_id}") as ws:
                ws.receive_text()
        with client.websocket_connect(f"/ws/flows/{flow_id}?token=s3cret") as ws:
            assert _receive(ws)["type"] == "flow"


def test_other_sites_cannot_open_the_socket(running):
    """A page on another site is refused; the UI's own origins are not."""
    atelier, flow_id = running
    app = FastApiServer().create_app(atelier)
    with TestClient(app, base_url="http://127.0.0.1", headers=HOST) as client:
        with pytest.raises(WebSocketDisconnect):
            with client.websocket_connect(
                f"/ws/flows/{flow_id}", headers={"origin": "https://evil.example"}
            ) as ws:
                ws.receive_text()
        for origin in ("http://localhost:5173", "http://127.0.0.1:8000"):
            with client.websocket_connect(
                f"/ws/flows/{flow_id}", headers={"origin": origin}
            ) as ws:
                assert _receive(ws)["type"] == "flow"
