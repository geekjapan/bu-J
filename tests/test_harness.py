import importlib.util
import json
import sys
import threading
import time
from pathlib import Path
from urllib.request import urlopen

import pytest

from decision_browser.contracts import ActionProposal, Lease, Parameters, TaskSpec
from decision_browser.drivers.harness_mcp import (
    EXTRACT_FIXTURE, HarnessError, HarnessMCPDriver, HarnessSettings,
    MCPStdioTransport, reviewed_tools, validate_tools,
)


@pytest.fixture
def settings(tmp_path):
    return HarnessSettings(endpoint="ws://127.0.0.1:9222/devtools/browser/synthetic", profile=tmp_path / "profile",
                           runtime_dir=tmp_path / "runtime", daemon_name="fixture-only", fixture_origin="http://127.0.0.1:8765", tab_id="tab-fixture")


def test_schema_diff_rejected():
    schema_path = Path(__file__).resolve().parents[1] / "fixtures/webapp/mcp-tools-reviewed.json"
    validate_tools(json.loads(schema_path.read_text(encoding='utf-8')))
    validate_tools(reviewed_tools())
    for mutate in (
        lambda r: r["tools"].append({"name": "new_tool", "inputSchema": {}}),
        lambda r: r["tools"][0]["inputSchema"]["properties"]["url"].update(type="integer"),
        lambda r: r["tools"].pop(),
        lambda r: r["tools"].append(r["tools"][0]),
    ):
        result = reviewed_tools()
        mutate(result)
        with pytest.raises(HarnessError, match="MCP_SCHEMA_CHANGED"):
            validate_tools(result)


async def test_stdio_persistent_schema_and_error_channels(tmp_path, settings):
    script = tmp_path / "fake_mcp.py"
    script.write_text('''import json, sys
from decision_browser.drivers.harness_mcp import reviewed_tools
for line in sys.stdin:
 r=json.loads(line)
 if "id" not in r: continue
 method=r["method"]
 if method=="initialize": out={"protocolVersion":"2024-11-05"}
 elif method=="tools/list": out=reviewed_tools()
 elif r["params"]["arguments"].get("mode")=="error": out={"isError":True,"content":[{"type":"text","text":"secret must not escape"}]}
 elif r["params"]["arguments"].get("mode")=="nested": out={"content":[{"type":"text","text":json.dumps({"error":"failed"})}]}
 elif r["params"]["arguments"].get("mode")=="badjson": out={"content":[{"type":"text","text":"not JSON"}]}
 else: out={"content":[{"type":"text","text":json.dumps({"targetId":"fixture"})}]}
 print(json.dumps({"jsonrpc":"2.0","id":r["id"],"result":out}),flush=True)
''', encoding='utf-8')
    env = {**settings.environment(), "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src")}
    transport = MCPStdioTransport((sys.executable, str(script)), env=env)
    try:
        await transport.start()
        pid = transport.process.pid
        for _ in range(2):
            assert await transport.call("browser_current_tab", {}) == {"targetId": "fixture"}
            assert transport.process.pid == pid
        for mode, error in (("error", "MCP_TOOL_ERROR"), ("nested", "MCP_OPERATION_FAILED"), ("badjson", "MCP_INVALID_JSON_TEXT")):
            with pytest.raises(HarnessError, match=error):
                await transport.call("browser_current_tab", {"mode": mode})
    finally:
        await transport.close()
    assert transport.process.returncode is not None


async def test_live_activation_refused(settings):
    driver = HarnessMCPDriver(settings, None, session_id="session-fixture")
    with pytest.raises(HarnessError, match="LIVE_ISOLATION_AND_EGRESS_NOT_VERIFIED"):
        await driver.start()
    assert "HOME" not in settings.environment()
    with pytest.raises(ValueError):
        HarnessSettings(endpoint="ws://external.invalid", profile=settings.profile, runtime_dir=settings.runtime_dir,
                        daemon_name="default", fixture_origin=settings.fixture_origin, tab_id="tab")


class FakeFixtureTransport:
    def __init__(self):
        self.tab = "tab-fixture"
        self.epoch = 1
        self.calls = []
        self.facts = {"page_index": 0, "has_next": False, "searched": False, "query": "", "records": [], "total_records": 0}

    async def call(self, name, args):
        self.calls.append((name, args))
        if name == "browser_current_tab":
            return {"targetId": self.tab}
        if name == "browser_js" and args["expression"] == EXTRACT_FIXTURE:
            return {"supported": True, "url": "http://127.0.0.1:8765/", "epoch": self.epoch,
                    "page_kind": "results" if self.facts["searched"] else "search", "facts": self.facts,
                    "elements": [{"element_ref": "search", "role": "textbox", "name": "Search", "enabled": True, "frame_ref": "main"}]}
        return {"ok": True}


