"""Run a natural-language task in your existing browser."""
from __future__ import annotations

import argparse
import asyncio
from contextlib import ExitStack
import hashlib
import json
import os
from pathlib import Path
from uuid import uuid4

from .files import collect_downloads, stage_uploads
from .journal import GeneralJournal
from .models import Task
from .provider import GeneralModelClient
from .runtime import run
from .connection import browser_identity, list_tabs, resolve_endpoint


def save(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding='utf-8')


async def execute(args, task, output, journal):
    from .browser import GeneralBrowser
    endpoint = await asyncio.to_thread(resolve_endpoint, args.cdp_url)
    result = {"status": "FAILED", "task_id": args.task_id}
    with ExitStack() as resources:
        import portalocker
        locks = Path.home() / ".cache" / "bu-j" / "locks"
        locks.mkdir(parents=True, exist_ok=True, mode=0o700)
        identity = hashlib.sha256(browser_identity(endpoint).encode()).hexdigest()
        resources.enter_context(portalocker.Lock(locks / (identity + ".lock"), timeout=0))
        uploads, receipts = stage_uploads(output, task.uploads)
        browser = GeneralBrowser(endpoint, task, uploads, tab_id=args.tab_id)
        model = GeneralModelClient(*(os.environ.get(name, "") for name in (
            "AZURE_ENDPOINT", "AZURE_DEPLOYMENT_NAME", "AZURE_TEXT_ENDPOINT", "AZURE_TEXT_DEPLOYMENT")),
            max_calls=task.max_steps, max_text_calls=20)
        try:
            model.authenticate()
            result = await run(task, args.task_id, browser, model, journal, output)
        except Exception as error:
            result["error"] = {"type": type(error).__name__, "message": str(error)[:500]}
        finally:
            for name, cleanup in (("downloads", lambda: collect_downloads(Path(args.downloads_dir) if args.downloads_dir else None,
                browser.session.downloads if browser.session is not None else [], output)),
                                  ("browser", browser.close)):
                try:
                    if name == "browser":
                        await cleanup()
                    else:
                        result[name] = cleanup()
                except Exception as error:
                    result[name + "_error"] = type(error).__name__
                    if result["status"] != "UNKNOWN_EFFECT":
                        result["status"] = "FAILED"
            result["decision_calls"] = model.calls
            result["text_calls"] = model.text_calls
            result["total_tokens"] = model.total_tokens
            result["uploads"] = receipts
            model.close()
            save(output / "result.json", result)
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--task", help="JSON task with url, goal and optional allowed_origins/acceptance/uploads")
    parser.add_argument("--output", help="new evidence directory")
    parser.add_argument("--journal", help="durable SQLite path; default OUTPUT/journal.sqlite3")
    parser.add_argument("--task-id", default=str(uuid4()))
    parser.add_argument("--cdp-url", help="existing Chrome CDP URL; otherwise discover the running browser")
    parser.add_argument("--tab-id", help="use this existing tab without initial navigation")
    parser.add_argument("--list-tabs", action="store_true", help="list existing tab IDs without running a task")
    parser.add_argument("--downloads-dir", help="existing browser download folder; copy only matched completed downloads")
    args = parser.parse_args(argv)
    if args.list_tabs:
        try:
            print(json.dumps(asyncio.run(list_tabs(resolve_endpoint(args.cdp_url)))))
            return 0
        except Exception as error:
            print(json.dumps({"status": "FAILED", "error": {"type": type(error).__name__, "message": str(error)[:500]}}))
            return 1
    if not args.task or not args.output:
        parser.error("--task and --output are required unless --list-tabs is used")
    output = Path(args.output)
    journal = None
    created = False
    result = {"status": "FAILED", "task_id": args.task_id}
    try:
        task = Task.model_validate_json(Path(args.task).read_text(encoding='utf-8'))
        output.mkdir(parents=True, exist_ok=False)
        created = True
        save(output / "task.json", task.model_dump())
        journal = GeneralJournal(Path(args.journal) if args.journal else output / "journal.sqlite3")
        result = asyncio.run(execute(args, task, output, journal))
    except Exception as error:
        if created and (output / "result.json").exists():
            result = json.loads((output / "result.json").read_text(encoding='utf-8'))
        if result["status"] != "UNKNOWN_EFFECT":
            result["status"] = "FAILED"
        result["error"] = {"type": type(error).__name__, "message": str(error)[:500]}
        if created:
            save(output / "result.json", result)
    finally:
        if journal is not None:
            journal.close()
    print(json.dumps({"status": result["status"], "output": str(output / "result.json")}))
    return 0 if result["status"] in {"VERIFIED", "REPORTED"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
