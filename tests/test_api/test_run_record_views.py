"""CLI and dashboard access to the same pre- and post-migration records."""
import asyncio
import json
import os
import sys
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from flow_atelier.cli.main import app
from flow_atelier.core.atelier import Atelier
from flow_atelier.schemas.conduit import Conduit
from flow_atelier.schemas.log import IntermediateStep, StepRecord
from flow_atelier.services.api.app import FastApiServer
from tests._runs import legacy_record


@pytest.mark.parametrize("legacy", [True, False])
async def test_view_watch_and_recover_a_run(tmp_path, monkeypatch, legacy):
    source, target, elsewhere = (tmp_path / n for n in ("source", "target", "elsewhere"))
    for path in (source, target, elsewhere):
        path.mkdir()
    monkeypatch.chdir(source)
    origin = Atelier()
    recipe = Conduit.model_validate(dict(name="build", description="build", tasks=[
        dict(name="build", description="original task", tool="tool:bash",
             task="test -f ready && pwd", depends_on=[])
    ]))
    origin.store.write_conduit(recipe)
    started = []
    with pytest.raises(RuntimeError):
        await origin.run_conduit("build", {}, working_dir=target, on_flow_started=started.append)
    [run] = started
    if legacy:
        legacy_record(origin.store, run)
    else:
        monkeypatch.chdir(elsewhere)
    dashboard = Atelier()
    status = CliRunner().invoke(app, ["status", run, "--json"])
    assert status.exit_code == 0
    assert json.loads(status.stdout)["status"] == "failed"
    api = FastApiServer().create_app(dashboard)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api), base_url="http://localhost") as client:
        view = (await client.get(f"/flows/{run}")).json()
        assert view["tasks"][0]["tool"] == "tool:bash"
        summary = (await client.get(f"/flows/{run}/summary")).json()
        assert summary["workspace"] == str(source)
    with TestClient(api) as client, client.websocket_connect(f"ws://localhost/ws/flows/{run}") as socket:
        assert socket.receive_json()["type"] == "flow"
        socket.send_json({"type": "watch", "task": "build"})
        assert socket.receive_json()["type"] == "task_log"
        await origin.store.append_step(
            run, StepRecord(task="build", step=IntermediateStep(kind="tool_call", tool_name="inspect"))
        )
        assert "inspect" in json.dumps(socket.receive_json())
    (target / "ready").touch()
    async def recover(option):
        proc = await asyncio.create_subprocess_exec(
            sys.executable, "-m", "flow_atelier.main", "run", option, run, "--json",
            env={**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[2])},
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        out, err = await proc.communicate()
        assert proc.returncode == 0, err
        return json.loads(out)["flow_id"]

    assert await recover("--resume") == run
    again = await recover("--again")
    assert again != run
    assert dashboard.get_outputs(again)["build"].strip() == str(target)
