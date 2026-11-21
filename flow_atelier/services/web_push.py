"""Local device subscriptions and completion delivery for the single-user server."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sqlite3
import time
from contextlib import closing, contextmanager
from datetime import datetime
from pathlib import Path
from uuid import uuid4

from flow_atelier.core.atelier import Atelier

logger = logging.getLogger(__name__)
TTL = 86400


class WebPush:
    def __init__(self, global_dir: Path) -> None:
        self.finished_watermark = 0.0
        self.path = global_dir / "web-push" / "devices.sqlite3"

    @contextmanager
    def connect(self):
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.close(os.open(self.path, os.O_CREAT | os.O_WRONLY, 0o600))
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("PRAGMA foreign_keys = ON")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS vapid (id INTEGER PRIMARY KEY, pem BLOB NOT NULL);
                CREATE TABLE IF NOT EXISTS devices (
                    endpoint TEXT PRIMARY KEY, keys TEXT NOT NULL, since REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS device_status (
                    endpoint TEXT PRIMARY KEY REFERENCES devices ON DELETE CASCADE,
                    origin TEXT, last_error TEXT);
                CREATE TABLE IF NOT EXISTS notices (
                    flow TEXT PRIMARY KEY, token TEXT UNIQUE NOT NULL, finished REAL NOT NULL);
                CREATE INDEX IF NOT EXISTS notice_finished ON notices(finished);
                CREATE TABLE IF NOT EXISTS deliveries (
                    endpoint TEXT REFERENCES devices ON DELETE CASCADE,
                    flow TEXT REFERENCES notices ON DELETE CASCADE, PRIMARY KEY(endpoint, flow));
            """)
            yield db

    def public_key(self) -> str:
        from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
        from py_vapid import Vapid, b64urlencode

        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT pem FROM vapid WHERE id=1").fetchone()
            if row:
                key = Vapid.from_pem(row[0])
            else:
                key = Vapid()
                key.generate_keys()
                db.execute("INSERT INTO vapid VALUES (1, ?)", (key.private_pem(),))
        return b64urlencode(
            key.public_key.public_bytes(Encoding.X962, PublicFormat.UncompressedPoint)
        )

    def subscribe(self, endpoint: str, keys: dict[str, str], origin: str | None) -> None:
        with self.connect() as db:
            db.execute(
                "INSERT INTO devices VALUES (?, ?, ?) ON CONFLICT(endpoint) DO UPDATE "
                "SET keys=excluded.keys",
                (endpoint, json.dumps(keys), time.time()),
            )

            db.execute(
                "INSERT INTO device_status(endpoint, origin) VALUES (?, ?) "
                "ON CONFLICT(endpoint) DO UPDATE SET origin=excluded.origin",
                (endpoint, origin),
            )

    def last_error(self, endpoint: str) -> str | None:
        with self.connect() as db:
            row = db.execute(
                "SELECT last_error FROM device_status WHERE endpoint=?", (endpoint,)
            ).fetchone()
        return row[0] if row else None

    def unsubscribe(self, endpoint: str) -> None:
        with self.connect() as db:
            db.execute("DELETE FROM devices WHERE endpoint=?", (endpoint,))

    def resolve(self, token: str) -> str | None:
        if not self.path.exists():
            return None
        with self.connect() as db:
            row = db.execute("SELECT flow FROM notices WHERE token=?", (token,)).fetchone()
        return row[0] if row else None

    def deliver(self, atelier: Atelier) -> None:
        if not self.path.exists():
            return
        with self.connect() as db:
            cutoff = time.time() - TTL
            days = atelier.settings.cleanup_period_days
            if days:
                db.execute("DELETE FROM notices WHERE finished < ?", (time.time() - days * TTL,))
            devices = db.execute(
                "SELECT endpoint, keys, since, origin FROM devices LEFT JOIN device_status "
                "USING(endpoint)"
            ).fetchall()
            row = db.execute("SELECT pem FROM vapid WHERE id=1").fetchone()
            # Another serve process can release a retryable claim after we pass its run.
            (pending,) = db.execute(
                "SELECT MIN(finished) FROM notices n WHERE finished >= ? AND EXISTS ("
                "SELECT 1 FROM devices d WHERE d.since <= n.finished AND NOT EXISTS ("
                "SELECT 1 FROM deliveries WHERE endpoint=d.endpoint AND flow=n.flow))",
                (cutoff,),
            ).fetchone()
        if not devices or not row:
            return
        import requests
        from py_vapid import Vapid
        from pywebpush import WebPushException, webpush

        # A run can start before the watermark and finish while we scan.
        scan_started = time.time()
        after = max(cutoff, self.finished_watermark, min(device[2] for device in devices))
        if pending is not None:
            after = min(after, pending)
        key = Vapid.from_pem(row[0])
        with requests.Session() as session, self.connect() as db:
            session.max_redirects = 0
            for run in atelier.list_prior_flows(modified_since=int(after)):
                if run.status not in {"completed", "failed", "stopped"} or not run.finished_at:
                    continue
                try:
                    finished = datetime.fromisoformat(run.finished_at).timestamp()
                except ValueError:
                    continue
                if finished < cutoff:
                    continue
                for endpoint, keys, since, origin in devices:
                    if finished < since:
                        continue
                    db.execute(
                        "INSERT OR IGNORE INTO notices VALUES (?, ?, ?)",
                        (run.flow_id, uuid4().hex, finished),
                    )
                    # Claim under the same transaction as the subscription check.
                    claim = db.execute(
                        "INSERT OR IGNORE INTO deliveries SELECT endpoint, ? "
                        "FROM devices WHERE endpoint=? AND since=?",
                        (run.flow_id, endpoint, since),
                    )
                    db.commit()
                    if not claim.rowcount:
                        continue
                    (token,) = db.execute(
                        "SELECT token FROM notices WHERE flow=?", (run.flow_id,)
                    ).fetchone()
                    error = None
                    subject = atelier.settings.vapid_subject or origin
                    try:
                        if not subject:
                            raise ValueError("Missing VAPID subject")
                        webpush(
                            subscription_info={"endpoint": endpoint, "keys": json.loads(keys)},
                            data=json.dumps({"run_id": token, "status": run.status}),
                            vapid_private_key=key,
                            vapid_claims={"sub": subject},
                            requests_session=session,
                            ttl=TTL,
                            timeout=5,
                        )
                    except WebPushException as exc:
                        status = exc.response.status_code if exc.response is not None else None
                        if status in {404, 410}:
                            db.execute("DELETE FROM devices WHERE endpoint=? AND since=?",
                                       (endpoint, since))
                            db.commit()
                            continue
                        error = f"Completion push rejected (HTTP {status})."
                        if status == 429 or (status and status >= 500):
                            db.execute(
                                "DELETE FROM deliveries WHERE endpoint=? AND flow=?",
                                (endpoint, run.flow_id),
                            )
                    except requests.RequestException:
                        # Delivery is uncertain. Keep the claim to avoid duplicate notices.
                        error = "Completion push transport failed."
                    except ValueError:
                        error = ("Re-enable notifications over HTTPS or set vapidSubject."
                                 if not subject else "Invalid Web Push configuration.")
                    if error:
                        logger.warning("%s", error)
                    db.execute(
                        "INSERT INTO device_status(endpoint, last_error) SELECT endpoint, ? "
                        "FROM devices WHERE endpoint=? AND since=? "
                        "ON CONFLICT(endpoint) DO UPDATE SET last_error=excluded.last_error",
                        (error, endpoint, since),
                    )
                    db.commit()
        # All final records visible at scan start are accounted for, including ties.
        self.finished_watermark = scan_started

    async def watch(self, atelier: Atelier) -> None:
        while True:
            try:
                await asyncio.to_thread(self.deliver, atelier)
            except Exception as exc:
                # Provider exceptions can contain device URLs and keys.
                logger.warning("Completion push unavailable (%s)", type(exc).__name__)
            await asyncio.sleep(15)
