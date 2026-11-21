"""In-memory reference peer. All authenticated operations derive tenant from the bearer."""

import threading
import uuid

from flow_atelier.schemas.runner import Dispatch, Identity


class Plane:
    origin = "https://runner.test"

    def __init__(self):
        self.lock = threading.Lock()
        self.tokens = {}
        self.runners = {}
        self.now = 1000
        self.hostile = None

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
                return Dispatch(job=self.hostile if message.claim else None, after=0)
            raise AssertionError(route)
