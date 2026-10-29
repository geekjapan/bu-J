"""Exercise the public generic browser adapter against an owned Chrome fixture."""
from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import json
from pathlib import Path
import subprocess
from uuid import uuid4

from decision_browser.general.browser import GeneralBrowser
from decision_browser.general.files import collect_downloads, stage_uploads
from decision_browser.general.journal import GeneralJournal
from decision_browser.general.models import ACTION, Task
from decision_browser.web.environment import ManagedChrome, validate_manifest


def state(container):
    script = "import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:8766/state').read().decode())"
    return json.loads(subprocess.run(["docker", "exec", container, "python", "-c", script],
                                    capture_output=True, text=True, check=True, timeout=10).stdout)


async def drive(manifest, output):
    upload = output / "specification.txt"
    upload.write_text("Northstar verification specification\n")
    task = Task(url="http://127.0.0.1:8766/", goal="Exercise the procurement sandbox",
                allowed_origins=["http://127.0.0.1:8766", "http://localhost:8766"],
                uploads={"specification": str(upload)})
    uploads, receipts = stage_uploads(manifest["container"], task.uploads)
    browser = GeneralBrowser(manifest["endpoint"], task, uploads)
    journal = GeneralJournal(output / "journal.sqlite3")
    task_id = str(uuid4())
    journal.begin(task_id, task.model_dump())
    before = state(manifest["container"])
    steps = []

    async def act(kind, *, testid=None, **args):
        page = await browser.observe()
        if testid:
            matches = [int(i) for i, n in page["nodes"].items() if n["attrs"].get("data-testid") == testid]
            assert len(matches) == 1, (testid, page["dom"])
            args["index"] = matches[0]
        action = ACTION.validate_python({"kind": kind, **args})
        step = len(steps)
        journal.prepare(task_id, step, action.model_dump(), page)
        journal.transition(task_id, step, "DISPATCHED")
        try:
            result = await browser.execute(action, page)
            journal.transition(task_id, step, "ACKNOWLEDGED", result)
        except BaseException:
            journal.transition(task_id, step, "UNKNOWN_EFFECT")
            raise
        steps.append(action.model_dump())
        await asyncio.sleep(.2)
        return await browser.observe()

    async def seek(testid):
        for _ in range(18):
            page = await browser.observe()
            for i, node in page["nodes"].items():
                if node["attrs"].get("data-testid") == testid:
                    return int(i)
            await act("scroll", dy=600)
        raise AssertionError("Control not observed: " + testid)

    try:
        await browser.start()
        await act("navigate", url=task.url)
        await act("input", testid="requester-name", text="Mina Verification")
        await act("select", testid="department", value="Operations")
        await act("click", testid="priority")
        await act("input", testid="requester-name", text="Mina Verification")
        await act("key", key="Enter")
        assert state(manifest["container"])["counters"]["request"] == before["counters"]["request"] + 1
        await seek("same_frame-contact")
        await act("input", testid="same_frame-contact", text="Local Mina")
        await act("click", testid="same_frame-confirm")
        await seek("cross_frame-contact")
        await act("input", testid="cross_frame-contact", text="External Mina")
        await act("click", testid="cross_frame-confirm")
        await seek("packaging-note")
        await act("input", testid="packaging-note", text="Reusable packaging")
        await act("click", testid="save-packaging")
        await seek("open-quotation")
        page = await act("click", testid="open-quotation")
        detail = next(t for t in page["tabs"] if t["url"].endswith("/detail"))
        await act("switch_tab", tab_id=detail["tab_id"])
        page = await browser.observe()
        assert "Quotation status: ready for review." in page["text"]
        await act("navigate", url=task.url + "?history=1")
        page = await act("back")
        assert page["url"].endswith("/detail")
        await act("forward")
        await act("reload")
        await act("close_tab", tab_id=detail["tab_id"])
        await seek("specification-file")
        await act("upload", testid="specification-file", file="specification")
        await act("click", testid="upload-specification")
        await seek("download-quotation")
        await act("click", testid="download-quotation")
        await seek("receiving-name")
        await act("input", testid="receiving-name", text="Receiver Mina")
        await act("click", testid="confirm-receiving")
        await asyncio.sleep(.5)
        final = state(manifest["container"])
        for key in ("request", "same_frame", "cross_frame", "shadow", "upload", "download", "scroll"):
            assert final["counters"][key] == before["counters"][key] + 1, (key, final)
        assert final["last"]["request"]["name"] == "Mina Verification"
        assert final["last"]["request"]["department"] == "Operations"
        assert final["last"]["request"]["priority"] is True
        assert final["last"]["upload"]["sha256"] == receipts["specification"]["sha256"]
        downloads = collect_downloads(manifest["container"], output)
        assert downloads, "No real browser download collected"
        image = base64.b64decode(await browser.screenshot(), validate=True)
        assert image.startswith(b"\x89PNG\r\n\x1a\n")
        (output / "final.png").write_bytes(image)
        proof = {"passed": True, "steps": steps, "before": before, "after": final,
                 "downloads": downloads, "uploads": receipts,
                 "screenshot_sha256": hashlib.sha256(image).hexdigest(), "model_calls": 0}
        (output / "proof.json").write_text(json.dumps(proof, ensure_ascii=False, indent=2) + "\n")
        journal.finish(task_id, "VERIFIED")
        return proof
    finally:
        await browser.close()
        journal.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True)
    parser.add_argument("--manifest")
    args = parser.parse_args(argv)
    output = Path(args.output)
    output.mkdir(parents=True, exist_ok=False)
    if args.manifest:
        proof = asyncio.run(drive(validate_manifest(args.manifest, generic=True), output))
    else:
        with ManagedChrome(output / "chrome", generic=True) as managed:
            proof = asyncio.run(drive(managed.manifest, output))
    print(json.dumps({"passed": proof["passed"], "output": str(output)}))


if __name__ == "__main__":
    main()
