"""Exercise the local sender and its persisted retry decision through the CLI."""
import asyncio
import base64
import json
from types import SimpleNamespace
from urllib.parse import parse_qs

import pytest
from typer.testing import CliRunner

from flow_atelier.cli import app
from flow_atelier.core.atelier import Atelier

PR = "https://github.com/example/demo/pull/42"
TO = "+12025550123"
SID = "AC" + "a" * 32
RECEIPT = "SM" + "b" * 32


@pytest.fixture
def finished(tmp_path, monkeypatch, _isolate_global_atelier_dir):
    monkeypatch.chdir(tmp_path)
    home = _isolate_global_atelier_dir
    (home / "settings.json").write_text(json.dumps({
        "notificationRecipient": TO, "notificationProvider": "twilio",
    }))
    (home / "twilio.json").write_text(json.dumps({
        "account_sid": SID, "auth_token": "test-only-secret", "sender": "+12025550111",
        "content_sid": "HX" + "c" * 32,
    }))
    recipe = tmp_path / ".atelier/conduits/demo"
    recipe.mkdir(parents=True)
    (tmp_path / ".atelier/settings.json").write_text(json.dumps({
        "notificationRecipient": "+12025550999", "notificationProvider": "project",
    }))
    (tmp_path / ".env").write_text("ATELIER_NOTIFICATION_RECIPIENT=+12025550888\n")
    atelier = Atelier()
    (recipe / "conduit.yaml").write_text(
        "name: demo\ndescription: demo\ntasks:\n  - work:\n      description: work\n"
        "      task: echo \"$ATELIER_FLOW_ID\"\n      tool: tool:bash\n      depends_on: []\n"
    )
    return atelier, asyncio.run(atelier.run_conduit("demo", {}))


def fake_twilio(monkeypatch, responses):
    requests = []

    class Connection:
        def __init__(self, host, timeout):
            assert host == "api.twilio.com" and timeout > 0

        def request(self, method, path, body, headers):
            assert method == "POST" and path == f"/2010-04-01/Accounts/{SID}/Messages.json"
            assert headers["Authorization"] == "Basic " + base64.b64encode(
                f"{SID}:test-only-secret".encode()
            ).decode()
            requests.append(parse_qs(body))

        def getresponse(self):
            response = responses.pop(0)
            if isinstance(response, Exception):
                raise response
            return SimpleNamespace(status=response, read=lambda: json.dumps({"sid": RECEIPT}).encode())

        def close(self):
            pass

    monkeypatch.setattr("flow_atelier.services.notification.twilio.HTTPSConnection", Connection)
    return requests


def test_retry_after_rejection_preserves_work_and_summary(finished, monkeypatch):
    atelier, run = finished
    requests = fake_twilio(monkeypatch, [429, 201])
    cli = CliRunner()
    result = cli.invoke(app, ["notify", run, "--link", PR, "--link", "http://localhost/runs/demo",
                              "--link", "/tmp/output.txt"])
    assert result.exit_code == 0, result.output
    assert cli.invoke(app, ["notify", run]).exit_code == 0
    assert len(requests) == 2 and requests[0] == requests[1]
    form = requests[1]
    assert form["To"] == ["whatsapp:" + TO]
    assert form["From"] == ["whatsapp:+12025550111"]
    assert form["ContentSid"] == ["HX" + "c" * 32]
    assert json.loads(form["ContentVariables"][0]) == {"1": run, "2": "completed", "3": PR}
    assert atelier.list_flows() == [run]
    assert atelier.get_outputs(run)["work"].strip() == run
    logs = atelier.get_flow_logs(run)
    assert sum(entry.task == "work" for entry in logs) == 1
    assert "accepted" in logs[-1].output and RECEIPT in logs[-1].output
    assert "retryable" in [entry.extra.get("delivery") for entry in logs]
    assert "test-only-secret" not in atelier.store.journal(run).path.read_text()


def test_uncertain_response_blocks_repeat(finished, monkeypatch):
    atelier, run = finished
    requests = fake_twilio(monkeypatch, [TimeoutError()])
    for _ in range(2):
        result = CliRunner().invoke(app, ["notify", run, "--status", "drafts ready", "--link", PR])
        assert result.exit_code == 1 and "uncertain" in result.output, result.output
    assert len(requests) == 1
    assert "uncertain" in atelier.get_flow_logs(run)[-1].output


def test_later_retry_reuses_links_without_rerunning_work(finished, monkeypatch):
    atelier, run = finished
    requests = fake_twilio(monkeypatch, [429, 429, 201])
    monkeypatch.setattr("flow_atelier.modules.notify.time.sleep", lambda _: None)
    cli = CliRunner()
    assert cli.invoke(app, ["notify", run, "--link", PR]).exit_code == 1
    assert cli.invoke(app, ["notify", run]).exit_code == 0
    assert requests[0] == requests[1] == requests[2]
    assert atelier.store.read_progress(run).status == "completed"

