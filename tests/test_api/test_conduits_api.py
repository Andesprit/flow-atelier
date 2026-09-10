"""/conduits CRUD round-trip tests."""
from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from flow_atelier.core.atelier import Atelier
from flow_atelier.core.settings import AtelierSettings
from flow_atelier.services.api.app import FastApiServer


@pytest.fixture
async def client(tmp_path, _isolate_global_atelier_dir):
    """Yield an httpx client wired to a fresh Atelier instance.

    :param tmp_path: pytest temp directory fixture.
    :param _isolate_global_atelier_dir: isolated global atelier dir fixture.
    """
    global_dir: Path = _isolate_global_atelier_dir
    atelier = Atelier(
        settings=AtelierSettings(
            atelier_dir=tmp_path / ".atelier",
            global_atelier_dir=global_dir,
        ),
    )
    app = FastApiServer().create_app(atelier)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as c:
        yield c


def _payload(name: str = "release_notes", description: str = "Generate notes"):
    """Build a baseline conduit payload.

    :param name: conduit name to embed in the payload.
    :param description: conduit description to embed in the payload.
    """
    return {
        "name": name,
        "description": description,
        "tasks": [
            {
                "name": "echo",
                "description": "echo",
                "task": "echo hi",
                "tool": "tool:bash",
                "depends_on": [],
            }
        ],
    }


# ---------------------------------------------------------------- read


async def test_list_conduits_returns_empty_array(client):
    """Verify GET /conduits returns an empty array initially.

    :param client: httpx client fixture.
    """
    resp = await client.get("/conduits")
    assert resp.status_code == 200
    assert resp.json() == []


async def test_get_unknown_conduit_returns_404(client):
    """Verify GET /conduits/<unknown> returns 404 with an error body.

    :param client: httpx client fixture.
    """
    resp = await client.get("/conduits/ghost")
    assert resp.status_code == 404
    assert "detail" in resp.json()


# ---------------------------------------------------------------- create


async def test_create_conduit_returns_201_and_persists(client):
    """Verify POST /conduits returns 201 and the conduit is listed afterwards.

    :param client: httpx client fixture.
    """
    resp = await client.post("/conduits", json=_payload())
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["name"] == "release_notes"

    listed = await client.get("/conduits")
    items = listed.json()
    assert any(item["name"] == "release_notes" for item in items)


async def test_create_conduit_collision_returns_409(client):
    """Verify creating a conduit with a duplicate name returns 409.

    :param client: httpx client fixture.
    """
    await client.post("/conduits", json=_payload())
    resp = await client.post("/conduits", json=_payload())
    assert resp.status_code == 409


async def test_create_conduit_invalid_returns_400(client):
    """Verify an invalid conduit payload returns 400 or 422.

    :param client: httpx client fixture.
    """
    bad = {"name": "x", "description": "d"}  # missing tasks
    resp = await client.post("/conduits", json=bad)
    assert resp.status_code in (400, 422)


async def test_create_conduit_traversal_name_rejected(client, tmp_path):
    """Verify a path-traversal conduit name is rejected and writes nothing outside.

    :param client: httpx client fixture.
    :param tmp_path: pytest temp directory fixture.
    """
    outside = tmp_path / "evil"
    resp = await client.post(
        "/conduits", json=_payload(name=f"../../../../{outside}")
    )
    assert resp.status_code in (400, 422)
    assert not outside.exists()


async def test_update_conduit_traversal_rename_rejected(client, tmp_path):
    """Verify renaming a conduit to a traversal name is rejected.

    :param client: httpx client fixture.
    :param tmp_path: pytest temp directory fixture.
    """
    await client.post("/conduits", json=_payload())
    outside = tmp_path / "evil"
    resp = await client.patch(
        "/conduits/release_notes",
        json={"name": f"../../../../{outside}"},
    )
    assert resp.status_code in (400, 422)
    assert not outside.exists()


# ---------------------------------------------------------------- update


