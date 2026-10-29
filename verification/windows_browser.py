"""Verify the native Windows Chrome adapter, separately from Docker acceptance."""
from __future__ import annotations

import argparse
import asyncio
import base64
from contextlib import ExitStack
import hashlib
from http.server import ThreadingHTTPServer
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from urllib.request import ProxyHandler as URLProxyHandler, build_opener
from uuid import uuid4

from decision_browser.general.browser import GeneralBrowser
from decision_browser.general.journal import GeneralJournal
from decision_browser.general.models import ACTION, Task
from verification.generic_browser.fixture_server import make_server
from verification.generic_browser.proxy import ProxyHandler


class FixtureProxy(ProxyHandler):
    def do_CONNECT(self):
        self.send_error(403, "Native verification permits only the local HTTP fixture")


def chrome_executable():
    if platform.system() != "Windows":
        raise RuntimeError("This acceptance harness requires native Windows Python")
    for variable in ("PROGRAMFILES", "PROGRAMFILES(X86)"):
        root = os.environ.get(variable)
        if root:
            candidate = Path(root) / "Google/Chrome/Application/chrome.exe"
            if candidate.is_file():
                return str(candidate)
    executable = shutil.which("chrome.exe") or shutil.which("chrome")
    if not executable:
        raise RuntimeError("Google Chrome is required in Program Files or PATH")
    return executable


def chrome_arguments(executable, profile):
    return [executable, f"--user-data-dir={profile}", "--remote-debugging-address=127.0.0.1",
            "--remote-debugging-port=0", "--headless=new", "--no-first-run", "--no-default-browser-check",
            "--disable-background-networking", "--disable-component-update",
            "--proxy-server=http://127.0.0.1:8888", "--proxy-bypass-list=<-loopback>",
            "--window-size=1280,1000", "about:blank"]


def read_json(url):
    with build_opener(URLProxyHandler({})).open(url, timeout=5) as response:
        return json.load(response)


def wait_endpoint(profile, timeout=60):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            lines = (profile / "DevToolsActivePort").read_text(encoding="utf-8").splitlines()
            port = int(lines[0])
            if not 0 < port < 65536 or not lines[1].startswith("/devtools/browser/"):
                raise ValueError("Invalid owned Chrome debug port")
            endpoint = f"ws://127.0.0.1:{port}{lines[1]}"
            version = read_json(f"http://127.0.0.1:{port}/json/version")
            if version["webSocketDebuggerUrl"] != endpoint:
                raise ValueError("Owned Chrome endpoint mismatch")
            return endpoint, version
        except (OSError, ValueError, IndexError, KeyError):
            time.sleep(.1)
    raise TimeoutError("Owned Chrome did not publish its loopback CDP endpoint")


def profile_processes(profile):
    import psutil

    argument = f"--user-data-dir={profile}"
    return [process for process in psutil.process_iter(["cmdline"], ad_value=None)
            if argument in (process.info["cmdline"] or [])]


def cleanup_chrome(process, profile):
    import psutil

    owned = {p.pid: p for p in profile_processes(profile)}
    if process.poll() is None:
        owned[process.pid] = psutil.Process(process.pid)
    tracked = dict(owned)
    for root in owned.values():
        try:
            tracked.update({p.pid: p for p in root.children(recursive=True)})
        except psutil.NoSuchProcess:
            pass
    for root in owned.values():
        if root.is_running():
            subprocess.run(["taskkill", "/PID", str(root.pid), "/T", "/F"],
                           capture_output=True, timeout=10, check=False)
    _, alive = psutil.wait_procs(list(tracked.values()), timeout=5)
    remaining = profile_processes(profile)
    if alive or remaining:
        raise RuntimeError("Owned Chrome process tree is still present; profile retained")
    shutil.rmtree(profile)
    return {"processes_absent": True, "profile_absent": not profile.exists(),
            "tracked_pids": sorted(tracked)}


async def cdp_command(client, domain, method, params=None, session_id=None):
    command = getattr(getattr(client.send, domain), method)
    return await asyncio.wait_for(command(params=params or {}, session_id=session_id), 15)


async def tab_state(client, session_id):
    result = await cdp_command(client, "Runtime", "evaluate", {
        "expression": """({url:location.href, value:document.querySelector('input')?.value,
            session:sessionStorage.getItem('windows-verification'),
            width:innerWidth, height:innerHeight})""", "returnByValue": True,
    }, session_id)
    assert not result.get("exceptionDetails"), result
    return result["result"]["value"]