async def test_observation_binding_and_fixed_action(settings):
    transport = FakeFixtureTransport()
    driver = HarnessMCPDriver(settings, transport, session_id="session-fixture")
    lease = Lease(session_id="session-fixture", tab_id="tab-fixture", fencing_token=1, expires_monotonic=time.monotonic() + 10)
    task = TaskSpec(task_id="fixture", query="Synthetic", allowed_origins=(settings.fixture_origin,))
    first = await driver.observe(task, lease)
    second = await driver.observe(task, lease)
    assert first.semantic_digest == second.semantic_digest
    assert first.navigation_epoch == second.navigation_epoch
    action = ActionProposal(action_id="search", kind="FILL_SEARCH", observation_id=second.observation_id,
                            parameters=Parameters(query=task.query), element_ref="search")
    assert (await driver.execute(action, lease)).status == "OK"
    js_calls = [args["expression"] for name, args in transport.calls if name == "browser_js"]
    assert all(task.query not in expression for expression in js_calls)
    transport.epoch = 2
    assert (await driver.observe(task, lease)).navigation_epoch == first.navigation_epoch + 1
    transport.tab = "personal-tab"
    with pytest.raises(HarnessError, match="TAB_CHANGED"):
        await driver.observe(task, lease)


def test_loopback_fixture_variants_and_pagination():
    module_path = Path(__file__).resolve().parents[1] / "fixtures/webapp/server.py"
    spec = importlib.util.spec_from_file_location("fixture_server", module_path)
    fixture = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(fixture)
    server = fixture.make_server()
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        base = f"http://127.0.0.1:{server.server_port}"
        assert server.server_address[0] == "127.0.0.1"
        with urlopen(base + "/?q=Synthetic") as response:
            body = response.read().decode()
            assert "form-action 'self'" in response.headers["Content-Security-Policy"]
            assert "R001" in body and "R003" not in body and "next-page" in body
        assert "R003" in urlopen(base + "/?q=Synthetic&page=2").read().decode()
        for variant, expected in (("labels", "Find records"), ("login", 'data-kind="login"'), ("wait", 'data-kind="unsupported"'), ("injection", "attacker.invalid")):
            assert expected in urlopen(base + "/?variant=" + variant).read().decode()
        assert "<script>alert" not in fixture.render("/?q=%3Cscript%3Ealert").decode()
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


async def test_stdio_timeout_closes_connection(tmp_path, settings):
    script = tmp_path / "silent.py"
    script.write_text("import time; time.sleep(60)", encoding="utf-8")
    transport = MCPStdioTransport((sys.executable, str(script)), env=settings.environment(), timeout=0.05)
    with pytest.raises(TimeoutError):
        await transport.start()
    assert not transport.ready
    assert transport.process.returncode is not None


async def test_changed_state_and_old_lease_reject_before_action(settings):
    from decision_browser.policy import PolicyError
    transport = FakeFixtureTransport()
    driver = HarnessMCPDriver(settings, transport, session_id="session-fixture")
    lease = Lease(session_id="session-fixture", tab_id="tab-fixture", fencing_token=2, expires_monotonic=time.monotonic() + 10)
    task = TaskSpec(task_id="fixture", query="Synthetic", allowed_origins=(settings.fixture_origin,))
    obs = await driver.observe(task, lease)
    action = ActionProposal(action_id="search", kind="FILL_SEARCH", observation_id=obs.observation_id,
                            parameters=Parameters(query=task.query), element_ref="search")
    transport.facts = dict(transport.facts, query="changed")
    with pytest.raises(PolicyError, match="OBSERVATION_CHANGED"):
        await driver.execute(action, lease)
    assert not any(name == "browser_fill" for name, _ in transport.calls)
    with pytest.raises(HarnessError, match="BROWSER_BINDING_MISMATCH"):
        await driver.observe(task, lease.model_copy(update={"fencing_token": 1}))


async def test_controller_pipeline_search_extract_next(settings):
    from decision_browser.controller import Controller
    from decision_browser.executor import BrowserLane
    from decision_browser.policy import Policy
    from decision_browser.providers.mock import MockDecisionProvider
    from decision_browser.drivers.harness_mcp import SEARCH_FIXTURE, NEXT_FIXTURE

    class LoopbackPolicy(Policy):
        origins = (settings.fixture_origin,)

        def check_url(self, url):
            assert url == settings.fixture_origin

    records = [{"id": str(i), "name": f"Synthetic {i}", "updated_at": "2026-10-01"} for i in range(5)]

    class WorkflowTransport(FakeFixtureTransport):
        def update(self, page):
            self.epoch += 1
            self.facts = {"page_index": page, "has_next": page < 3, "searched": True, "query": "Synthetic",
                          "records": records[(page - 1) * 2:page * 2], "total_records": len(records)}

        async def call(self, name, args):
            if name == "browser_js" and args["expression"] == SEARCH_FIXTURE:
                self.update(1)
                return {"ok": True}
            if name == "browser_js" and args["expression"] == NEXT_FIXTURE:
                self.update(self.facts["page_index"] + 1)
                return {"ok": True}
            result = await super().call(name, args)
            if name == "browser_js" and args["expression"] == EXTRACT_FIXTURE and self.facts["searched"]:
                result["elements"] = ([{"element_ref": "next-page", "role": "button", "name": "Next", "enabled": True, "frame_ref": "main"}]
                                      if self.facts["has_next"] else [])
            return result

    transport = WorkflowTransport()
    driver = HarnessMCPDriver(settings, transport, session_id="session-fixture")
    controller = Controller(driver, MockDecisionProvider(), policy=LoopbackPolicy(),
                            lane=BrowserLane("session-fixture", settings.tab_id), mode="enforced")
    result = await controller.run(TaskSpec(task_id="pipeline", query="Synthetic", allowed_origins=(settings.fixture_origin,)))
    assert result.state == "SUCCEEDED", result
    assert [record.id for record in result.records] == [str(i) for i in range(5)]
    assert result.steps == 6
