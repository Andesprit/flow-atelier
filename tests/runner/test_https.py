import asyncio
import json
import shutil
import socket
import ssl
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from typer.testing import CliRunner

from flow_atelier.cli import app
from flow_atelier.schemas.runner import ENDPOINTS, Pair, Record, Records
from flow_atelier.services.runner.client import Client
from flow_atelier.services.runner.local import Local
from flow_atelier.services.runner.worker import Worker
from tests.runner.plane import Plane
from tests.runner.test_registration import recipe, step


def test_https_cli_pairs_polls_without_inbound_socket(tmp_path, monkeypatch):
    openssl = shutil.which("openssl")
    if not openssl:
        pytest.skip("local TLS fixture needs openssl")
    cert, key = tmp_path / "cert", tmp_path / "key"
    subprocess.run([openssl, "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1",
                    "-subj", "/CN=localhost", "-addext", "subjectAltName=DNS:localhost",
                    "-addext", "basicConstraints=critical,CA:TRUE",
                    "-addext", "keyUsage=critical,keyCertSign,digitalSignature,keyEncipherment",
                    "-keyout", str(key), "-out", str(cert)], check=True, capture_output=True)
    plane = Plane()
    redirect = False

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_POST(self):
            route = self.path.removeprefix("/runner/v1/")
            if redirect:
                self.send_response(307)
                self.send_header("Location", "https://different.invalid/")
                self.end_headers()
                return
            assert self.path == "/runner/v1/" + route
            request = ENDPOINTS[route][0].model_validate_json(
                self.rfile.read(int(self.headers["Content-Length"])))
            token = self.headers["Authorization"].removeprefix("Bearer ")
            try:
                reply = plane.post(route, request, token)
            except PermissionError:
                self.send_error(403)
                return
            body = reply.model_dump(mode="json")
            if route == "pair":
                body["token"] = reply.token.get_secret_value()
            self.send_response(200)
            self.end_headers()
            self.wfile.write(json.dumps(body).encode())

    with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server:
        tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        tls.load_cert_chain(cert, key)
        server.socket = tls.wrap_socket(server.socket, server_side=True)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            def no_listen(*args):
                pytest.fail("runner tried to listen")
            monkeypatch.setattr(socket.socket, "bind", no_listen)
            origin = f"https://localhost:{server.server_port}"
            client = Client(origin, tmp_path / "user/runner")
            with pytest.raises(ssl.SSLCertVerificationError):
                client.post("pair", Pair(), "unused")
            monkeypatch.setenv("SSL_CERT_FILE", str(cert))
            monkeypatch.setenv("ATELIER_GLOBAL_ATELIER_DIR", str(tmp_path / "user"))
            token = plane.invite("one")
            cli = CliRunner()
            result = cli.invoke(app, ["runner", "pair", origin], input=token + "\n")
            assert result.exit_code == 0, result.exception
            assert token not in result.output
            result = cli.invoke(app, ["runner", "expose", str(tmp_path), "Manuals"])
            assert result.exit_code == 0, result.exception
            fid = result.output.strip()
            local = Local(tmp_path / "user")
            local.poll(client)
            rid = local.identity()["id"]
            assert plane.view("one")[0]["folders"][0]["id"] == fid
            recipe(tmp_path, "catalog", [step("tool:bash", "echo ok > marker")])
            approved = cli.invoke(app, ["runner", "approve", fid, "catalog"])
            assert approved.exit_code == 0, approved.exception
            jid = plane.enqueue("one", {rid: fid}, "catalog")

            async def finish_job():
                worker = Worker(local, client)
                await worker.tick()
                await worker.active
                await worker.tick()

            asyncio.run(finish_job())
            assert plane.job_view("one", jid) == "completed"
            assert (tmp_path / "marker").read_text() == "ok\n"
            assert plane.records_for("one", rid)[-1]["status"] == "completed"
            monkeypatch.setenv("WIRE_CANARY", "privateWireName")
            raw = Record(sequence=4, job=jid, folder=fid, kind="log",
                         status="completed", task="privateWireName")
            client.post("records", Records(records=[raw]), local.identity()["token"])
            assert "privateWireName" not in json.dumps(plane.traffic)
            redirect = True
            with pytest.raises(ValueError, match="307"):
                local.poll(client)
            redirect = False
            plane.revoke("one")
            with pytest.raises(PermissionError):
                local.poll(client)
        finally:
            server.shutdown()
            thread.join()
