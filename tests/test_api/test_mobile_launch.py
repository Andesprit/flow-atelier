import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from uuid import uuid4

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

