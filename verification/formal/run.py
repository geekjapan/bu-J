#!/usr/bin/env python3
"""Run pinned TLC, real crash boundaries, and behavioral mutation controls."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.request

ROOT = Path(__file__).resolve().parents[2]
HERE = Path(__file__).resolve().parent
JAR_SHA256 = "fc6878c603f0fdc18b9fe3e5bf51860cb90ab5b1154e3c371660c036d6bdd63e"
JAR_URL = "https://github.com/tlaplus/tlaplus/releases/download/v1.8.0/tla2tools.jar"
COVERAGE = HERE / "coverage.json"


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def hashes():
    record = json.loads(COVERAGE.read_text())
    return {name: digest(ROOT / name) for name in record["source_sha256"]}


def verified_commit(root, source_hashes):
    head = subprocess.run(["git", "rev-parse", "--verify", "HEAD"], cwd=root, capture_output=True)
    if head.returncode != 0:
        return None
    for name, expected in source_hashes.items():
        stored = subprocess.run(["git", "show", f"HEAD:{name}"], cwd=root, capture_output=True)
        if stored.returncode != 0 or hashlib.sha256(stored.stdout).hexdigest() != expected:
            return None
    return head.stdout.decode().strip()


def command(arguments, output, cwd=ROOT, expected=0, marker=None):
    try:
        completed = subprocess.run(arguments, cwd=cwd, text=True, capture_output=True, timeout=120)
    except subprocess.TimeoutExpired as error:
        partial = error.stdout or b""
        if isinstance(partial, bytes):
            partial = partial.decode(errors="replace")
        output.write_text("COMMAND " + json.dumps([str(a) for a in arguments]) + "\n" + partial + "\nTIMEOUT after 120 seconds\n")
        raise RuntimeError(f"command timed out; inspect {output}") from error
    output.write_text("COMMAND " + json.dumps([str(a) for a in arguments]) + "\n" + completed.stdout + completed.stderr)
    if completed.returncode != expected or (marker and marker not in completed.stdout + completed.stderr):
        raise RuntimeError(f"unexpected result ({completed.returncode}); inspect {output}")
    return completed.stdout + completed.stderr


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--record-sync", action="store_true", help="after reviewing model/source correspondence, record current source hashes and real HEAD")
    args = parser.parse_args()
    record = json.loads(COVERAGE.read_text())
    current = hashes()
    if args.record_sync:
        commit = verified_commit(ROOT, current)
        record.update(sync_commit=commit, source_sha256=current,
                      sync_note="Covered source matches this commit." if commit else
                      "Working-tree source hashes only; HEAD is absent or does not match all covered files.")
        COVERAGE.write_text(json.dumps(record, indent=2) + "\n")
        print("Recorded source sync; this does not establish model correctness. Run verification next.")
        return
    if current != record["source_sha256"]:
        changed = [name for name in current if current[name] != record["source_sha256"][name]]
        raise RuntimeError(f"model/source sync review required: {changed}; read SKILL.md before --record-sync")
    tools = ROOT / ".audit/formal/tools"
    tools.mkdir(parents=True, exist_ok=True)
    jar = tools / "tla2tools-1.8.0.jar"
    if not jar.exists():
        downloaded = tools / "download.tmp"
        try:
            urllib.request.urlretrieve(JAR_URL, downloaded)
            if digest(downloaded) != JAR_SHA256:
                raise RuntimeError("TLC download checksum mismatch")
            os.replace(downloaded, jar)
        finally:
            downloaded.unlink(missing_ok=True)
    if digest(jar) != JAR_SHA256:
        raise RuntimeError(f"TLC checksum mismatch: {jar}")
    run = Path(tempfile.mkdtemp(prefix=datetime.now(timezone.utc).strftime("run-%Y%m%dT%H%M%SZ-"), dir=ROOT / ".audit/formal"))
    summary = {"evidence": str(run.relative_to(ROOT)), "source_sha256": current,
               "sync_commit": record["sync_commit"], "tool": {"release": "1.8.0", "sha256": JAR_SHA256, "url": JAR_URL}}
    print(f"Evidence: {run.relative_to(ROOT)}", flush=True)
    try:
        summary["java"] = command(["java", "-version"], run / "java.log").strip()
        summary["python"] = sys.version
        for name in ("DispatchSafety.tla", "DispatchSafety.cfg", "BrokenDispatch.cfg", "BrokenPrepare.cfg"):
            shutil.copy2(HERE / name, run / name)
        for config, invariant in (("DispatchSafety", None), ("BrokenDispatch", "DurableDispatchBeforeSend"), ("BrokenPrepare", "NoDuplicateSend")):
            arguments = ["java", "-Xmx512m", "-XX:+UseParallelGC", "-cp", str(jar), "tlc2.TLC", "-workers", "1", "-seed", "1", "-fp", "0", "-metadir", str(run / f"{config}-states"), "-config", f"{config}.cfg", "DispatchSafety.tla"]
            output = command(arguments, run / f"{config}.log", cwd=run, expected=12 if invariant else 0,
                             marker=f"Invariant {invariant} is violated" if invariant else "Model checking completed. No error has been found.")
            count = re.search(r"([\d,]+) states generated, ([\d,]+) distinct states found, ([\d,]+) states left on queue", output)
            if not count:
                raise RuntimeError(f"missing TLC state counts: {config}")
            summary[config] = {"generated": int(count[1].replace(",", "")), "distinct": int(count[2].replace(",", "")), "queue": int(count[3].replace(",", "")), "expected_violation": invariant}
            if not invariant and summary[config]["queue"] != 0:
                raise RuntimeError("baseline TLC search was incomplete")
        real = [sys.executable, str(HERE / "real_boundaries.py")]
        command([*real, "--source", str(ROOT), "--output", str(run / "baseline")], run / "baseline.log")
        summary["real_boundaries"] = json.loads((run / "baseline/summary.json").read_text())
        mutations = (
            ("remove-dispatch", "src/decision_browser/executor.py", '        self.journal.transition(key, "DISPATCHED")\n', '', "DurableDispatchBeforeSend"),
            ("replace-unique-operation", "src/decision_browser/audit.py", '"INSERT INTO operations VALUES(?,?,?,?,?,?)"', '"INSERT OR REPLACE INTO operations VALUES(?,?,?,?,?,?)"', "NoDuplicateSend"),
        )
        for name, file, old, new, invariant in mutations:
            with tempfile.TemporaryDirectory(prefix="scratch-", dir=run) as scratch:
                scratch = Path(scratch)
                shutil.copytree(ROOT / "src", scratch / "src", ignore=shutil.ignore_patterns("__pycache__"))
                target = scratch / file
                original = target.read_text()
                if original.count(old) != 1:
                    raise RuntimeError(f"mutation no longer applies exactly once: {name}")
                target.write_text(original.replace(old, new))
                (run / f"{name}-mutation.json").write_text(json.dumps({"file": file, "old": old, "new": new}, indent=2) + "\n")
                command([*real, "--source", str(scratch), "--output", str(run / name)], run / f"{name}.log", expected=1, marker=f"AssertionError: {invariant}:")
                summary[name] = {"rejected_by_behavior": invariant, "scratch_removed": True}
        command([*real, "--source", str(ROOT), "--output", str(run / "restored-baseline")], run / "restored-baseline.log")
        if hashes() != current:
            raise RuntimeError("product files changed during verification")
        summary["status"] = "PASS"
    except Exception as error:
        summary.update(status="FAIL", error=str(error))
        raise
    finally:
        (run / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
