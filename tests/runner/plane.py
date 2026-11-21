"""In-memory reference peer. All authenticated operations derive tenant from the bearer."""

import threading
import uuid

from flow_atelier.schemas.runner import Ack, Dispatch, Identity, Job


class Plane:
    origin = "https://runner.test"

    def __init__(self):
        self.lock = threading.Lock()
        self.tokens = {}
        self.runners = {}
        self.now = 1000
        self.jobs = {}
        self.drop_next = None

    def enqueue(self, tenant, candidates, conduit, hostile=False):
        """Each eligible runner has its own locally assigned folder ID."""
        with self.lock:
            jid = uuid.uuid4().hex
            self.jobs[jid] = dict(tenant=tenant, candidates=candidates, conduit=conduit,
                                  owner=None, state="queued", hostile=hostile)
            return jid

    def job_view(self, tenant, jid):
        with self.lock:
            job = self.jobs[jid]
            if job["tenant"] != tenant:
                raise PermissionError("foreign job")
            if job["state"] == "started":
                owner = next(r for r in self.runners.values() if r["id"] == job["owner"])
                if self.now - owner["heartbeat"] >= 60:
                    return "unknown"
            return job["state"]

    def _reply(self, operation, response):
        if self.drop_next == operation:
            self.drop_next = None
            raise ConnectionError("response lost after commit")
        return response

    def invite(self, tenant):
        token = uuid.uuid4().hex
        self.tokens[token] = tenant
        return token

    def revoke(self, tenant):
        for runner in self.runners.values():
            if runner["tenant"] == tenant:
                runner["revoked"] = True

    def view(self, tenant):
        with self.lock:
            return [{**{k: row[k] for k in ("id", "folders", "heartbeat")},
                     "online": self.now - row["heartbeat"] < 60 and not row["revoked"]}
                    for row in self.runners.values() if row["tenant"] == tenant]

    def post(self, route, message, token):
        with self.lock:
            if route == "pair":
                tenant = self.tokens.pop(token, None)
                if tenant is None:
                    raise PermissionError("invalid invitation")
                token, rid = uuid.uuid4().hex, uuid.uuid4().hex
                self.runners[token] = dict(id=rid, tenant=tenant, folders=[], heartbeat=self.now,
                                           revoked=False)
                return Identity(id=rid, token=token)
            row = self.runners.get(token)
            if row is None or row["revoked"]:
                raise PermissionError("invalid identity")
            if route == "poll":
                row.update(heartbeat=self.now, folders=[f.model_dump() for f in message.folders])
                claim = None
                for jid, job in self.jobs.items():
                    rid = row["id"]
                    if (not message.claim or job["tenant"] != row["tenant"]
                            or rid not in job["candidates"] or job["owner"] not in (None, rid)
                            or job["state"] not in ("queued", "claimed", "started")):
                        continue
                    folder = job["candidates"][rid]
                    eligible = any(f.id == folder and job["conduit"] in f.conduits
                                   for f in message.folders)
                    if job["owner"] is None and not job["hostile"] and not eligible:
                        continue
                    job["owner"] = rid
                    if job["state"] == "queued":
                        job["state"] = "claimed"
                    claim = Job(id=jid, folder=folder, conduit=job["conduit"])
                    break
                return self._reply(route, Dispatch(job=claim, after=0))
            if route == "result":
                job = self.jobs[message.job]
                if job["tenant"] != row["tenant"] or job["owner"] != row["id"]:
                    raise PermissionError("foreign claim")
                if job["state"] not in ("claimed", "started", message.status):
                    raise ValueError("terminal job")
                job["state"] = message.status
                return self._reply(message.status, Ack())
            raise AssertionError(route)
