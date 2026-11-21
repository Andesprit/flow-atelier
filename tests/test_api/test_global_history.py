import json

import httpx
from typer.testing import CliRunner

from flow_atelier.cli.main import app
from flow_atelier.core.atelier import Atelier
from flow_atelier.schemas.api import RunTaskInput
from flow_atelier.services.api.app import FastApiServer


async def test_history_in_a_third_project_identifies_the_actual_target(tmp_path, monkeypatch):
    source, target, collision, dashboard = [tmp_path / p for p in ("a", "b-b", "b_b", "c")]
    for folder in (source, target, collision, dashboard):
        folder.mkdir()
    monkeypatch.chdir(source)
    runner = Atelier()
    runs = []
    for directory in (target, collision):
        run = await runner.run_single_task(
            RunTaskInput(
                name="pwd",
                description="report cwd",
                task="pwd",
                tool="tool:bash",
                run_path=str(directory),
            )
        )
        assert run.success
        runs.append(run.flow_id)
    monkeypatch.chdir(dashboard)
    api = FastApiServer().create_app(Atelier())
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=api), base_url="http://localhost"
    ) as client:
        global_rows = (await client.get("/flows")).json()
        assert {row["flow_id"]: row["project"] for row in global_rows} == dict(
            zip(runs, map(str, (target, collision)), strict=True)
        )
        for directory, ids in ((source, []), (target, runs[:1]), (collision, runs[1:])):
            rows = (await client.get("/flows", params={"project": str(directory)})).json()
            assert [row["flow_id"] for row in rows] == ids
            result = CliRunner().invoke(
                app, ["list", "flows", "--project", str(directory), "--json"]
            )
            assert result.exit_code == 0
            assert [row["flow_id"] for row in json.loads(result.stdout)] == ids
    result = CliRunner().invoke(app, ["list", "flows", "--json"])
    assert {row["flow_id"] for row in json.loads(result.stdout)} == set(runs)

    # A complete invalid event is corruption even if later events can be read.
    journal = runner.store.journal(runs[0])
    with journal.path.open("ab") as stream:
        stream.write(b"broken\n{}\n")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=api), base_url="http://localhost"
    ) as client:
        response = await client.get("/flows")
    assert response.status_code == 422
    assert runs[0] in response.json()["detail"]
    result = CliRunner().invoke(app, ["list", "flows", "--json"])
    assert result.exit_code == 1
    assert runs[0] in json.loads(result.stderr)["error"]["message"]