async def test_update_conduit_partial_returns_200(client):
    """Verify PATCH /conduits/<name> applies partial updates.

    :param client: httpx client fixture.
    """
    await client.post("/conduits", json=_payload(description="old"))
    resp = await client.patch(
        "/conduits/release_notes", json={"description": "new"}
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["description"] == "new"


async def test_update_unknown_returns_404(client):
    """Verify patching an unknown conduit returns 404.

    :param client: httpx client fixture.
    """
    resp = await client.patch(
        "/conduits/ghost", json={"description": "anything"}
    )
    assert resp.status_code == 404


async def test_update_rename_to_existing_returns_409(client):
    """Verify renaming a conduit onto an existing name returns 409.

    :param client: httpx client fixture.
    """
    await client.post("/conduits", json=_payload(name="conduit_a"))
    await client.post("/conduits", json=_payload(name="conduit_b", description="B"))
    resp = await client.patch("/conduits/conduit_a", json={"name": "conduit_b"})
    assert resp.status_code == 409
    # conduit_b's description is unchanged.
    got = await client.get("/conduits/conduit_b")
    assert got.json()["description"] == "B"


# ---------------------------------------------------------------- delete


async def test_delete_conduit_returns_204(client):
    """Verify DELETE /conduits/<name> returns 204 and removes the conduit.

    :param client: httpx client fixture.
    """
    await client.post("/conduits", json=_payload())
    resp = await client.delete("/conduits/release_notes")
    assert resp.status_code == 204
    listed = await client.get("/conduits")
    assert listed.json() == []


async def test_delete_unknown_returns_404(client):
    """Verify deleting an unknown conduit returns 404.

    :param client: httpx client fixture.
    """
    resp = await client.delete("/conduits/ghost")
    assert resp.status_code == 404


# ---------------------------------------------------------------- open-path


async def test_open_path_invokes_opener(client, monkeypatch, tmp_path):
    """Verify POST /conduits/open-path opens a recorded flow run path.

    :param client: httpx client fixture.
    :param monkeypatch: pytest monkeypatch fixture.
    :param tmp_path: pytest temp directory fixture.
    """
    await client.post("/conduits", json=_payload())
    # Register the path as a known flow run_path so the opener accepts it.
    atelier = client._transport.app.state.atelier
    run_path = tmp_path / "runs"
    run_path.mkdir()
    _fid = atelier.store.create_flow("release_notes", {})
    _progress = atelier.store.read_progress(_fid)
    _progress.run_path = str(run_path)
    atelier.store.write_progress(_fid, _progress)
    calls = []

    class _FakeProc:
        def poll(self):
            """Return ``None`` to indicate the fake process is still running."""
            return None

    def fake_popen(cmd, *a, **kw):
        """Capture the command and return a fake process handle.

        :param cmd: subprocess command argv passed to Popen.
        :param a: positional args forwarded by callers (ignored).
        :param kw: keyword args forwarded by callers (ignored).
        """
        calls.append(cmd)
        return _FakeProc()

    monkeypatch.setattr("subprocess.Popen", fake_popen)
    resp = await client.post(
        "/conduits/open-path",
        json={"conduit_name": "release_notes", "run_path": str(run_path)},
    )
    assert resp.status_code == 200
    assert resp.json() == {"opened": True}
    assert calls


async def test_open_path_refuses_unknown_path(client, monkeypatch):
    """Verify POST /conduits/open-path refuses a path with no recorded flow.

    :param client: httpx client fixture.
    :param monkeypatch: pytest monkeypatch fixture.
    """
    await client.post("/conduits", json=_payload())
    calls = []
    monkeypatch.setattr("subprocess.Popen", lambda *a, **k: calls.append(a))
    resp = await client.post(
        "/conduits/open-path",
        json={"conduit_name": "release_notes", "run_path": "/tmp/not-a-flow"},
    )
    assert resp.status_code == 200
    assert resp.json() == {"opened": False}
    assert not calls


async def test_conduit_reads_carry_a_default_run_path(client, tmp_path):
    """Verify reads expose the server's working directory as `run_path`.

    The UI requires a working directory before it will run anything, and only
    the server knows where it was started; without this the field starts empty
    and every user hand-types an absolute path.

    :param client: httpx client fixture bound to the API.
    :param tmp_path: pytest temp directory fixture.
    """
    await client.post("/conduits", json=_payload())
    expected = str(tmp_path)

    listed = await client.get("/conduits")
    fetched = await client.get("/conduits/release_notes")

    assert listed.json()[0]["run_path"] == expected
    assert fetched.json()["run_path"] == expected


async def test_run_path_is_not_persisted_to_disk(client, tmp_path):
    """Verify `run_path` stays response-only and never lands in conduit.yaml.

    :param client: httpx client fixture bound to the API.
    :param tmp_path: pytest temp directory fixture.
    """
    await client.post("/conduits", json={**_payload(), "run_path": "/somewhere/else"})
    on_disk = (tmp_path / ".atelier/conduits/release_notes/conduit.yaml").read_text()

    assert "run_path" not in on_disk
    assert (await client.get("/conduits/release_notes")).json()["run_path"] == str(tmp_path)


async def test_patch_accepts_the_input_shape_reads_return(client):
    """Verify a conduit read from the API can be PATCHed straight back.

    The UI edits what GET returned, so the object form of `inputs`
    ({description, default}) must be accepted on update — a string-only
    annotation made every save fail with 422 for any conduit declaring inputs.

    :param client: httpx client fixture bound to the API.
    """
    payload = {**_payload(), "inputs": {"repo_url": "The git repo URL"}}
    await client.post("/conduits", json=payload)

    fetched = (await client.get("/conduits/release_notes")).json()
    assert fetched["inputs"]["repo_url"] == {
        "description": "The git repo URL",
        "default": None,
    }

    resp = await client.patch(
        "/conduits/release_notes",
        json={"description": "edited", "inputs": fetched["inputs"]},
    )

    assert resp.status_code == 200
    assert resp.json()["inputs"]["repo_url"]["description"] == "The git repo URL"


async def test_patch_still_accepts_the_string_shorthand(client):
    """Verify the plain-string shorthand for inputs keeps working on update.

    :param client: httpx client fixture bound to the API.
    """
    await client.post("/conduits", json=_payload())

    resp = await client.patch(
        "/conduits/release_notes", json={"inputs": {"branch": "Branch to deploy"}}
    )

    assert resp.status_code == 200
    assert resp.json()["inputs"]["branch"]["description"] == "Branch to deploy"


async def test_interaction_policy_round_trip_patch_and_remove(client):
    """Keep policies across saves, validate replacements, and allow explicit removal."""
    policy = {
        "questions": "hybrid", "permissions": "human",
        "supervisor": {"tool": "harness:codex", "instructions": "Keep the existing API"},
    }
    created = await client.post("/conduits", json={**_payload(), "interaction": policy})
    assert created.status_code == 201, created.text
    got = (await client.get("/conduits/release_notes")).json()
    assert got["interaction"]["supervisor"]["instructions"] == "Keep the existing API"
    saved = await client.patch("/conduits/release_notes", json={"description": "Updated"})
    assert saved.json()["interaction"] == got["interaction"]
    invalid = await client.patch("/conduits/release_notes", json={
        "interaction": {"questions": "supervisor"},
    })
    assert invalid.status_code == 422
    removed = await client.patch("/conduits/release_notes", json={"interaction": None})
    assert removed.status_code == 200, removed.text
    assert (await client.get("/conduits/release_notes")).json()["interaction"] is None
