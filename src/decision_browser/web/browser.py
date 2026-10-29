"""DOM actions and settling adapted from the pinned MIT upstream in upstream/."""

import base64
import hashlib
import ipaddress
import json
import sys
import time
from collections import Counter
from pathlib import Path
from urllib.parse import urlsplit

import websocket

from .tasks import allowed_action, validate_url

READ_STATE = (Path(__file__).with_name("upstream") / "snapshot.js").read_text(encoding='utf-8')
MARKER = f"(() => {{ const state={READ_STATE}; return state?.marker ?? null; }})()"
METADATA = """(() => {
  const result={};
  for (const [node,e] of window.__jevFast?.nodes || []) {
    if (!e.isConnected) continue;
    const form=e.form || e.closest('form');
    result[String(node)]={tag:e.tagName.toLowerCase(), input_type:e.type || '',
      attributes:Object.fromEntries(['id','name','role','aria-label','data-place']
        .map(key=>[key,e.getAttribute(key) || ''])),
      href:e.closest('a[href]')?.href || '',
      form_action:(e.hasAttribute('formaction') ? e.formAction : form?.action) || '',
      form_method:(e.hasAttribute('formmethod') ? e.formMethod : form?.method) || '',
      form_id:form?.id || '',
      role:e.getAttribute('role') || '', name:e.name || '',
      label:e.getAttribute('aria-label') || e.innerText || e.placeholder || '',
      context:(e.closest('form,dialog,[role="dialog"],article,li,tr,[role="row"]') ||
        e.parentElement)?.innerText?.slice(0,6000) || ''};
  }
  return {url:location.href, metadata:result};
})()"""


class StalePage(ValueError):
    """A decision no longer refers to the observed page."""


class NotDispatched(StalePage):
    """The observed action became stale before any input could be sent."""


