"""Check exact-origin navigation and native confirmation in fresh owned Chrome."""
from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
from importlib.metadata import version
import json
from pathlib import Path
import subprocess
import time
from unittest.mock import patch
from uuid import uuid4

from decision_browser.general.browser import GeneralBrowser
from decision_browser.general.journal import GeneralJournal
from decision_browser.general.models import ACTION, Task
from decision_browser.web.environment import ManagedChrome, ROOT, save


ORIGIN = "http://127.0.0.1:8766"
FORBIDDEN = "http://localhost:8766/boundary-target"


def state(container):
    script = "import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:8766/state', timeout=5).read().decode())"
    result = subprocess.run(["docker", "exec", container, "python", "-c", script],
                            capture_output=True, text=True, check=True, timeout=10, encoding='utf-8')
    return json.loads(result.stdout)


async def check(manifest, output, case):
    from browser_use.browser._cdp_timeout import TimeoutWrappedCDPClient
    from cdp_use.cdp.registry import EventRegistry

    task = Task(url=ORIGIN + "/boundaries", goal="Verify browser boundary " + case,
                allowed_origins=[ORIGIN])
    browser = GeneralBrowser(manifest["endpoint"], task, {})
    journal = GeneralJournal(output / "journal.sqlite3")
    task_id = str(uuid4())
    journal.begin(task_id, task.model_dump())
    proof = {"case": case, "passed": False, "task": task.model_dump(), "task_id": task_id,
             "before": state(manifest["container"]), "commands": [], "events": [], "steps": []}
    assert not any(proof["before"]["counters"].values()), proof["before"]
    original = TimeoutWrappedCDPClient.send_raw

    async def traced(client, method, params=None, session_id=None):
        record = {"method": method, "session_id": session_id, "time": time.monotonic()}
        if method.startswith(("Fetch.", "Input.", "Page.handleJavaScriptDialog", "Page.navigate")):
            record["params"] = params
        proof["commands"].append(record)
        result = await original(client, method, params, session_id)
        record["returned"] = True
        return result

    original_event = EventRegistry.handle_event

    async def traced_event(registry, method, params, session_id=None):
        if method in ("Fetch.requestPaused", "Page.javascriptDialogOpening", "Page.javascriptDialogClosed"):
            proof["events"].append({"kind": method, "params": params, "session_id": session_id,
                                    "time": time.monotonic()})
        return await original_event(registry, method, params, session_id)

    async def dispatch(action, observed, timeout=15):
        step = len(proof["steps"])
        record = {"action": action.model_dump()}
        proof["steps"].append(record)
        journal.prepare(task_id, step, action.model_dump(), observed)
        journal.transition(task_id, step, "DISPATCHED")
        try:
            result = await asyncio.wait_for(browser.execute(action, observed), timeout)
        except Exception as error:
            record.update(state="UNKNOWN_EFFECT", error=type(error).__name__, detail=str(error))
            journal.transition(task_id, step, "UNKNOWN_EFFECT", record)
        else:
            record.update(state="ACKNOWLEDGED", result=result)
            journal.transition(task_id, step, "ACKNOWLEDGED", result)
        return record

    with patch.object(TimeoutWrappedCDPClient, "send_raw", traced), \
            patch.object(EventRegistry, "handle_event", traced_event):
        try:
            await asyncio.wait_for(browser.start(), 30)
            observed = await asyncio.wait_for(browser.observe(), 15)
            result = await dispatch(ACTION.validate_python({"kind": "navigate", "url": task.url}), observed)
            assert result["state"] == "ACKNOWLEDGED", result
            observed = await asyncio.wait_for(browser.observe(), 15)
            save(output / "observation.json", observed)
            image = base64.b64decode(await asyncio.wait_for(browser.screenshot(), 10), validate=True)
            assert image.startswith(b"\x89PNG\r\n\x1a\n")
            (output / "before.png").write_bytes(image)
            proof["screenshot_sha256"] = hashlib.sha256(image).hexdigest()
            control = {"redirect": "redirect-out", "popup": "popup-out", "confirm": "confirm-action"}[case]
            indices = [int(index) for index, node in observed["nodes"].items()
                       if node["attrs"].get("data-testid") == control]
            assert len(indices) == 1, observed["dom"]
            result = await dispatch(ACTION.validate_python({"kind": "click", "index": indices[0]}),
                                    observed, timeout=1 if case == "confirm" else 15)
            await asyncio.sleep(1)
            proof["after"] = state(manifest["container"])
            counters = proof["after"]["counters"]
            assert counters["boundary_target"] == counters["confirm"] == 0, proof["after"]
            assert not any(c["method"] == "Page.handleJavaScriptDialog" for c in proof["commands"])
            clicks = [c for c in proof["commands"] if c["method"] == "Input.dispatchMouseEvent"]
            assert [c["params"]["type"] for c in clicks] == ["mousePressed", "mouseReleased"], clicks
            if case == "confirm":
                dialogs = [e for e in proof["events"] if e["kind"] == "Page.javascriptDialogOpening"]
                assert dialogs and all(e["params"]["type"] == "confirm" for e in dialogs), dialogs
                assert len({e["params"]["frameId"] for e in dialogs}) == 1, dialogs
                assert not any(e["kind"] == "Page.javascriptDialogClosed" for e in proof["events"])
                assert result["state"] in ("ACKNOWLEDGED", "UNKNOWN_EFFECT"), result
                if result["state"] == "UNKNOWN_EFFECT":
                    assert result["error"] == "TimeoutError", result
                assert counters["redirect_out"] == 0
            else:
                blocked = [e for e in proof["events"] if e["kind"] == "Fetch.requestPaused"
                           and e["params"]["request"]["url"] == FORBIDDEN]
                assert blocked, proof["events"]
                for request in blocked:
                    assert request["params"]["resourceType"] == "Document"
                    request_id = request["params"]["requestId"]
                    commands = [c for c in proof["commands"] if c["method"].startswith("Fetch.")
                                and (c.get("params") or {}).get("requestId") == request_id]
                    assert len(commands) == 1 and commands[0]["method"] == "Fetch.failRequest", commands
                    assert commands[0].get("returned") and commands[0]["params"]["errorReason"] == "BlockedByClient"
                assert counters["redirect_out"] == (1 if case == "redirect" else 0)
            proof["passed"] = True
            journal.finish(task_id, "VERIFIED")
        except Exception as error:
            proof["error"] = {"type": type(error).__name__, "message": str(error)}
            journal.finish(task_id, "FAILED")
        finally:
            try:
                await asyncio.wait_for(browser.close(), 5)
                proof["browser_close"] = "returned"
            except Exception as error:
                proof["browser_close"] = {"type": type(error).__name__, "message": str(error)}
            proof["final_state"] = state(manifest["container"])
            if (any(c["method"] == "Page.handleJavaScriptDialog" for c in proof["commands"])
                    or proof["final_state"]["counters"]["boundary_target"]
                    or proof["final_state"]["counters"]["confirm"]):
                proof.update(passed=False, error={"type": "AssertionError", "message": "Boundary changed during close"})
                journal.finish(task_id, "FAILED")
            proof["journal_operations"] = journal.db.execute(
                "SELECT step,action,state,result FROM general_operations ORDER BY step").fetchall()
            journal.close()
            save(output / "proof.json", proof)
    return proof


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True)
    args = parser.parse_args(argv)
    output = Path(args.output)
    output.mkdir(parents=True, exist_ok=False)
    sources = ["verification/generic_browser/check_boundaries.py", "verification/generic_browser/fixture_server.py",
               "fixtures/generic_browser/boundaries.html", "src/decision_browser/general/browser.py",
               "src/decision_browser/general/session.py", "src/decision_browser/general/journal.py",
               "src/decision_browser/web/environment.py", "verification/generic_browser/Dockerfile",
               "verification/generic_browser/proxy.py", "verification/web_benchmark/container.py"]
    proof = {"passed": False, "model_calls": 0, "cases": {}, "source_sha256": {
        path: hashlib.sha256((ROOT / path).read_bytes()).hexdigest() for path in sources},
        "versions": {name: version(name) for name in ("browser-use", "cdp-use", "decision-browser")}}
    try:
        for case in ("redirect", "popup", "confirm"):
            case_output = output / case
            case_output.mkdir()
            with ManagedChrome(case_output / "chrome", generic=True) as managed:
                result = asyncio.run(check(managed.manifest, case_output, case))
            cleanup = json.loads((case_output / "chrome" / "cleanup.json").read_text(encoding='utf-8'))
            proof["cases"][case] = {"passed": result["passed"], "cleanup": cleanup,
                                    "proof": str(case_output / "proof.json")}
            assert cleanup["absent"], cleanup
        proof["passed"] = all(case["passed"] for case in proof["cases"].values())
    except Exception as error:
        proof["error"] = {"type": type(error).__name__, "message": str(error)}
    finally:
        save(output / "proof.json", proof)
    print(json.dumps({"passed": proof["passed"], "output": str(output)}))
    return 0 if proof["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
