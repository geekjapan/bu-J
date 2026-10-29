"""Run a natural-language web task in an owned disposable Chrome."""
from __future__ import annotations

import argparse
import asyncio
from contextlib import ExitStack, nullcontext
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
from ..web.environment import ManagedChrome, validate_manifest


def save(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n")


async def execute(args, task, output, journal):
    from .browser import GeneralBrowser
    context = ManagedChrome(output / "chrome", generic=True) if args.launch else nullcontext()
    result = {"status": "FAILED", "task_id": args.task_id}
    with context as managed, ExitStack() as resources:
        manifest = validate_manifest(managed.manifest_path if managed else args.manifest, generic=True)
        import portalocker
        locks = Path.home() / ".cache" / "bu-j" / "locks"
        locks.mkdir(parents=True, exist_ok=True, mode=0o700)
        identity = hashlib.sha256(manifest["container"].encode()).hexdigest()
        resources.enter_context(portalocker.Lock(locks / (identity + ".lock"), timeout=0))
        uploads, receipts = stage_uploads(manifest["container"], task.uploads)
        browser = GeneralBrowser(manifest["endpoint"], task, uploads)
        model = GeneralModelClient(*(os.environ.get(name, "") for name in (
            "AZURE_ENDPOINT", "AZURE_DEPLOYMENT_NAME", "AZURE_TEXT_ENDPOINT", "AZURE_TEXT_DEPLOYMENT")),
            max_calls=task.max_steps, max_text_calls=20)
        try:
            model.authenticate()
            result = await run(task, args.task_id, browser, model, journal, output)
        except Exception as error:
            result["error"] = {"type": type(error).__name__, "message": str(error)[:500]}
        finally:
            for name, cleanup in (("downloads", lambda: collect_downloads(manifest["container"], output)),
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
    parser.add_argument("--task", required=True, help="JSON task with url, goal and optional allowed_origins/acceptance/uploads")
    parser.add_argument("--output", required=True, help="new evidence directory")
    parser.add_argument("--journal", help="durable SQLite path; default OUTPUT/journal.sqlite3")
    parser.add_argument("--task-id", default=str(uuid4()))
    browser = parser.add_mutually_exclusive_group(required=True)
    browser.add_argument("--launch", action="store_true", help="launch and clean an owned Docker Chrome")
    browser.add_argument("--manifest", help="attested running generic ManagedChrome manifest")
    args = parser.parse_args(argv)
    output = Path(args.output)
    journal = None
    created = False
    result = {"status": "FAILED", "task_id": args.task_id}
    try:
        task = Task.model_validate_json(Path(args.task).read_text())
        output.mkdir(parents=True, exist_ok=False)
        created = True
        save(output / "task.json", task.model_dump())
        journal = GeneralJournal(Path(args.journal) if args.journal else output / "journal.sqlite3")
        result = asyncio.run(execute(args, task, output, journal))
    except Exception as error:
        if created and (output / "result.json").exists():
            result = json.loads((output / "result.json").read_text())
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
