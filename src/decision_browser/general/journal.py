from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path


def encoded(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


class GeneralJournal:
    def __init__(self, path: Path):
        self.db = sqlite3.connect(path, isolation_level=None)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS general_runs (
                task_id TEXT PRIMARY KEY, task_digest TEXT NOT NULL, status TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS general_operations (
                task_id TEXT NOT NULL, step INTEGER NOT NULL, action TEXT NOT NULL,
                observation TEXT NOT NULL, state TEXT NOT NULL, result TEXT,
                PRIMARY KEY(task_id, step));
            CREATE TABLE IF NOT EXISTS general_events (
                sequence INTEGER PRIMARY KEY, task_id TEXT NOT NULL, kind TEXT NOT NULL, payload TEXT NOT NULL);
        """)

    def begin(self, task_id, task):
        digest = hashlib.sha256(encoded(task).encode()).hexdigest()
        if self.db.execute("SELECT 1 FROM general_runs WHERE task_id=?", (task_id,)).fetchone():
            self.db.execute("UPDATE general_operations SET state='UNKNOWN_EFFECT' "
                            "WHERE task_id=? AND state IN ('PREPARED','DISPATCHED')", (task_id,))
            raise ValueError("task ID already used; automatic replay is forbidden")
        self.db.execute("INSERT INTO general_runs VALUES(?,?,'RUNNING')", (task_id, digest))

    def prepare(self, task_id, step, action, observation):
        self.db.execute("INSERT INTO general_operations VALUES(?,?,?,?, 'PREPARED',NULL)",
                        (task_id, step, encoded(action), encoded(observation)))

    def transition(self, task_id, step, state, result=None):
        previous = "PREPARED" if state == "DISPATCHED" else "DISPATCHED"
        if state not in {"DISPATCHED", "ACKNOWLEDGED", "UNKNOWN_EFFECT"}:
            raise ValueError("invalid dispatch state")
        changed = self.db.execute("UPDATE general_operations SET state=?,result=? "
                                 "WHERE task_id=? AND step=? AND state=?",
                                 (state, encoded(result), task_id, step, previous))
        if changed.rowcount != 1:
            raise RuntimeError("invalid durable dispatch transition")

    def event(self, task_id, kind, value):
        self.db.execute("INSERT INTO general_events(task_id,kind,payload) VALUES(?,?,?)",
                        (task_id, kind, encoded(value)))

    def finish(self, task_id, status):
        self.db.execute("UPDATE general_runs SET status=? WHERE task_id=?", (status, task_id))

    def close(self):
        self.db.close()
