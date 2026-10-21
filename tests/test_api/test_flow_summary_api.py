"""GET /flows/<id>/summary: the run page's saved results and next step."""
from __future__ import annotations

import hashlib
import json
import shlex

import httpx
import pytest

from flow_atelier.core.atelier import Atelier
from flow_atelier.services.api.app import FastApiServer

TWO = """\
name: two
description: Two steps; finish needs a ready file
tasks:
  - prepare:
      description: Save a first result
      tool: tool:bash
      task: "echo run >> prepare.count; echo PREPARED"
  - finish:
      description: Needs the ready file
      tool: tool:bash
      depends_on: [prepare]
      task: "test -f ready || { echo 'missing ready file' >&2; exit 3; }; echo FINISHED"
"""

OUTER = """\
name: outer
description: Calls two
tasks:
  - call:
      description: Run the two recipe
      tool: tool:conduit
      task: two
"""


@pytest.fixture
def project(tmp_path, monkeypatch):
    """A project folder whose path has a space, holding both recipes."""
    monkeypatch.delenv("ATELIER_GLOBAL_ATELIER_DIR", raising=False)
    root = tmp_path / "my project"
    for name, body in (("two", TWO), ("outer", OUTER)):
        (root / ".atelier" / "conduits" / name).mkdir(parents=True)
        (root / ".atelier" / "conduits" / name / "conduit.yaml").write_text(body)
    return root


@pytest.fixture
async def client(project):
    """An httpx client on a server rooted in ``project``."""
    atelier = Atelier(base_dir=project / ".atelier")
    app = FastApiServer().create_app(atelier)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1"
    ) as c:
        yield c, atelier


async def _run(atelier: Atelier, project, name: str = "two") -> str:
    """Run ``name`` in ``project`` and return its flow id, failed or not."""
    started: list[str] = []
    try:
        await atelier.run_conduit(
            name, {}, working_dir=project, on_flow_started=started.append
        )
    except Exception:  # noqa: BLE001 - a failed run is what some tests want
        pass
    return started[0]


def _commands(body: dict) -> list[str]:
    return [s["command"] for s in body["next_steps"] if s["command"]]


async def test_a_completed_run_shows_every_saved_result_as_final(client, project):
    c, atelier = client
    flow_id = await _run(atelier, project)
    (project / "ready").touch()
    flow_id = await _run(atelier, project)

    body = (await c.get(f"/flows/{flow_id}/summary")).json()

    assert body["final"] is True and body["settled"] is True
    assert [(r["task"], r["state"], r["text"]) for r in body["results"]] == [
        ("prepare", "text", "PREPARED\n"),
        ("finish", "text", "FINISHED\n"),
    ]
    assert body["workspace"] == str(project)
    assert body["full_results_command"] == (
        f"cd {shlex.quote(str(project))} && atelier outputs {flow_id}"
    )
    assert not any(cmd.startswith("atelier run") for cmd in _commands(body))


async def test_a_failed_run_names_its_cause_and_a_resume_that_keeps_prepare(
    client, project
):
    c, atelier = client
    flow_id = await _run(atelier, project)

    body = (await c.get(f"/flows/{flow_id}/summary")).json()

    assert body["final"] is False
    assert body["failures"][0]["task"] == "finish"
    assert body["failures"][0]["excerpt"] == "missing ready file"
    assert body["failures"][0]["exit_code"] == 3
    assert [(r["task"], r["state"]) for r in body["results"]] == [("prepare", "text")]
    resume = next(s for s in body["next_steps"] if "--resume" in (s["command"] or ""))
    assert resume["copy_text"] == (
        f"cd {shlex.quote(str(project))} && atelier run --resume {flow_id}"
    )
    assert resume["side_effects"] and resume["acts"] is True
    assert body["next_steps"][0]["acts"] is False  # diagnose's own order is kept

    (project / "ready").touch()
    await atelier.resume_flow(flow_id, working_dir=project)
    body = (await c.get(f"/flows/{flow_id}/summary")).json()
    assert body["final"] is True and body["failures"] == []
    assert (project / "prepare.count").read_text() == "run\n"


async def test_reading_the_summary_changes_no_saved_byte(client, project):
    c, atelier = client
    flow_id = await _run(atelier, project)

    def digest() -> dict[str, str]:
        return {
            str(p): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted((project / ".atelier").rglob("*"))
            if p.is_file()
        }

    before = digest()
    assert (await c.get(f"/flows/{flow_id}/summary")).status_code == 200
    assert digest() == before


async def test_results_are_masked_before_they_are_cut(client, project):
    c, atelier = client
    flow_id = await _run(atelier, project)
    outputs = atelier.store._flow_dir(flow_id) / "outputs.yaml"
    secret = "API_KEY=" + "s" * 40
    # The secret straddles the preview cut: masking after the cut would leave
    # its first characters, and the shortened remainder, unmasked.
    from flow_atelier.modules.run_summary import RESULT_MAX_CHARS

    outputs.write_text(
        json.dumps({"prepare": "x" * (RESULT_MAX_CHARS - 10) + secret, "finish": None})
    )

    body = (await c.get(f"/flows/{flow_id}/summary")).json()
    prepare = body["results"][0]
    assert "sss" not in prepare["text"]
    assert prepare["truncated"] is False
    assert prepare["chars"] == RESULT_MAX_CHARS - 10 + len(secret)


