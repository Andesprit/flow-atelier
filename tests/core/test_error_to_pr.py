import asyncio
import json
import shutil
import subprocess
import sys

import httpx
import pytest

from flow_atelier.core.atelier import Atelier
from flow_atelier.core.settings import AtelierSettings
from flow_atelier.services import github_draft
from flow_atelier.services.api.error_hooks import create_webhook_app
from flow_atelier.services.executor import error_pr
from flow_atelier.services.notification.twilio import Twilio
from tests.services.api.test_error_hooks import signed

pytestmark = pytest.mark.skipif(
    sys.platform not in {"linux", "darwin"}, reason="POSIX worker profile"
)


def git(folder, *args):
    return subprocess.check_output(["git", "-C", folder, *args], text=True).strip()


@pytest.mark.parametrize(
    ("reproduces", "native", "unsafe_change"),
    [
        (True, False, None),
        (True, False, ".editor-settings"),
        (True, False, "deletion"),
        (True, False, "symlink"),
        (False, False, None),
        pytest.param(
            True,
            True,
            None,
            marks=pytest.mark.skipif(not shutil.which("codex"), reason="Codex unavailable"),
        ),
    ],
)
async def test_webhook_to_verified_draft(tmp_path, monkeypatch, reproduces, native, unsafe_change):
    project = tmp_path / "orders"
    project.mkdir()
    monkeypatch.chdir(project)
    git(project, "init", "-b", "main")
    git(project, "remote", "add", "origin", "https://github.com/fixture/orders.git")
    (project / "orders.py").write_text("def total(): return 2\n")
    (project / "notes").write_text("fixture")
    (project / "test_orders.py").write_text(
        "import unittest\nfrom orders import total\n"
        "class Orders(unittest.TestCase):\n"
        "    def test_returns_count(self): self.assertIsInstance(total(), int)\n"
    )
    git(project, "add", "orders.py", "test_orders.py", "notes")
    git(
        project,
        "-c",
        "user.name=Fixture",
        "-c",
        "user.email=a@example.test",
        "commit",
        "-m",
        "fixture",
    )
    parent = git(project, "rev-parse", "main")
    python = "/usr/bin/python3" if native else sys.executable
    home = AtelierSettings().global_atelier_dir
    (home / "settings.json").write_text(
        json.dumps(
            {
                "webhookSources": {"app": {"project": str(project), "conduit": "error-to-pr"}},
                "webhookSecrets": {"app": "fixture key"},
                "notificationRecipient": "+1234567890",
                "notificationProvider": "twilio",
                "draftCiDisabledRepositories": ["fixture/orders"],
                "errorPrProjects": {
                    str(project.resolve()): {
                        "repository": "fixture/orders",
                        "test_file": "test_{id}.py",
                        "regression": [python, "{test}"],
                        "suite": [python, "-m", "unittest", "discover"],
                    }
                },
            }
        )
    )
    (home / "twilio.json").write_text(
        json.dumps(
            {
                "account_sid": "AC" + "1" * 32,
                "auth_token": "fixture",
                "sender": "+1234567899",
                "content_sid": "HX" + "1" * 32,
            }
        )
    )
    atlas = Atelier()
    atlas.install_package("builtin:platform")
    starts, prs, sends = [], [], []

    class FakeWorker:
        def __init__(self, workspace):
            self.workspace = workspace

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def command(self, argv):
            return subprocess.run(argv, cwd=self.workspace, capture_output=True, text=True)

        async def turn(self, prompt):
            checkout = self.workspace / "checkout"
            if not starts:
                assert "order total" in prompt
                name = next(
                    word
                    for word in prompt.split()
                    if word.startswith("test_") and word.endswith(".py")
                )
                starts.append(name)
                expected = 3 if reproduces else 2
                (checkout / name).write_text(
                    "import unittest\nfrom orders import total\nclass Regression(unittest.TestCase):\n"
                    f"    def test_total(self): self.assertEqual(total(), {expected})\n"
                    "if __name__ == '__main__': unittest.main()\n"
                )
            else:
                (checkout / "orders.py").write_text("def total(): return 3\n")
                if unsafe_change == "deletion":
                    (checkout / "notes").unlink()
                elif unsafe_change == "symlink":
                    (checkout / "link").symlink_to("notes")
                elif unsafe_change:
                    (checkout / unsafe_change).touch()

    if native:
        isolated_login = tmp_path / "empty-codex-home"
        isolated_login.mkdir()
        monkeypatch.setenv("CODEX_HOME", str(isolated_login))
        monkeypatch.setattr(error_pr.WorkerSandbox, "turn", FakeWorker.turn)
    else:
        monkeypatch.setattr(error_pr, "WorkerSandbox", FakeWorker)
    url = "https://github.com/fixture/orders/pull/17"

    gh = tmp_path / "bin" / "gh"
    gh.parent.mkdir()
    gh.touch(mode=0o755)
    monkeypatch.setattr(github_draft, "SYSTEM_PATH", f"{gh.parent}:{github_draft.SYSTEM_PATH}")
    remote = tmp_path / "fake-github.git"
    git(tmp_path, "clone", "--bare", str(project), str(remote))

    def fake_remote(argv, env, snapshot):
        if argv[1:3] == ["pr", "list"]:
            assert argv[argv.index("--repo") + 1] == "fixture/orders"
            if not prs:
                return "[]"
            return json.dumps(
                [
                    {
                        "headRefOid": git(remote, "rev-parse", prs[0]),
                        "isCrossRepository": False,
                        "url": url,
                    }
                ]
            )
        if argv[1:3] == ["api", "repos/fixture/orders"]:
            return "private"
        if argv[1] == "api":
            assert argv[2] == "repos/fixture/orders/git/ref/heads/main"
            return parent
        if argv[0] == "git" and "push" in argv:
            lease, destination, refspec = argv[-3:]
            sha, ref = refspec.split(":")
            assert destination == "https://github.com/fixture/orders.git"
            assert lease == f"--force-with-lease={ref}:" and ref.startswith(
                "refs/heads/atelier/errors/"
            )
            git(snapshot, "push", lease, str(remote), refspec)
            assert "[skip ci]" in git(remote, "log", "-1", "--format=%B", sha)
            assert git(remote, "rev-parse", sha + "^") == parent
            assert "return 3" in git(remote, "show", sha + ":orders.py")
            assert "assertEqual" in git(remote, "show", sha + ":" + starts[0])
            return ""
        assert argv[1:3] == ["pr", "create"] and "--draft" in argv
        assert argv[argv.index("--base") + 1] == "main"
        prs.append(argv[argv.index("--head") + 1])
        return url

    monkeypatch.setattr(github_draft, "_remote", fake_remote)
    monkeypatch.setattr(
        Twilio,
        "send",
        lambda self, recipient, run, status, links: (
            sends.append((recipient, run, links)) or "SM" + "1" * 32
        ),
    )
    receiver = create_webhook_app(AtelierSettings())
    data = b'{"fingerprint":"wrong-total","message":"order total"}'
    async with (
        receiver.router.lifespan_context(receiver),
        httpx.AsyncClient(
            base_url="http://local",
            transport=httpx.ASGITransport(app=receiver),
        ) as client,
    ):

        async def post():
            reply = await client.post("/hooks/errors/app", content=data, headers=signed(data))
            assert reply.status_code == 202
            return reply.json()

        first = await post()
        async with asyncio.timeout(90 if native else 15):
            while (result := await post())["status"] not in ("failed", "completed"):
                await asyncio.sleep(0.05)
        assert result["run_id"] == first["run_id"]
    run = result["run_id"]
    assert git(project, "rev-parse", "main") == parent
    assert git(project, "status", "--porcelain") == ""
    assert len(starts) == 1, [entry.stderr for entry in atlas.store.read_logs(run)]
    assert git(remote, "rev-parse", "main") == parent
    if unsafe_change:
        assert result["status"] == "failed" and not prs and not sends
        assert "protected" in str(atlas.store.read_logs(run))
    elif reproduces:
        assert result["status"] == "completed", atlas.store.read_logs(run)
        assert prs == ["atelier/errors/" + run]
        assert atlas.get_outputs(run)["publish"] == url
        assert sends == [("+1234567890", run, [url])]
        snapshot = atlas.store.journal(run).side / "error-to-pr.git"
        with pytest.raises(ValueError, match="draftCiDisabledRepositories"):
            github_draft.publish_draft(snapshot, "fixture/orders", prs[0], ["other/repo"])
        assert github_draft.publish_draft(snapshot, "fixture/orders", prs[0], ["fixture/orders"]) == url
        monkeypatch.setattr(github_draft, "_remote", lambda *args: "public")
        with pytest.raises(ValueError, match="private"):
            github_draft.publish_draft(snapshot, "fixture/orders", prs[0], ["fixture/orders"])
        with pytest.raises(ValueError, match="destination"):
            github_draft.publish_draft(snapshot, "fixture/orders", "main", [])
        assert len(prs) == 1
    else:
        assert result["status"] == "failed" and not prs and not sends
        assert "did not reproduce" in str(atlas.store.read_logs(run))
