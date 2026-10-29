from __future__ import annotations

import asyncio
import base64
import time
from pathlib import Path


def completion(task, page):
    expected = task.acceptance
    if expected is None:
        return {"verified": False, "reason": "No independent acceptance predicate supplied"}
    checks = {}
    if expected.url:
        checks["url"] = page["url"] == expected.url
    if expected.text:
        checks["text"] = expected.text in page["text"]
    return {"verified": all(checks.values()), "checks": checks}


async def run(task, task_id, browser, model, journal, output: Path):
    history, extracts = [], []
    result = {"task_id": task_id, "status": "FAILED", "history": history, "extracts": extracts,
              "answer": None, "completion": {"verified": False}}
    journal.begin(task_id, task.model_dump())
    deadline = time.monotonic() + task.timeout_seconds
    model.deadline = deadline

    def remaining():
        seconds = deadline - time.monotonic()
        if seconds <= 0:
            raise TimeoutError("task deadline exceeded")
        return seconds

    async def observe():
        timeout = min(30, remaining())
        return await asyncio.wait_for(browser.observe(), timeout)

    async def screenshot(step):
        timeout = min(15, remaining())
        value = await asyncio.wait_for(browser.screenshot(), timeout)
        (output / f"page-{step:03d}.png").write_bytes(base64.b64decode(value, validate=True))
        return value

    async def dispatch(step, action, page):
        timeout = min(30, remaining())
        journal.prepare(task_id, step, action.model_dump(), page)
        journal.transition(task_id, step, "DISPATCHED")
        try:
            receipt = await asyncio.wait_for(browser.execute(action, page), timeout)
            journal.transition(task_id, step, "ACKNOWLEDGED", receipt)
            return receipt
        except BaseException:
            result["status"] = "UNKNOWN_EFFECT"
            journal.transition(task_id, step, "UNKNOWN_EFFECT")
            raise

    try:
        await asyncio.wait_for(browser.start(), min(30, remaining()))
        from .models import Navigate
        if browser.tab_id is None:
            initial = await observe()
            await dispatch(0, Navigate(kind="navigate", url=task.url), initial)
        for step in range(1, task.max_steps + 1):
            page = await observe()
            image = await screenshot(step)
            remaining()
            candidates = await asyncio.to_thread(model.propose, task, page, history,
                                                 screenshot=image if task.vision else None)
            for action in candidates:
                if action.kind == "navigate":
                    task.check_url(action.url)
                index = getattr(action, "index", None)
                if index is not None and str(index) not in page["nodes"]:
                    raise ValueError("candidate uses unobserved element")
                if action.kind == "upload" and action.file not in task.uploads:
                    raise ValueError("candidate uses unapproved upload token")
            remaining()
            decision = await asyncio.to_thread(model.select, task, page, history, candidates)
            journal.event(task_id, "decision", {"step": step, "candidates": [a.model_dump() for a in candidates],
                                                "decision": decision, "observation": page["fingerprint"]})
            fresh = await observe()
            if fresh["fingerprint"] != page["fingerprint"]:
                journal.event(task_id, "stale", {"step": step})
                continue
            remaining()
            if not decision["accepted"]:
                result["status"] = "ABSTAINED"
                break
            action = candidates[int(decision["choice"])]
            entry = {"step": step, "action": action.model_dump()}
            if action.kind == "done":
                result["answer"] = action.answer
                result["completion"] = completion(task, fresh)
                result["status"] = ("VERIFIED" if result["completion"]["verified"] else
                                    "ACCEPTANCE_FAILED" if task.acceptance else "REPORTED")
                if task.output_schema:
                    if extracts:
                        result["data"] = extracts[-1]
                    else:
                        result["status"] = "ACCEPTANCE_FAILED"
                        result["completion"] = {"verified": False, "reason": "Required structured extraction missing"}
                history.append(entry)
                break
            if action.kind == "extract":
                timeout = min(30, remaining())
                content = await asyncio.wait_for(browser.content(action.start_from_char), timeout)
                remaining()
                data = await asyncio.to_thread(model.extract, task, content, action.query)
                extracts.append(data)
                entry["result"] = {"data": data, "truncated": content["truncated"],
                                   "next_start_char": content["next_start_char"]}
            elif action.kind == "screenshot":
                entry["result"] = {"path": f"page-{step:03d}.png"}
            else:
                entry["result"] = await dispatch(step, action, fresh)
            history.append(entry)
            journal.event(task_id, "result", entry)
        else:
            result["status"] = "STEP_LIMIT"
        result["final_page"] = await observe()
    except Exception as error:
        if result["status"] != "UNKNOWN_EFFECT":
            result["status"] = "TIMED_OUT" if isinstance(error, TimeoutError) else "FAILED"
        result["error"] = {"type": type(error).__name__, "message": str(error)[:500]}
    finally:
        journal.finish(task_id, result["status"])
    return result