async def test_long_results_are_bounded_and_say_so(client, project):
    c, atelier = client
    flow_id = await _run(atelier, project)
    outputs = atelier.store._flow_dir(flow_id) / "outputs.yaml"
    outputs.write_text(json.dumps({"prepare": "y" * 50_000, "finish": ""}))

    body = (await c.get(f"/flows/{flow_id}/summary")).json()
    prepare, finish = body["results"]
    assert prepare["truncated"] is True and prepare["chars"] == 50_000
    assert len(prepare["text"]) < 50_000
    assert finish["state"] == "empty"


async def test_a_task_without_an_entry_is_reported_missing(client, project):
    c, atelier = client
    (project / "ready").touch()
    flow_id = await _run(atelier, project)
    outputs = atelier.store._flow_dir(flow_id) / "outputs.yaml"
    outputs.write_text(json.dumps({"finish": "FINISHED\n"}))

    body = (await c.get(f"/flows/{flow_id}/summary")).json()
    assert [(r["task"], r["state"]) for r in body["results"]] == [
        ("prepare", "missing"),
        ("finish", "text"),
    ]


async def test_unreadable_outputs_keep_the_rest_of_the_summary(client, project):
    c, atelier = client
    flow_id = await _run(atelier, project)
    (atelier.store._flow_dir(flow_id) / "outputs.yaml").write_text("{: not yaml")

    resp = await c.get(f"/flows/{flow_id}/summary")
    assert resp.status_code == 200
    body = resp.json()
    assert body["results"] == []
    assert "cannot be read" in body["results_unavailable"]
    assert body["failures"][0]["task"] == "finish"


async def test_a_run_that_moves_while_read_is_not_final_and_offers_no_command(
    client, project, monkeypatch
):
    c, atelier = client
    flow_id = await _run(atelier, project)
    progress_file = atelier.store._flow_dir(flow_id) / "progress.json"
    real = atelier.store.read_logs

    # The run is resumed between diagnose's own snapshot and the summary's
    # last reads: its progress moves on after diagnose has already checked.
    def read_logs_then_move(fid):
        data = json.loads(progress_file.read_text())
        data["status"] = "running"
        data["tasks"]["finish"]["status"] = "running"
        progress_file.write_text(json.dumps(data))
        return real(fid)

    calls = {"n": 0}

    def counted(fid):
        calls["n"] += 1
        # diagnose reads the logs first; the summary's own read comes second.
        return real(fid) if calls["n"] == 1 else read_logs_then_move(fid)

    monkeypatch.setattr(atelier.store, "read_logs", counted)
    body = (await c.get(f"/flows/{flow_id}/summary")).json()

    assert body["settled"] is False and body["final"] is False
    assert body["settled_note"]
    assert not any(
        cmd.startswith(("atelier run", "atelier stop")) for cmd in _commands(body)
    )


async def test_a_nested_run_routes_recovery_to_its_parent(client, project):
    c, atelier = client
    parent = await _run(atelier, project, "outer")
    [child] = atelier.store.list_child_flows(parent)

    body = (await c.get(f"/flows/{child}/summary")).json()

    assert body["parent_flow_id"] == parent
    assert body["parent_task"] == "call"
    assert not any(cmd.startswith("atelier run") for cmd in _commands(body))
    assert any(parent in s["what"] for s in body["next_steps"])
    assert body["failures"][0]["task"] == "finish"

    parent_body = (await c.get(f"/flows/{parent}/summary")).json()
    assert any(f"--resume {parent}" in cmd for cmd in _commands(parent_body))


async def test_an_unknown_run_says_where_it_was_looked_for(client, project):
    c, _ = client
    resp = await c.get("/flows/20260101_deadbeef_two/summary")
    assert resp.status_code == 404
    assert str(project) in resp.json()["detail"]
    assert "atelier list flows" in resp.json()["detail"]


async def test_a_custom_store_selects_itself_in_every_command(tmp_path, monkeypatch):
    monkeypatch.delenv("ATELIER_GLOBAL_ATELIER_DIR", raising=False)
    store = tmp_path / "runs store"
    (store / "conduits" / "two").mkdir(parents=True)
    (store / "conduits" / "two" / "conduit.yaml").write_text(TWO)
    atelier = Atelier(base_dir=store)
    flow_id = await _run(atelier, tmp_path)
    app = FastApiServer().create_app(atelier)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1"
    ) as c:
        body = (await c.get(f"/flows/{flow_id}/summary")).json()
    assert body["workspace"] is None and body["store_note"]
    assert body["full_results_command"].startswith(
        f"ATELIER_ATELIER_DIR={shlex.quote(str(store))} atelier outputs"
    )


async def test_the_summary_keeps_the_api_token_boundary(project):
    atelier = Atelier(base_dir=project / ".atelier")
    flow_id = await _run(atelier, project)
    app = FastApiServer().create_app(atelier, api_token="s3cret")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1"
    ) as c:
        assert (await c.get(f"/flows/{flow_id}/summary")).status_code == 401
        ok = await c.get(
            f"/flows/{flow_id}/summary", headers={"Authorization": "Bearer s3cret"}
        )
        assert ok.status_code == 200
