from __future__ import annotations

import asyncio
from collections import OrderedDict
import json
import os
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit
from uuid import uuid4

from decision_browser.contracts import ActionProposal, Element, ExecutionResult, Facts, Lease, Observation, TaskSpec, digest

UPSTREAM_SOURCE_URL = "https://raw.githubusercontent.com/browser-use/browser-harness/main/src/mcp_server.py"
UPSTREAM_SOURCE_SHA256 = "88be79452beb74bf1dd4294b4b50bfbf672c2cf76bd8bec738b9567d0716833e"


class HarnessError(RuntimeError):
    pass


# Names, types, defaults and required arguments reviewed against the pinned source.
_TOOL_ARGUMENTS = {
    "browser_new_tab": {"url": ("string", "about:blank")},
    "browser_goto": {"url": ("string",)},
    "browser_page_info": {},
    "browser_click": {"x": ("integer",), "y": ("integer",), "button": ("string", "left"), "clicks": ("integer", 1)},
    "browser_type": {"text": ("string",)},
    "browser_fill": {"selector": ("string",), "text": ("string",), "clear_first": ("boolean", True)},
    "browser_press": {"key": ("string",), "modifiers": ("integer", 0)},
    "browser_scroll": {"x": ("integer",), "y": ("integer",), "dy": ("integer", -300), "dx": ("integer", 0)},
    "browser_screenshot": {"path": ("string?", None), "full": ("boolean", False), "max_dim": ("integer?", None)},
    "browser_list_tabs": {}, "browser_current_tab": {},
    "browser_switch_tab": {"target": ("string",)},
    "browser_close_tab": {"target": ("string?", None)},
    "browser_ensure_real_tab": {},
    "browser_wait": {"seconds": ("number", 1.0)},
    "browser_wait_for_load": {"timeout": ("number", 15.0)},
    "browser_wait_for_element": {"selector": ("string",), "timeout": ("number", 10.0), "visible": ("boolean", False)},
    "browser_js": {"expression": ("string",), "target_id": ("string?", None)},
    "browser_cdp": {"method": ("string",), "params": ("object?", None)},
    "browser_upload_file": {"selector": ("string",), "path": ("string",)},
    "browser_http_get": {"url": ("string",), "timeout": ("number", 20.0), "headers": ("object?", None)},
    "browser_start_recording": {"name": ("string?", None), "title": ("string?", None)},
    "browser_stop_recording": {},
}


def reviewed_tools() -> dict:
    tools = []
    for name, arguments in _TOOL_ARGUMENTS.items():
        properties = {}
        for key, value in arguments.items():
            kind = value[0]
            base = {"type": kind.rstrip("?")}
            if kind.rstrip("?") == "object":
                base["additionalProperties"] = True
            prop = {"anyOf": [base, {"type": "null"}]} if kind.endswith("?") else base
            if len(value) == 2:
                prop["default"] = value[1]
            properties[key] = prop
        schema = {"type": "object", "properties": properties}
        required = [key for key, value in arguments.items() if len(value) == 1]
        if required:
            schema["required"] = required
        tools.append({"name": name, "inputSchema": schema})
    return {"tools": tools}


def _schema(value):
    if isinstance(value, dict):
        return {k: ({name: _schema(prop) for name, prop in v.items()} if k == "properties" else _schema(v))
                for k, v in value.items() if k not in ("title", "description")}
    if isinstance(value, list):
        return [_schema(v) for v in value]
    return value


def validate_tools(result: dict):
    if set(result) != {"tools"} or not isinstance(result["tools"], list):
        raise HarnessError("MCP_SCHEMA_CHANGED")
    expected = {t["name"]: t["inputSchema"] for t in reviewed_tools()["tools"]}
    actual = {}
    for tool in result["tools"]:
        if not isinstance(tool, dict) or set(tool) - {"name", "description", "inputSchema"}:
            raise HarnessError("MCP_SCHEMA_CHANGED")
        name = tool.get("name")
        if not isinstance(name, str) or name in actual:
            raise HarnessError("MCP_SCHEMA_CHANGED")
        actual[name] = _schema(tool.get("inputSchema"))
    if actual != expected:
        raise HarnessError("MCP_SCHEMA_CHANGED")


