"""Poll a local Streamable HTTP MCP server through the installed conduit helper."""
import json
import socket
import subprocess
import threading
import time
from pathlib import Path
from uuid import UUID

import pytest
import uvicorn
import yaml
from mcp.server.fastmcp import FastMCP
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import Response
from typer.testing import CliRunner

from flow_atelier.cli import app
from flow_atelier.core.settings import AtelierSettings
from flow_atelier.schemas.api import CreateScheduleInput
from flow_atelier.services.store.claims import EventClaims

IDS = [str(UUID(int=n)) for n in (7, 3, 1)]
TOKEN = "fixture-only-access-token"
PACKAGE = Path(__file__).resolve().parents[2] / "flow_atelier/packages/platform"


@pytest.fixture
def intake(tmp_path, monkeypatch, request, _isolate_global_atelier_dir):
    from flow_atelier.services.dictaria import client

    home = _isolate_global_atelier_dir
    monkeypatch.chdir(tmp_path)
    rows = []
    calls = []
    errors = {}
    extracted = []
    mcp = FastMCP("local fixture", stateless_http=True, json_response=getattr(request, "param", True))

    @mcp.tool()
    def list_meetings(offset: int, limit: int) -> dict:
        calls.append(("list", offset))
        page = rows[offset:offset + limit]
        next_offset = offset + limit if len(page) == limit else None
        return {"meetings": page, "next_offset": errors.get("list_offset", next_offset)}

    @mcp.tool()
    def read_meeting(process_id: str, transcript_offset: int) -> dict:
        calls.append((process_id, transcript_offset))
        page = "First " if transcript_offset == 0 else "second"
        return {"process_id": process_id, "transcript_page": page, "title": process_id,
                "next_offset": 6 if not transcript_offset else errors.get("bad_offset")}

    server_app = mcp.streamable_http_app()

    async def authorize(request, call_next):
        if request.headers.get("authorization") != "Bearer " + TOKEN:
            return Response(status_code=401)
        message = await request.json()
        if message.get("params", {}).get("arguments", {}).get("process_id") == errors.get("expire", "never"):
            return Response(status_code=401)
        return await call_next(request)

    server_app.add_middleware(BaseHTTPMiddleware, dispatch=authorize)

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        url = f"http://127.0.0.1:{sock.getsockname()[1]}/mcp"
        server = uvicorn.Server(uvicorn.Config(server_app, log_level="error", ws="none"))
        thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
        thread.start()
        try:
            deadline = time.monotonic() + 10
            while not server.started:
                assert time.monotonic() < deadline
                time.sleep(0.01)
            settings = {"dictariaAccount": "owner@example.test", "dictariaAliases": ["Alex"],
                        "dictariaUrl": url}
            (home / "settings.json").write_text(json.dumps(settings))
            (tmp_path / ".atelier").mkdir()
            (tmp_path / ".atelier/settings.json").write_text(json.dumps(
                {key: "project-value" for key in settings}
            ))
            (tmp_path / ".env").write_text("ATELIER_DICTARIA_URL=https://project.invalid\n")
            for key in ("ACCOUNT", "ALIASES", "URL"):
                monkeypatch.setenv("ATELIER_DICTARIA_" + key, "shell-value")
            assert AtelierSettings().dictaria_account == settings["dictariaAccount"]

            monkeypatch.setattr(client, "PAGE_SIZE", 2)
            monkeypatch.setattr(client, "access_token", lambda account: TOKEN if account == settings["dictariaAccount"] else "")

            def run(argv, *, input, cwd, env, **kwargs):
                assert argv[0] == "claude" and "--safe-mode" in argv
                assert argv[argv.index("--tools") + 1] == ""
                assert "--strict-mcp-config" in argv and "--disable-slash-commands" in argv
                assert Path(cwd) != tmp_path and not list(Path(cwd).iterdir())
                assert TOKEN not in repr((input, argv, env))
                meeting = json.loads(input)
                assert meeting["transcript"] == "First second"
                extracted.append(meeting["process_id"])
                tasks = [
                    {"assignee": " Alex ", "task": "Write the report"},
                    {"assignee": "alex@example.invalid", "task": "Other person's task"},
                    {"assignee": None, "task": "Someone should call"},
                ]
                return subprocess.CompletedProcess(argv, 0, errors.get("output", json.dumps({"tasks": tasks})))

            monkeypatch.setattr("flow_atelier.modules.meetings.subprocess.run", run)
            yield home, rows, calls, errors, extracted
        finally:
            server.should_exit = True
            thread.join(10)


def meeting(index, day):
    return {"process_id": IDS[index], "updated_at": f"2026-11-{day:02}T09:00:00Z", "status": "completed"}


