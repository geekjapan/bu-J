
from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path


def encoded(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


class WebJournal:
    def __init__(self, path: str | Path):
        self.db = sqlite3.connect(str(path), isolation_level=None)
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS web_operations (
                task_id TEXT NOT NULL, step INTEGER NOT NULL, identity TEXT NOT NULL,
                state TEXT NOT NULL, reason TEXT NOT NULL DEFAULT '',
                PRIMARY KEY(task_id, step)
            );
            CREATE TABLE IF NOT EXISTS web_events (
                sequence INTEGER PRIMARY KEY, timestamp TEXT NOT NULL,
                task_id TEXT NOT NULL, event_type TEXT NOT NULL, payload TEXT NOT NULL
            );
        """)

    def unresolved(self, task_id):
        return self.db.execute(
            "SELECT 1 FROM web_operations WHERE task_id=? AND state NOT IN ('VERIFIED','NOT_SENT') LIMIT 1",
            (task_id,),
        ).fetchone() is not None

    def recover(self, task_id):
        self.db.execute(
            "UPDATE web_operations SET state='UNKNOWN_EFFECT',reason='RESTART_RECONCILIATION_REQUIRED' "
            "WHERE task_id=? AND state IN ('PREPARED','DISPATCHED')", (task_id,),
        )

    def prepare(self, task_id, step, action, page):
        identity = hashlib.sha256(encoded({
            "action": action, "url": page.get("url"), "marker": page.get("marker"),
            "page_key": page.get("page_key"), "fingerprint": page.get("fingerprint"),
            "metadata": page.get("metadata", {}).get(str(action.get("node"))),
        }).encode()).hexdigest()
        self.db.execute("INSERT INTO web_operations(task_id,step,identity,state) VALUES(?,?,?,'PREPARED')",
                        (task_id, step, identity))

    def transition(self, task_id, step, state, reason=""):
        expected = "PREPARED" if state == "DISPATCHED" else "DISPATCHED"
        if state not in {"DISPATCHED", "VERIFIED", "UNKNOWN_EFFECT", "NOT_SENT"}:
            raise ValueError("invalid operation state")
        cursor = self.db.execute(
            "UPDATE web_operations SET state=?,reason=? WHERE task_id=? AND step=? AND state=?",
            (state, reason, task_id, step, expected),
        )
        if cursor.rowcount != 1:
            raise RuntimeError("INVALID_OPERATION_TRANSITION")

    def event(self, task_id, event_type, **payload):
        self.db.execute("INSERT INTO web_events(timestamp,task_id,event_type,payload) VALUES(?,?,?,?)",
                        (datetime.now(timezone.utc).isoformat(), task_id, event_type, encoded(payload)))

    def close(self):
        self.db.close()