class MCPStdioTransport:
    """One persistent, serialized JSON-RPC stdio connection. No browser discovery."""

    def __init__(self, command: tuple[str, ...], *, env: dict[str, str], timeout: float = 5.0):
        if not command or timeout <= 0:
            raise ValueError("explicit command and positive timeout required")
        self.command, self.env, self.timeout = command, env, timeout
        self.process = None
        self._lock = asyncio.Lock()
        self._request_id = 0
        self.ready = False

    async def start(self):
        if self.process is not None:
            raise HarnessError("MCP_ALREADY_STARTED")
        self.process = await asyncio.create_subprocess_exec(*self.command, env=self.env, stdin=asyncio.subprocess.PIPE,
                                                            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
                                                            limit=1_000_000)
        try:
            response = await self.request("initialize", {"protocolVersion": "2024-11-05", "capabilities": {},
                                                       "clientInfo": {"name": "decision-browser", "version": "0.1.0"}})
            if response.get("protocolVersion") != "2024-11-05":
                raise HarnessError("MCP_PROTOCOL_CHANGED")
            self.process.stdin.write(b'{"jsonrpc":"2.0","method":"notifications/initialized"}\n')
            await self.process.stdin.drain()
            validate_tools(await self.request("tools/list", {}))
            self.ready = True
        except BaseException:
            await self.close()
            raise

    async def request(self, method: str, params: dict):
        async with self._lock:
            if self.process is None or self.process.returncode is not None:
                raise HarnessError("MCP_NOT_RUNNING")
            self._request_id += 1
            request_id = self._request_id
            try:
                async with asyncio.timeout(self.timeout):
                    self.process.stdin.write(json.dumps({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}).encode() + b"\n")
                    await self.process.stdin.drain()
                    message = json.loads(await self.process.stdout.readline())
                    if (not isinstance(message, dict) or message.get("jsonrpc") != "2.0" or
                            type(message.get("id")) is not int or message["id"] != request_id):
                        raise HarnessError("MCP_INVALID_RESPONSE")
                    if "error" in message:
                        raise HarnessError("MCP_RPC_ERROR")
                    if not isinstance(message.get("result"), dict):
                        raise HarnessError("MCP_INVALID_RESULT")
                    return message["result"]
            except BaseException:
                self.ready = False
                await self.close()
                raise

    async def call(self, name: str, arguments: dict):
        if not self.ready or name not in _TOOL_ARGUMENTS:
            raise HarnessError("MCP_TOOL_NOT_APPROVED")
        result = await self.request("tools/call", {"name": name, "arguments": arguments})
        content = result.get("content")
        if result.get("isError", False):
            raise HarnessError("MCP_TOOL_ERROR")
        if (not isinstance(content, list) or len(content) != 1 or not isinstance(content[0], dict) or
                content[0].get("type") != "text"):
            raise HarnessError("MCP_INVALID_TOOL_RESULT")
        try:
            value = json.loads(content[0]["text"])
        except (ValueError, TypeError, KeyError) as exc:
            raise HarnessError("MCP_INVALID_JSON_TEXT") from exc
        if isinstance(value, dict) and ("error" in value or value.get("ok") is False):
            raise HarnessError("MCP_OPERATION_FAILED")
        return value

    async def close(self):
        self.ready = False
        if self.process is not None and self.process.returncode is None:
            self.process.terminate()
            try:
                await asyncio.wait_for(self.process.wait(), 2)
            except TimeoutError:
                self.process.kill()
                await self.process.wait()


@dataclass(frozen=True)
class HarnessSettings:
    endpoint: str
    profile: Path
    runtime_dir: Path
    daemon_name: str
    fixture_origin: str
    tab_id: str

    def __post_init__(self):
        endpoint, origin = urlsplit(self.endpoint), urlsplit(self.fixture_origin)
        if (endpoint.scheme not in ("ws", "wss") or endpoint.hostname not in ("127.0.0.1", "localhost", "::1") or
                endpoint.username or endpoint.password or endpoint.query or endpoint.fragment or
                not self.profile.is_absolute() or not self.runtime_dir.is_absolute() or not self.daemon_name or
                self.daemon_name == "default" or not self.tab_id or origin.hostname not in ("127.0.0.1", "localhost", "::1") or
                origin.scheme != "http" or origin.username or origin.password or origin.path or origin.query or origin.fragment):
            raise ValueError("explicit dedicated loopback endpoint, profile, runtime, daemon, origin and tab required")

    def environment(self) -> dict[str, str]:
        return {"PATH": os.defpath, "BU_CDP_WS": self.endpoint, "BU_NAME": self.daemon_name,
                "BH_RUNTIME_DIR": str(self.runtime_dir), "BH_UPDATE_CHECK": "0", "BH_OPEN_LIVE_URL": "0"}


EXTRACT_FIXTURE = """(() => {
 const root = document.querySelector('[data-fixture="search-results-v1"]');
 if (!root) return {supported:false};
 return {supported:true, url:location.href, epoch:performance.timeOrigin,
  page_kind:root.dataset.kind, facts:JSON.parse(root.querySelector('#fixture-facts').textContent),
  elements:Array.from(root.querySelectorAll('[data-action]')).map(e => ({
   element_ref:e.dataset.action,role:e.tagName==='INPUT'?'textbox':'button',
   name:e.getAttribute('aria-label') || e.textContent,enabled:!e.disabled,frame_ref:'main'}))};
})()"""
SEARCH_FIXTURE = "document.querySelector('#fixture-search').requestSubmit()"
NEXT_FIXTURE = "document.querySelector('[data-action=\"next-page\"]').click()"