def poll():
    return CliRunner().invoke(app, ["meetings-intake"])


def test_pages_restart_expired_login_and_dedupe(intake):
    home, rows, calls, errors, extracted = intake
    rows.append(meeting(0, 1))
    first = poll()
    assert first.exit_code == 0, first.output
    cursor, = (home / "meetings").rglob("cursor.json")
    before = cursor.read_bytes()
    rows[:] = [meeting(1, 3), meeting(1, 3), meeting(2, 2), *rows]
    errors["expire"] = IDS[2]
    interrupted = poll()
    assert interrupted.exit_code == 1 and "login" in interrupted.output
    assert TOKEN not in interrupted.output and cursor.read_bytes() == before
    assert extracted == IDS[:2]
    errors.clear()
    assert poll().exit_code == 0 and poll().exit_code == 0
    assert extracted == IDS and ("list", 2) in calls
    assert cursor.read_bytes() != before
    claims = EventClaims(home, "meeting-tasks")
    tasks = [json.loads(row["inputs"]) for row in iter(claims.take, None)]
    assert len(tasks) == len({task["task_id"] for task in tasks}) == 3
    assert all(task["task"] == "Write the report" for task in tasks)
    reviews = list((home / "meetings").rglob("tasks.json"))
    assert len(reviews) == 3
    for path in reviews:
        result = json.loads(path.read_text())
        assert result["review"] == ["Someone should call"]
        assert "Other person's task" not in path.read_text()


def test_incomplete_transcript_and_malformed_extraction_are_retryable(intake):
    home, rows, _, errors, extracted = intake
    rows.append(meeting(0, 1))
    errors["list_offset"] = 100
    assert poll().exit_code == 1 and not list(home.rglob("cursor.json"))
    errors.clear()
    errors["bad_offset"] = 6
    assert poll().exit_code == 1
    assert not extracted and not list(home.rglob("meeting.json"))
    errors.clear()
    errors["output"] = '{"tasks":[{"task":"Missing assignee"}]}'
    bad = poll()
    assert bad.exit_code == 1 and "extraction" in bad.output
    assert not list(home.rglob("cursor.json")) and not list(home.rglob("tasks.json"))
    assert EventClaims(home, "meeting-tasks").take() is None
    errors.clear()
    assert poll().exit_code == 0
    assert EventClaims(home, "meeting-tasks").take() is not None


def test_five_minute_recipe():
    recipe = PACKAGE / ".atelier/conduits/meetings-to-tasks"
    assert yaml.safe_load((recipe / "conduit.yaml").read_text())["tasks"][0]["intake"]["task"] == "collect"
    config = CreateScheduleInput.model_validate(yaml.safe_load((recipe / "schedule.yaml").read_text()))
    assert config.conduit_name == "meetings-to-tasks" and config.schedule.every_minutes == 5


async def test_cleartext_remote_rejected_before_keychain_access(monkeypatch):
    from flow_atelier.services.dictaria import client

    monkeypatch.setattr(client, "access_token", lambda _: pytest.fail("read keychain"))
    with pytest.raises(ValueError, match="HTTPS"):
        async with client.connect("http://dictaria.invalid/mcp", "owner@example.test"):
            pytest.fail("connected")


@pytest.mark.parametrize("intake", [False], indirect=True, ids=["SSE"])
def test_restart_after_partial_task_handoff(intake, monkeypatch):
    home, rows, calls, errors, extracted = intake
    rows.append(meeting(0, 1))
    errors["output"] = json.dumps({"tasks": [
        {"task": task, "assignee": "owner@example.test"} for task in ("Draft A", "Draft B")
    ]})
    original = EventClaims.claim

    def interrupt(self, key, project, conduit, inputs):
        if self.category == "meeting-tasks" and inputs["task"] == "Draft B":
            raise RuntimeError("simulated interruption")
        return original(self, key, project, conduit, inputs)

    with monkeypatch.context() as patch:
        patch.setattr(EventClaims, "claim", interrupt)
        assert poll().exit_code == 1
    queue = EventClaims(home, "meeting-tasks")
    first, = queue.waiting()
    assert not list(home.rglob("cursor.json"))
    rows.clear()  # Recovery can finish the saved meeting even if it disappears upstream.
    errors["output"] = "different answer must never be used"
    assert poll().exit_code == 0 and poll().exit_code == 0
    assert extracted == [IDS[0]] and calls.count((IDS[0], 0)) == 1
    replay, second = queue.waiting()
    assert first["run_id"] == replay["run_id"]
    a, b = (json.loads(row["inputs"]) for row in (first, second))
    assert [a["task"], b["task"]] == ["Draft A", "Draft B"]
    assert a["task_id"] != b["task_id"]
