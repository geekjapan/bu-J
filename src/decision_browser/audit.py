from __future__ import annotations

import json
import os
import sqlite3
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from decision_browser.contracts import ActionProposal, Observation, TaskSpec, canonical, digest


class PersistenceError(RuntimeError):
    pass


class Journal:
    def __init__(self, path: str | Path = ":memory:"):
        self.db = sqlite3.connect(str(path), isolation_level=None)
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS operations (
                operation_key TEXT PRIMARY KEY, task_id TEXT NOT NULL,
                state TEXT NOT NULL, action_id TEXT NOT NULL,
                fencing_token INTEGER NOT NULL, reason TEXT NOT NULL DEFAULT ''
            );
            CREATE TABLE IF NOT EXISTS events (
                sequence INTEGER PRIMARY KEY, timestamp TEXT NOT NULL,
                task_id TEXT NOT NULL, event_type TEXT NOT NULL, payload TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS approval_nonces (nonce TEXT PRIMARY KEY);
            CREATE TABLE IF NOT EXISTS observations (
                observation_id TEXT PRIMARY KEY, task_id TEXT NOT NULL, snapshot TEXT NOT NULL
            );
        """)
        self.db.execute("UPDATE operations SET state='UNKNOWN_EFFECT', reason='RESTART_RECONCILIATION_REQUIRED' WHERE state IN ('PREPARED','DISPATCHED')")

    def close(self):
        self.db.close()

    def event(self, task_id: str, event_type: str, **payload: object):
        try:
            self.db.execute(
                "INSERT INTO events(timestamp,task_id,event_type,payload) VALUES(?,?,?,?)",
                (datetime.now(timezone.utc).isoformat(), task_id, event_type, canonical(payload).decode()),
            )
        except sqlite3.Error as error:
            raise PersistenceError("AUDIT_PERSISTENCE_FAILED") from error

    def export_events(self, path: str | Path):
        destination = Path(path)
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=destination.parent, delete=False) as output:
                temporary = Path(output.name)
                for sequence, timestamp, task_id, event_type, payload in self.db.execute(
                    "SELECT sequence,timestamp,task_id,event_type,payload FROM events ORDER BY sequence"
                ):
                    event = {"schema_version": "1.0", "event_id": str(sequence), "timestamp": timestamp,
                             "task_id": task_id, "event_type": event_type, "metadata": json.loads(payload)}
                    output.write(canonical(event).decode() + "\n")
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, destination)
        except (OSError, sqlite3.Error) as error:
            raise PersistenceError("AUDIT_EXPORT_FAILED") from error
        finally:
            if temporary is not None and temporary.exists():
                temporary.unlink()

    def save_observation(self, observation: Observation):
        records = tuple(record.model_copy(update={"id": digest(record.id), "name": "[redacted]", "updated_at": "[redacted]"})
                        for record in observation.facts.records)
        facts = observation.facts.model_copy(update={"query": "[redacted]" if observation.facts.query else "", "records": records})
        elements = tuple(element.model_copy(update={"element_ref": digest(element.element_ref),
                         "role": element.role if element.role in {"button", "textbox", "link"} else "[redacted]",
                         "name": "[redacted]"}) for element in observation.elements)
        redacted = observation.model_copy(update={"facts": facts, "elements": elements,
                                                  "redaction_profile": "persisted-redacted-v1"})
        try:
            self.db.execute("INSERT INTO observations VALUES(?,?,?)", (observation.observation_id,
                            observation.task_id, redacted.model_dump_json()))
        except sqlite3.Error as error:
            raise PersistenceError("OBSERVATION_PERSISTENCE_FAILED") from error

    def load_observations(self, task_id: str) -> tuple[Observation, ...]:
        return tuple(Observation.model_validate_json(row[0]) for row in self.db.execute(
            "SELECT snapshot FROM observations WHERE task_id=? ORDER BY rowid", (task_id,)))

    @staticmethod
    def operation_key(task: TaskSpec, action: ActionProposal) -> str:
        return digest({"task": task.task_id, "tenant": task.tenant_id, "actor": task.principal_id,
                       "workflow": task.workflow_version, "kind": action.kind,
                       "parameters": action.parameters.model_dump(mode="json")})

    def prepare(self, task: TaskSpec, action: ActionProposal, fencing_token: int) -> str:
        key = self.operation_key(task, action)
        try:
            self.db.execute("BEGIN IMMEDIATE")
            self.db.execute("INSERT INTO operations VALUES(?,?,?,?,?,?)",
                            (key, task.task_id, "PREPARED", action.action_id, fencing_token, ""))
            self.db.execute("COMMIT")
        except sqlite3.Error as error:
            if self.db.in_transaction:
                self.db.execute("ROLLBACK")
            raise PersistenceError("DUPLICATE_OR_UNWRITABLE_OPERATION") from error
        return key

    def transition(self, key: str, state: str, reason: str = ""):
        if state not in {"DISPATCHED", "VERIFIED", "FAILED", "UNKNOWN_EFFECT"}:
            raise ValueError("unknown journal state")
        expected = "PREPARED" if state == "DISPATCHED" else "DISPATCHED"
        try:
            cursor = self.db.execute("UPDATE operations SET state=?,reason=? WHERE operation_key=? AND state=?",
                                     (state, reason, key, expected))
            if cursor.rowcount != 1:
                raise PersistenceError("INVALID_OPERATION_TRANSITION")
        except sqlite3.Error as error:
            raise PersistenceError("JOURNAL_PERSISTENCE_FAILED") from error

    def unresolved(self, task_id: str) -> bool:
        return self.db.execute("SELECT 1 FROM operations WHERE task_id=? AND state IN ('PREPARED','DISPATCHED','UNKNOWN_EFFECT') LIMIT 1", (task_id,)).fetchone() is not None

    def consume_nonce(self, nonce: str):
        try:
            self.db.execute("INSERT INTO approval_nonces VALUES(?)", (nonce,))
        except sqlite3.Error as error:
            raise PersistenceError("APPROVAL_REPLAY_OR_PERSISTENCE_FAILURE") from error
