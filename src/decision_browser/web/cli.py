"""Run a benchmark task only against an attested disposable Chrome."""

import argparse
from collections import Counter
import json
import os
from pathlib import Path

from .agent import WebAgent
from .browser import Browser
from .environment import validate_manifest
from .provider import ModelClient
from .tasks import get_task, verify_task


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--task", choices=("flights", "wikipedia", "hotel"), required=True)
    parser.add_argument("--flight-date", default="2027-09-20")
    for name in ("manifest", "journal", "task-id", "output"):
        parser.add_argument("--" + name, required=True)
    args = parser.parse_args(argv)
    browser = model = agent = None
    result = {"status": "FAILED", "reason": "INITIALIZATION_FAILED", "independent_verification": {"passed": False}}
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    try:
        manifest = validate_manifest(args.manifest)
        task = get_task(args.task, args.flight_date)
        browser = Browser(task, manifest["endpoint"])
        model = ModelClient(*(os.environ.get(name, "") for name in (
            "AZURE_ENDPOINT", "AZURE_DEPLOYMENT_NAME", "AZURE_TEXT_ENDPOINT", "AZURE_TEXT_DEPLOYMENT")),
            max_calls=int(os.environ.get("WEB_DECISION_CALL_LIMIT", "120")),
            max_text_calls=int(os.environ.get("WEB_TEXT_CALL_LIMIT", "20")))
        model.authenticate()
        agent = WebAgent(task, browser, model, args.journal, args.task_id)
        before = Counter(browser.cdp_counts)
        result = agent.run()
        after = Counter(browser.cdp_counts)
        result.setdefault("cdp_counts_run", dict(after - before))
        result.setdefault("cdp_counts_run_bounds", "WebAgent.run, including initial observation; final verification and PNG excluded")
        result["browser_version"] = browser.browser_version
        result["independent_verification"] = verify_task(task, browser.observe())
        result["calls"] = model.calls
        result["model_text_calls"] = model.text_calls
        result["total_tokens"] = model.total_tokens
        screenshot = output.with_suffix(".png")
        try:
            screenshot.write_bytes(browser.screenshot())
            result["screenshot"] = str(screenshot)
        except Exception as error:
            result["diagnostic_error"] = type(error).__name__
        result["cdp_counts_total"] = dict(browser.cdp_counts)
    except Exception as error:
        result.update(status="FAILED", reason="RUNTIME_ERROR", exception_type=type(error).__name__)
    finally:
        for resource in (agent, model, browser):
            if resource is not None:
                try:
                    resource.close()
                except Exception as error:
                    result.update(status="FAILED", reason="CLEANUP_FAILED", exception_type=type(error).__name__)
        output.write_text(json.dumps(result, indent=2, ensure_ascii=False, allow_nan=False) + "\n")
    success = result["status"] == "SUCCEEDED" and result.get("independent_verification", {}).get("passed") is True
    print(json.dumps({"status": result["status"], "verified": success, "output": str(output)}))
    return 0 if success else 1


if __name__ == "__main__":
    raise SystemExit(main())
