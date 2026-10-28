"""Finite crash-boundary refinement checks; runs real product code in subprocesses."""
from __future__ import annotations

import argparse
import asyncio
import json
import hashlib
import importlib
import os
from pathlib import Path
import sqlite3
import subprocess
import sys

BOUNDARIES = tuple(f"{name}_{edge}" for name in
                   ("begin", "insert", "commit", "dispatch", "send", "after_observe", "finish")
                   for edge in ("before", "after"))


def append(path: Path, value: dict):
    with path.open("a") as stream:
        stream.write(json.dumps(value, sort_keys=True) + "\n")
        stream.flush()
        os.fsync(stream.fileno())


def checkpoint(directory: Path, selected: str, name: str):
    if selected == name:
        append(directory / "crash.jsonl", {"boundary": name})
        os._exit(75)


async def worker(directory: Path, selected: str):
    from decision_browser.audit import Journal
    from decision_browser.contracts import Budget, TaskSpec
    from decision_browser.controller import Controller
    from decision_browser.drivers.mock import MockBrowserDriver
    from decision_browser.providers.mock import MockDecisionProvider

    source = Path(os.environ["FORMAL_SOURCE_ROOT"]).resolve() / "src"
    imported = {}
    for name in ("audit", "contracts", "executor", "controller", "drivers.mock"):
        module = importlib.import_module(f"decision_browser.{name}")
        location = Path(module.__file__).resolve()
        assert location.is_relative_to(source), f"wrong product import: {location}, expected under {source}"
        imported[name] = {"path": str(location), "sha256": hashlib.sha256(location.read_bytes()).hexdigest()}
    append(directory / "imports.jsonl", imported)
    task = TaskSpec(task_id="formal-one-operation", budget=Budget(max_steps=1))
    path = directory / "journal.sqlite3"
    journal = Journal(path)
    append(directory / "recovery.jsonl", {"states": journal.db.execute(
        "SELECT state FROM operations").fetchall(), "unresolved": journal.unresolved(task.task_id)})

    class Connection:
        def __init__(self, connection):
            self.connection = connection

        def __getattr__(self, name):
            return getattr(self.connection, name)

        def execute(self, sql, parameters=()):
            label = None
            if sql == "BEGIN IMMEDIATE":
                label = "begin"
            elif sql.startswith("INSERT INTO operations"):
                label = "insert"
            elif sql == "COMMIT":
                label = "commit"
            elif sql.startswith("UPDATE operations SET state=?"):
                label = {"DISPATCHED": "dispatch", "VERIFIED": "finish"}.get(parameters[0])
            if label:
                checkpoint(directory, selected, f"{label}_before")
            result = self.connection.execute(sql, parameters)
            if label:
                checkpoint(directory, selected, f"{label}_after")
            return result

    journal.db = Connection(journal.db)

    class Driver(MockBrowserDriver):
        observations = 0

        async def observe(self, task, lease):
            self.observations += 1
            if self.observations == 3:
                checkpoint(directory, selected, "after_observe_before")
            result = await super().observe(task, lease)
            if self.observations == 3:
                checkpoint(directory, selected, "after_observe_after")
            return result

        async def execute(self, action, lease):
            checkpoint(directory, selected, "send_before")
            key = journal.operation_key(task, action)
            with sqlite3.connect(path) as reader:
                row = reader.execute("SELECT state FROM operations WHERE operation_key=?", (key,)).fetchone()
            append(directory / "sends.jsonl", {"key": key, "durable_state": row[0] if row else None})
            checkpoint(directory, selected, "send_after")
            return await super().execute(action, lease)

    result = await Controller(Driver(), MockDecisionProvider(), journal=journal, mode="enforced").run(task)
    append(directory / "results.jsonl", {"state": result.state, "reason": result.reason})
    journal.close()


def records(path: Path):
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def check(directory: Path, source: Path):
    env = dict(os.environ, PYTHONPATH=str(source.resolve() / "src"), PYTHONDONTWRITEBYTECODE="1", FORMAL_SOURCE_ROOT=str(source.resolve()))
    cases = []
    for selected in ("none", *BOUNDARIES):
        case = directory / selected
        case.mkdir(parents=True)
        command = [sys.executable, str(Path(__file__).resolve()), "--worker", str(case)]
        for attempt, boundary in enumerate((selected, "none", "none")):
            completed = subprocess.run([*command, "--boundary", boundary], env=env, capture_output=True, text=True, timeout=15)
            (case / f"attempt-{attempt}.log").write_text(completed.stdout + completed.stderr)
            expected = 75 if attempt == 0 and selected != "none" else 0
            if completed.returncode != expected:
                raise AssertionError(f"{selected}: exit {completed.returncode}, expected {expected}; see {case}")
        sends = records(case / "sends.jsonl")
        assert all(row["durable_state"] == "DISPATCHED" for row in sends), f"DurableDispatchBeforeSend: {case} {sends}"
        assert len({row["key"] for row in sends}) == len(sends), f"NoDuplicateSend: {case} {sends}"
        if selected != "none":
            assert records(case / "crash.jsonl") == [{"boundary": selected}], f"unreached crash: {selected}"
        recovered = records(case / "recovery.jsonl")
        durable_crash = selected in {"commit_after", "dispatch_before", "dispatch_after", "send_before",
                                     "send_after", "after_observe_before", "after_observe_after", "finish_before"}
        if durable_crash:
            assert recovered[1] == {"states": [["UNKNOWN_EFFECT"]], "unresolved": True}, recovered
            assert records(case / "results.jsonl")[0]["reason"] == "RECONCILIATION_REQUIRED_NO_REPLAY"
        expected_sends = 0 if selected in {"commit_after", "dispatch_before", "dispatch_after", "send_before"} else 1
        assert len(sends) == expected_sends, f"{selected}: expected {expected_sends} sends, got {sends}"
        cases.append({"boundary": selected, "sends": len(sends), "attempts": 3})
    return {"cases": cases, "case_count": len(cases), "process_count": len(cases) * 3}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--worker", type=Path)
    parser.add_argument("--boundary", default="none", choices=("none", *BOUNDARIES))
    parser.add_argument("--source", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.worker:
        asyncio.run(worker(args.worker, args.boundary))
    else:
        result = check(args.output, args.source)
        (args.output / "summary.json").write_text(json.dumps(result, indent=2) + "\n")
        print(json.dumps(result))


if __name__ == "__main__":
    main()