async def seed_existing_tabs(client, url):
    tabs = {}
    for label, path in (("selected", "?borrowed=1"), ("unrelated", "detail?unrelated=1")):
        target = await cdp_command(client, "Target", "createTarget", {"url": url + path})
        attached = await cdp_command(client, "Target", "attachToTarget", {
            "targetId": target["targetId"], "flatten": True})
        session_id = attached["sessionId"]
        deadline = time.monotonic() + 15
        while True:
            ready = await cdp_command(client, "Runtime", "evaluate", {
                "expression": "document.readyState === 'complete' && location.href !== 'about:blank'",
                "returnByValue": True}, session_id)
            if ready["result"].get("value"):
                break
            if time.monotonic() >= deadline:
                raise TimeoutError("Existing fixture tab did not load")
            await asyncio.sleep(.1)
        result = await cdp_command(client, "Runtime", "evaluate", {
            "expression": """(() => {
                const input = document.querySelector('input') || document.body.appendChild(document.createElement('input'));
                input.value = %s;
                sessionStorage.setItem('windows-verification', %s);
            })()""" % (json.dumps(label + " form"), json.dumps(label + " session")),
        }, session_id)
        assert not result.get("exceptionDetails"), result
        tabs[label] = {"id": target["targetId"], "session_id": session_id,
                       "before": await tab_state(client, session_id)}
    return tabs


async def assert_tabs_preserved(client, tabs):
    for tab in tabs.values():
        tab["after"] = await tab_state(client, tab["session_id"])
        assert tab["after"] == tab["before"], tab
    targets = await cdp_command(client, "Target", "getTargets")
    ids = {target["targetId"] for target in targets["targetInfos"]}
    assert all(tab["id"] in ids for tab in tabs.values()), targets


async def cli_inventory(endpoint, tabs, output):
    command = [sys.executable, "-m", "decision_browser.general.cli", "--list-tabs", "--cdp-url", endpoint]
    result = await asyncio.to_thread(subprocess.run, command, capture_output=True, text=True,
                                     encoding="utf-8", timeout=15)
    receipt = {"argv": command, "returncode": result.returncode,
               "stdout": result.stdout, "stderr": result.stderr}
    (output / "list-tabs.json").write_text(json.dumps(receipt, indent=2) + "\n", encoding="utf-8")
    assert result.returncode == 0, receipt
    inventory = json.loads(result.stdout)
    by_id = {tab["id"]: tab for tab in inventory}
    for tab in tabs.values():
        assert by_id[tab["id"]]["url"] == tab["before"]["url"], inventory
    return inventory


