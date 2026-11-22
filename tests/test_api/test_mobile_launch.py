import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from flow_atelier.core.atelier import Atelier
from flow_atelier.services.api.app import FastApiServer


def test_mobile_launch_uses_selected_project_and_returns_a_readable_run(tmp_path: Path):
    server = Atelier(base_dir=tmp_path / "server" / ".atelier")
    project = tmp_path / "work"
    recipe = project / ".atelier/conduits/probe/conduit.yaml"
    recipe.parent.mkdir(parents=True)
    recipe.write_text(
        "name: probe\ndescription: test\ninputs: {value: text}\ntasks:\n"
        "  - name: write\n    tool: tool:bash\n    description: write\n"
        "    task: echo {{inputs.value}} > result\n"
    )
    with TestClient(FastApiServer().create_app(server), base_url="http://localhost") as client:
        assert client.get("/mobile/projects").json() == []
        assert client.get("/mobile/conduits", params={"project": str(project)}).json()[0]["name"] == "probe"
        body = {"conduit_name": "probe", "run_path": str(project), "inputs": {}}
        assert client.post("/mobile/runs", json={**body, "confirmed_folder": str(project)}).status_code == 422
        body["inputs"] = {"value": "phone"}
        body["launch_id"] = str(uuid4())
        assert client.post("/mobile/runs", json=body).status_code == 422
        body["confirmed_folder"] = str(project)
        with ThreadPoolExecutor(2) as pool:
            first, response = pool.map(lambda _: client.post("/mobile/runs", json=body), range(2))
        assert first.json() == response.json()
        assert response.status_code == 202, response.text
        flow_id = response.json()["flow_id"]
        for _ in range(150):
            if server.store.read_progress(flow_id).finished_at:
                break
            time.sleep(0.02)
        assert (project / "result").read_text().strip() == "phone"
        assert client.get(f"/flows/{flow_id}").status_code == 200
        assert list(server.store.flow_projects()) == [flow_id]
        assert client.get("/mobile/projects").json() == [str(project)]
        assert client.post("/mobile/runs", json={**body, "inputs": {"value": "new"}}).status_code == 409



def _wait(check, what: str):
    for _ in range(250):
        if value := check():
            return value
        time.sleep(0.02)
    raise AssertionError(f"timed out waiting for {what}")


def test_interactive_launch_waits_for_a_form_and_continues_with_the_answers(tmp_path: Path):
    server = Atelier(base_dir=tmp_path / "server" / ".atelier")
    project = tmp_path / "work"
    for name, yaml in {
        "gate": "name: gate\ndescription: test\ntasks:\n"
        "  - name: ask\n    tool: tool:hitl\n    description: ask\n"
        "    task: Approve the release?\n    inputs: {approve: yes or no}\n"
        "  - name: write\n    tool: tool:bash\n    description: write\n    depends_on: [ask]\n"
        "    task: echo {{inputs.approve}} > result\n",
        "outer": "name: outer\ndescription: test\ntasks:\n"
        "  - name: inner\n    tool: tool:conduit\n    description: nested\n    task: gate\n",
    }.items():
        recipe = project / f".atelier/conduits/{name}/conduit.yaml"
        recipe.parent.mkdir(parents=True)
        recipe.write_text(yaml)
    with TestClient(FastApiServer().create_app(server), base_url="http://localhost") as client:
        body = {"conduit_name": "gate", "run_path": str(project), "inputs": {},
                "confirmed_folder": str(project)}
        refused = client.post("/mobile/runs", json=body)
        assert refused.status_code == 422, "a client that cannot answer still gets a clear refusal"
        assert "human-input" in refused.json()["detail"]

        response = client.post("/mobile/runs", json={**body, "interactive": True})
        assert response.status_code == 202, response.text
        flow_id = response.json()["flow_id"]
        [request] = _wait(lambda: client.get("/mobile/inputs").json(), "the form")
        assert request["flow_id"] == flow_id
        assert request["sub_flow_id"] is None
        assert request["task"] == "ask"
        assert request["kind"] == "form"
        assert request["prompt"] == "Approve the release?"
        assert request["fields"] == [{"name": "approve", "description": "yes or no"}]
        assert client.get("/mobile/inputs", params={"flow_id": "other"}).json() == []
        [listed] = [f for f in client.get("/flows").json() if f["flow_id"] == flow_id]
        assert listed["status"] == "needs_input"

        url = f"/mobile/runs/{flow_id}/inputs/{request['request_id']}"
        assert client.post(f"/mobile/runs/other/inputs/{request['request_id']}",
                           json={"answers": {"approve": "yes"}}).status_code == 404
        missing = client.post(url, json={"answers": {}})
        assert missing.status_code == 422
        assert "approve" in missing.json()["detail"]
        assert client.post(url, json={"answers": {"approve": "yes"}}).status_code == 204
        assert client.post(url, json={"answers": {"approve": "no"}}).status_code == 404

        _wait(lambda: server.store.read_progress(flow_id).finished_at, "the run to end")
        assert server.store.read_progress(flow_id).status.value == "completed"
        assert (project / "result").read_text().strip() == "yes"
        assert server.store.read_input(flow_id)["approve"] == "yes"
        assert client.get("/mobile/inputs").json() == []

        nested = client.post("/mobile/runs", json={**body, "conduit_name": "outer", "interactive": True})
        outer_id = nested.json()["flow_id"]
        [request] = _wait(lambda: client.get("/mobile/inputs").json(), "the nested form")
        assert request["flow_id"] == outer_id, "requests use the run GET /flows lists"
        assert request["sub_flow_id"] and request["sub_flow_id"] != outer_id
        [listed] = [f for f in client.get("/flows").json() if f["flow_id"] == outer_id]
        assert listed["status"] == "needs_input"
        answer = client.post(f"/mobile/runs/{outer_id}/inputs/{request['request_id']}",
                             json={"answers": {"approve": "later"}})
        assert answer.status_code == 204
        _wait(lambda: server.store.read_progress(outer_id).finished_at, "the nested run to end")
        assert server.store.read_progress(outer_id).status.value == "completed"
        assert (project / "result").read_text().strip() == "later"


async def test_agent_questions_wait_for_a_nonempty_answer_and_are_withdrawn_on_cancel():
    import asyncio

    from flow_atelier.schemas.api import InputAnswer
    from flow_atelier.services.api.run_inputs import RunInputs, RunScope, ServerPromptSink

    inputs = RunInputs()
    sink = ServerPromptSink(RunScope(inputs, root="20261122_abcd_demo"))
    reply = asyncio.create_task(sink.request_input("Which caller should I focus on?"))
    await asyncio.sleep(0)
    [request] = inputs.pending()
    assert (request.kind, request.prompt, request.flow_id) == (
        "question", "Which caller should I focus on?", "20261122_abcd_demo")
    assert inputs.waiting_runs() == {"20261122_abcd_demo"}
    with pytest.raises(ValueError, match="nonempty"):
        inputs.answer("20261122_abcd_demo", request.request_id, InputAnswer(answer="  "))
    inputs.answer("20261122_abcd_demo", request.request_id, InputAnswer(answer="the CLI"))
    assert await reply == "the CLI"
    assert inputs.pending() == []

    waiting = asyncio.create_task(sink.request_input("Still there?"))
    await asyncio.sleep(0)
    [request] = inputs.pending()
    waiting.cancel()
    await asyncio.gather(waiting, return_exceptions=True)
    assert inputs.pending() == []
    with pytest.raises(KeyError):
        inputs.answer("20261122_abcd_demo", request.request_id, InputAnswer(answer="late"))