class HarnessMCPDriver:
    def __init__(self, settings: HarnessSettings, transport: MCPStdioTransport, *, session_id: str):
        self.settings, self.transport, self.session_id = settings, transport, session_id
        self._last = None
        self._observations = OrderedDict()
        self._epoch = 0
        self._document = None
        self._fencing_token = 0

    async def start(self):
        # Upstream MCP calls ensure_daemon, not require_existing_daemon. No attested sandbox is available yet.
        raise HarnessError("LIVE_ISOLATION_AND_EGRESS_NOT_VERIFIED")

    def _lease(self, lease: Lease):
        if (lease.session_id != self.session_id or lease.tab_id != self.settings.tab_id or
                lease.expires_monotonic <= time.monotonic() or lease.fencing_token < self._fencing_token):
            raise HarnessError("BROWSER_BINDING_MISMATCH")
        self._fencing_token = lease.fencing_token

    async def observe(self, task: TaskSpec, lease: Lease) -> Observation:
        self._lease(lease)
        tab = await self.transport.call("browser_current_tab", {})
        if tab.get("targetId") != self.settings.tab_id:
            raise HarnessError("TAB_CHANGED")
        raw = await self.transport.call("browser_js", {"expression": EXTRACT_FIXTURE, "target_id": self.settings.tab_id})
        if not isinstance(raw, dict) or raw.get("supported") is not True:
            raise HarnessError("UNSUPPORTED_FIXTURE")
        url = urlsplit(raw["url"])
        origin = f"{url.scheme}://{url.netloc}"
        if origin != self.settings.fixture_origin or origin not in task.allowed_origins:
            raise HarnessError("ORIGIN_CHANGED")
        if self._document != raw["epoch"]:
            self._epoch += 1
            self._document = raw["epoch"]
        facts = Facts.model_validate_json(json.dumps(raw["facts"]))
        elements = tuple(Element.model_validate(e) for e in raw["elements"])
        semantic = {"facts": facts.model_dump(mode="json"), "elements": [e.model_dump(mode="json") for e in elements], "kind": raw["page_kind"]}
        self._last = Observation(task_id=task.task_id, session_id=self.session_id, tab_id=self.settings.tab_id,
                                 navigation_epoch=self._epoch, observation_id=uuid4().hex, captured_at=datetime.now(timezone.utc),
                                 captured_monotonic=time.monotonic(), origin=origin, page_kind=raw["page_kind"],
                                 phase="collect" if facts.searched else "search", semantic_digest=digest(semantic),
                                 transaction_digest=digest({"query": facts.query, "page": facts.page_index}), facts=facts, elements=elements)
        self._observations[self._last.observation_id] = (self._last, task, lease.fencing_token)
        if len(self._observations) > 32:
            self._observations.popitem(last=False)
        return self._last

    async def execute(self, action: ActionProposal, lease: Lease) -> ExecutionResult:
        self._lease(lease)
        bound = self._observations.get(action.observation_id)
        if bound is None or bound[2] != lease.fencing_token:
            raise HarnessError("UNBOUND_ACTION")
        before, task, _ = bound
        from decision_browser.observer import require_fresh
        require_fresh(before, await self.observe(task, lease), action.element_ref)
        if action.kind == "EXTRACT":
            return ExecutionResult(action_id=action.action_id, status="OK", records=self._last.facts.records)
        if action.kind == "OBSERVE":
            return ExecutionResult(action_id=action.action_id, status="OK")
        try:
            if action.kind == "FILL_SEARCH" and action.parameters.query == task.query:
                await self.transport.call("browser_fill", {"selector": "#query", "text": task.query, "clear_first": True})
                await self.transport.call("browser_js", {"expression": SEARCH_FIXTURE, "target_id": self.settings.tab_id})
            elif action.kind == "CLICK_NEXT" and before.facts.has_next:
                await self.transport.call("browser_js", {"expression": NEXT_FIXTURE, "target_id": self.settings.tab_id})
            elif action.kind == "WAIT" and action.parameters.wait_ms is not None:
                await asyncio.sleep(action.parameters.wait_ms / 1000)
            else:
                raise HarnessError("UNAPPROVED_FIXTURE_ACTION")
        except HarnessError as exc:
            if str(exc) == "UNAPPROVED_FIXTURE_ACTION":
                raise
            return ExecutionResult(action_id=action.action_id, status="UNKNOWN", reason="MCP_EFFECT_UNCONFIRMED")
        except (TimeoutError, OSError, ValueError):
            return ExecutionResult(action_id=action.action_id, status="UNKNOWN", reason="MCP_EFFECT_UNCONFIRMED")
        return ExecutionResult(action_id=action.action_id, status="OK")

    async def close(self):
        await self.transport.close()