async def drive(endpoint, output):
    from cdp_use import CDPClient

    inspector = CDPClient(endpoint)
    task = Task(url="http://127.0.0.1:8766/", goal="Verify native Chrome transport",
                allowed_origins=["http://127.0.0.1:8766", "http://localhost:8766"])
    browser = GeneralBrowser(endpoint, task, {})
    journal = GeneralJournal(output / "journal.sqlite3")
    task_id, steps = str(uuid4()), []
    journal.begin(task_id, task.model_dump())
    before = read_json(task.url + "state")

    async def observe():
        return await asyncio.wait_for(browser.observe(), 15)

    async def act(kind, testid=None, **arguments):
        page = await observe()
        if testid:
            for _ in range(15):
                indices = [int(i) for i, n in page["nodes"].items()
                           if n["attrs"].get("data-testid") == testid]
                if indices:
                    assert len(indices) == 1, (testid, indices)
                    arguments["index"] = indices[0]
                    break
                await act("scroll", dy=550)
                page = await observe()
            else:
                raise AssertionError("Control not observed: " + testid)
        action = ACTION.validate_python({"kind": kind, **arguments})
        step = len(steps)
        journal.prepare(task_id, step, action.model_dump(), page)
        journal.transition(task_id, step, "DISPATCHED")
        try:
            result = await asyncio.wait_for(browser.execute(action, page), 15)
        except BaseException:
            journal.transition(task_id, step, "UNKNOWN_EFFECT")
            raise
        journal.transition(task_id, step, "ACKNOWLEDGED", result)
        steps.append(action.model_dump())
        await asyncio.sleep(.2)
        return await observe()

    try:
        await asyncio.wait_for(inspector.start(), 15)
        existing_tabs = await seed_existing_tabs(inspector, task.url)
        inventory = await cli_inventory(endpoint, existing_tabs, output)
        borrowed = GeneralBrowser(endpoint, task, {}, tab_id=existing_tabs["selected"]["id"])
        try:
            await asyncio.wait_for(borrowed.start(), 60)
            page = await asyncio.wait_for(borrowed.observe(), 15)
            assert page["url"] == existing_tabs["selected"]["before"]["url"]
            assert {tab["tab_id"] for tab in page["tabs"]} == {existing_tabs["selected"]["id"]}
            await assert_tabs_preserved(inspector, existing_tabs)
        finally:
            await asyncio.wait_for(borrowed.close(), 5)
        await assert_tabs_preserved(inspector, existing_tabs)
        (output / "existing-tabs.json").write_text(json.dumps({
            "borrowed_startup_preserved": True, "browser_alive_after_borrowed_close": True,
            "tabs": existing_tabs}, indent=2) + "\n", encoding="utf-8")
        await asyncio.wait_for(browser.start(), 60)
        initial = await observe()
        assert initial["target_id"] is None and initial["tabs"] == [] and initial["nodes"] == {}
        await assert_tabs_preserved(inspector, existing_tabs)
        await act("navigate", url=task.url)
        await act("input", "requester-name", text="Windows Verification")
        await act("select", "department", value="Operations")
        await act("click", "priority")
        await act("key", "requester-name", key="Enter")
        cdp = await browser.session.get_or_create_cdp_session(focus=False)
        before_scroll = (await browser._send(cdp, "Page", "getLayoutMetrics"))["cssLayoutViewport"]["pageY"]
        await act("scroll", dy=550)
        after_scroll = (await browser._send(cdp, "Page", "getLayoutMetrics"))["cssLayoutViewport"]["pageY"]
        assert after_scroll > before_scroll, (before_scroll, after_scroll)
        for frame in ("same_frame", "cross_frame"):
            await act("input", frame + "-contact", text=frame + " Windows")
            await act("click", frame + "-confirm")
        await act("input", "packaging-note", text="Windows packaging")
        await act("click", "save-packaging")
        page = await act("click", "open-quotation")
        detail = next(t for t in page["tabs"] if t["url"].endswith("/detail"))
        page = await act("switch_tab", tab_id=detail["tab_id"])
        assert "Quotation status: ready for review." in page["text"]
        await act("navigate", url=task.url + "?history=1")
        page = await act("back")
        assert page["url"].endswith("/detail")
        page = await act("reload")
        assert page["url"].endswith("/detail")
        await act("close_tab", tab_id=detail["tab_id"])
        after = read_json(task.url + "state")
        for key in ("request", "same_frame", "cross_frame", "shadow"):
            assert after["counters"][key] == before["counters"][key] + 1, after
        assert after["last"]["request"] == {
            "name": "Windows Verification", "department": "Operations", "priority": True}
        for frame in ("same_frame", "cross_frame"):
            assert after["last"][frame]["name"] == frame + " Windows"
        assert after["last"]["shadow"]["name"] == "Windows packaging"
        for key in ("upload", "download"):
            assert after["counters"][key] == 0
        assert any(step["kind"] == "scroll" for step in steps), "Native scroll was not exercised"
        image = base64.b64decode(await asyncio.wait_for(browser.screenshot(), 15), validate=True)
        assert image.startswith(b"\x89PNG\r\n\x1a\n")
        (output / "final.png").write_bytes(image)
        await asyncio.wait_for(browser.close(), 5)
        await assert_tabs_preserved(inspector, existing_tabs)
        journal.finish(task_id, "VERIFIED")
        return {"steps": steps, "before": before, "after": after,
                "existing_tabs": existing_tabs, "cli_inventory": inventory,
                "browser_alive_after_adapter_close": True,
                "scroll": {"before": before_scroll, "after": after_scroll},
                "screenshot_sha256": hashlib.sha256(image).hexdigest()}
    finally:
        try:
            await asyncio.wait_for(browser.close(), 5)
        finally:
            journal.close()
            await asyncio.wait_for(inspector.stop(), 5)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args(argv)
    executable = chrome_executable()
    import psutil
    psutil.Process(os.getpid())

    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    proof = {"passed": False, "os": platform.platform(), "python": platform.python_version(),
             "scope": "native Windows existing-browser adapter using a test-only owned profile; "
                      "personal-profile discovery and file transfer are separate; Docker is not required",
             "model_calls": 0, "executable": executable}
    profile = Path(tempfile.mkdtemp(prefix="bu-j-windows-chrome-"))
    process = None
    try:
        with ExitStack() as stack:
            fixture = stack.enter_context(make_server(Path(__file__).resolve().parents[1] / "fixtures/generic_browser"))
            proxy = stack.enter_context(ThreadingHTTPServer(("127.0.0.1", 8888), FixtureProxy))
            for server in (fixture, proxy):
                thread = threading.Thread(target=server.serve_forever, daemon=True)
                thread.start()
                stack.callback(thread.join, 5)
                stack.callback(server.shutdown)
            with (output / "chrome.log").open("wb") as log:
                process = subprocess.Popen(chrome_arguments(executable, profile), stdout=log, stderr=log)
                proof.update(pid=process.pid, profile=str(profile))
                endpoint, version = wait_endpoint(profile)
                proof["chrome_version"] = version
                proof.update(asyncio.run(drive(endpoint, output)))
    except BaseException as exc:
        proof["error"] = f"{type(exc).__name__}: {exc}"
        raise
    finally:
        try:
            if process is not None:
                proof["cleanup"] = cleanup_chrome(process, profile)
            else:
                shutil.rmtree(profile)
                proof["cleanup"] = {"processes_absent": True, "profile_absent": not profile.exists()}
            proof["passed"] = "error" not in proof
        except BaseException as exc:
            proof["cleanup_error"] = f"{type(exc).__name__}: {exc}"
            raise
        finally:
            (output / "proof.json").write_text(json.dumps(proof, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"passed": proof["passed"], "output": str(output)}))


if __name__ == "__main__":
    main()