class Browser:
    def __init__(self, task, endpoint, *, enforce_policy=True):
        parsed = urlsplit(endpoint)
        try:
            loopback = ipaddress.ip_address(parsed.hostname or "").is_loopback
        except ValueError:
            loopback = parsed.hostname == "localhost"
        if (parsed.scheme not in {"ws", "wss"} or not loopback or not parsed.port
                or parsed.username or parsed.password or parsed.fragment):
            raise ValueError("An explicit loopback CDP websocket endpoint is required")
        validate_url(task, task.url)
        self.task = task
        self.enforce_policy = enforce_policy
        self.target = self.session = self.socket = None
        self.cdp_counts = Counter()
        self._sequence = 0
        self.after_input = None
        try:
            self.socket = websocket.create_connection(endpoint, timeout=15,
                suppress_origin=True, http_no_proxy=[parsed.hostname])
            self.browser_version = self._request("Browser.getVersion").get("product", "")
            self.target = self._request("Target.createTarget", url="about:blank", background=True)["targetId"]
            self.session = self._request("Target.attachToTarget", targetId=self.target, flatten=True)["sessionId"]
            self.call("Target.activateTarget", targetId=self.target)
            self.call("Emulation.setDeviceMetricsOverride", width=1120, height=780,
                      deviceScaleFactor=1, mobile=False)
            self.call("Emulation.setFocusEmulationEnabled", enabled=True)
            self.call("Page.navigate", url=task.url)
            deadline = time.monotonic() + 15
            while True:
                try:
                    state = self.evaluate("({ready:document.readyState,url:location.href})")
                except StalePage:
                    state = None
                if state and state["ready"] == "complete" and state["url"] != "about:blank":
                    validate_url(task, state["url"])
                    break
                if time.monotonic() >= deadline:
                    raise RuntimeError("Benchmark page did not become ready")
                time.sleep(0.02)
            self.observe()
        except BaseException:
            self.close()
            raise

    def _request(self, method, **params):
        if self.socket is None:
            raise RuntimeError("Browser is closed")
        self._sequence += 1
        request = {"id": self._sequence, "method": method, "params": params}
        if self.session is not None and not method.startswith(("Target.", "Browser.")):
            request["sessionId"] = self.session
        self.cdp_counts[method] += 1
        try:
            self.socket.send(json.dumps(request))
            deadline = time.monotonic() + 15
            while True:
                if time.monotonic() >= deadline:
                    raise RuntimeError("CDP response deadline exceeded")
                response = json.loads(self.socket.recv())
                if response.get("id") == request["id"]:
                    break
        except (websocket.WebSocketException, OSError, ValueError) as exc:
            raise RuntimeError(f"CDP {method} response unavailable; do not replay input") from exc
        if "error" in response:
            raise RuntimeError(f"CDP {method} failed")
        return response.get("result", {})

    def call(self, method, **params):
        return self._request(method, **params)

    def evaluate(self, expression):
        response = self.call("Runtime.evaluate", expression=expression, returnByValue=True)
        if response.get("exceptionDetails"):
            raise StalePage("Document changed during evaluation")
        return response.get("result", {}).get("value")

    def observe(self, screenshot=False):
        if getattr(self, "after_input", None):
            action, self.after_input = self.after_input, None
            try:
                self.call(
                    "Runtime.evaluate",
                    expression="""(action => new Promise(resolve => {
                      const field=window.__jevFast?.nodes.get(action.node);
                      const autocomplete=action.kind==='fill' && field?.getAttribute('role')==='combobox';
                      let frames=0, stopped=false;
                      const finish=()=>{stopped=true;resolve()};
                      setTimeout(finish,autocomplete ? 2000 : 50);
                      const ready=()=>{
                        if (stopped) return;
                        const ids=(field?.getAttribute('aria-controls')||field?.getAttribute('aria-owns')||'')
                          .split(/\\s+/).filter(Boolean);
                        const roots=ids.length ? ids.map(id=>document.getElementById(id)).filter(Boolean) : [document];
                        const options=roots.flatMap(root=>[...root.querySelectorAll('[role="option"]')]);
                        const normalized=s=>String(s).normalize('NFD').replace(/[\u0300-\u036f]/g,'').toLowerCase().trim();
                        if (++frames>=2 && (!autocomplete || options.some(e=>{
                          const r=e.getBoundingClientRect();
                          return normalized(e.textContent).includes(normalized(field.value)) &&
                            r.width && r.height && r.bottom>0 && r.top<innerHeight &&
                            e.checkVisibility({checkOpacity:true,checkVisibilityCSS:true});
                        }))) finish();
                        else requestAnimationFrame(ready);
                      };
                      requestAnimationFrame(ready);
                    }))(""" + json.dumps(action) + ")",
                    awaitPromise=True,
                    returnByValue=True,
                )
            except RuntimeError:
                pass
        for attempt in range(10):
            try:
                info = self._operation({"operation": "observe", "screenshot": screenshot})
                return info
            except StalePage:
                if attempt == 9:
                    raise
                time.sleep(0.02)
        raise StalePage("Page did not settle")

    def fresh(self, page, action=None):
        if action is not None and action["kind"] in {"click", "select"}:
            node = action["node"]
            if type(node) is not int:
                return False
            current = self.evaluate(
                "(() => { const c=window.__jevFast; "
                f"return c ? [c.pageKey(),c.guard(c.nodes.get({node}))] : null; }})()"
            )
            return current == [page["page_key"], page["guards"].get(str(node))]
        return self.evaluate(MARKER) == page["marker"]

    def act(self, action, page, text=None):
        if action not in page["actions"]:
            raise ValueError("Action was not observed")
        try:
            if not self.fresh(page, action):
                raise StalePage("Page changed since this decision. Observe again.")
            current = self.evaluate(METADATA)
        except StalePage as error:
            raise NotDispatched(str(error)) from error
        if not isinstance(current, dict):
            raise NotDispatched("Document changed before policy check")
        validate_url(self.task, current["url"])
        metadata = current["metadata"]
        if not allowed_action(self.task, action, metadata, current["url"]):
            raise ValueError("Control is outside benchmark policy")
        if action["kind"] == "fill" and (not isinstance(text, str) or len(text) > 2000):
            raise ValueError("Field text must be a string of at most 2000 characters")
        if action["kind"] == "wait":
            time.sleep(0.1)
        result = self._operation({"operation": "act", "action": action, "text": text})
        self.after_input = action if action["kind"] != "wait" else None
        return result

    def screenshot(self):
        return base64.b64decode(self.call("Page.captureScreenshot", format="png")["data"], validate=True)

    def close(self):
        try:
            if self.target is not None and self.socket is not None:
                target, self.target = self.target, None
                self._request("Target.closeTarget", targetId=target)
        except RuntimeError:
            pass
        finally:
            if self.socket is not None:
                socket, self.socket = self.socket, None
                try:
                    socket.close()
                except (websocket.WebSocketException, OSError):
                    pass

    def _operation(self, request):
        operation = request["operation"]

        def call(method, **params):
            return self.call(method, **params)

        def evaluate(expression):
            result = call("Runtime.evaluate", expression=expression, returnByValue=True)
            if result.get("exceptionDetails"):
                if operation == "act" and request["action"]["kind"] == "select":
                    raise RuntimeError("Dropdown execution was interrupted; inspect before retrying.")
                if operation == "act":
                    raise NotDispatched("Document changed before input")
                raise StalePage("Document changed during evaluation")
            return result.get("result", {}).get("value")

        if operation == "act":
            action = request["action"]
            kind = action["kind"]
            if kind == "scroll":
                call("Input.dispatchMouseEvent", type="mouseWheel", x=550, y=650, deltaX=0, deltaY=action["delta"])
            elif kind != "wait":
                if type(action["node"]) is not int:
                    raise ValueError("Invalid observed node")
                target = evaluate("""(action => {
                  const e=window.__jevFast?.nodes.get(action.node);
                  if (!e?.isConnected || e.matches(':disabled') || e.closest('[aria-disabled="true"],[inert]') ||
                      !e.checkVisibility({checkOpacity:true,checkVisibilityCSS:true})) return null;
                  if (action.kind==='fill' && (e.readOnly || e.getAttribute('aria-readonly')==='true')) return null;
                  const r=e.getBoundingClientRect(), x=r.x+r.width/2, y=r.y+r.height/2;
                  if (!r.width || !r.height || x<0 || y<0 || x>=innerWidth || y>=innerHeight) return null;
                  if (!e.contains(document.elementFromPoint(x,y))) return null;
                  if (action.kind==='select') {
                    if (e.tagName!=='SELECT' || ![...e.options].some(o=>o.value===action.value &&
                        !o.disabled && !o.closest('optgroup[disabled]'))) return null;
                    e.value=action.value;
                    e.dispatchEvent(new Event('input',{bubbles:true}));
                    e.dispatchEvent(new Event('change',{bubbles:true}));
                  }
                  return {x,y};
                })(""" + json.dumps(action) + ")")
                if target is None:
                    if kind == "select":
                        raise RuntimeError("Dropdown execution was not confirmed; inspect before retrying.")
                    raise NotDispatched("Target changed or is covered. Observe again.")
                if kind != "select":
                    x, y = target["x"], target["y"]
                    for event in ("mousePressed", "mouseReleased"):
                        call("Input.dispatchMouseEvent", type=event, x=x, y=y, button="left", clickCount=1)
                    if kind == "fill":
                        call(
                            "Input.dispatchKeyEvent",
                            type="keyDown",
                            key="a",
                            code="KeyA",
                            modifiers=4 if sys.platform == "darwin" else 2,
                            commands=["selectAll"],
                        )
                        call(
                            "Input.dispatchKeyEvent",
                            type="keyUp",
                            key="a",
                            code="KeyA",
                            modifiers=4 if sys.platform == "darwin" else 2,
                        )
                        call("Input.insertText", text=request["text"])
            return {"executed": action["id"]}

        info = evaluate("(() => { const state=" + READ_STATE +
                        "; if (!state) return null; const scope=" + METADATA +
                        "; return {...state, metadata:scope.metadata}; })()")
        if info is None:
            raise StalePage("Document is navigating")
        validate_url(self.task, info["url"])
        info["fingerprint"] = fingerprint(info)
        if request.get("screenshot", False):
            info["screenshot"] = base64.b64encode(self.screenshot()).decode("ascii")
        return info


def fingerprint(state):
    content = {k: state[k] for k in ("url", "text", "actions", "scroll")}
    return hashlib.sha256(json.dumps(content, sort_keys=True).encode()).hexdigest()


