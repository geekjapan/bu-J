from __future__ import annotations

import asyncio
import base64
import copy
import hashlib
import json
import os
from typing import Any
from urllib.parse import urljoin, urlsplit

from .models import Action, Task


class NotDispatched(ValueError):
    """Validation failed before the first browser input was sent."""


class ObservationPending(NotDispatched):
    pass


_GUARD = """function(withValue, withChecked) {
 const doc = this.ownerDocument, win = doc && doc.defaultView;
 const attrs = Object.fromEntries(Array.from(this.attributes).map(a => [a.name, a.value]));
 if (withValue) attrs.value = this.value;
 if (withChecked) {
   if (this.checked) attrs.checked = 'true';
   else delete attrs.checked;
 }
 const r = this.getBoundingClientRect();
 const x = Math.max(0, Math.min(win.innerWidth - 1, r.x + r.width / 2));
 const y = Math.max(0, Math.min(win.innerHeight - 1, r.y + r.height / 2));
 let hit = doc.elementFromPoint(x, y);
 while (hit && hit.shadowRoot) {
   const inner = hit.shadowRoot.elementFromPoint(x, y);
   if (!inner || inner === hit) break;
   hit = inner;
 }
 return {connected: this.isConnected, tag: this.tagName.toLowerCase(),
   attrs, value: this.value, checked: this.checked, selected: this.selected,
   url: doc.URL, disabled: !!this.disabled, editable: this.isContentEditable,
   x, y, visible: r.width > 0 && r.height > 0,
   hit: !!hit && (hit === this || this.contains(hit)),
   focused: doc.activeElement === this,
   form_url: this.form ? (this.getAttribute('formaction') ? this.formAction : this.form.action) : null};
}"""
_SELECT = """function(value) {
 this.value = value;
 this.dispatchEvent(new Event('input', {bubbles: true}));
 this.dispatchEvent(new Event('change', {bubbles: true}));
 return this.value;
}"""
_SELECT_CHECK = "function(value) { return Array.from(this.options).some(o => o.value === value && !o.disabled); }"
_KEY_CODES = {
    'Enter': 13, 'Tab': 9, 'Escape': 27, 'ArrowUp': 38, 'ArrowDown': 40,
    'ArrowLeft': 37, 'ArrowRight': 39, 'Home': 36, 'End': 35,
    'PageUp': 33, 'PageDown': 34, 'Backspace': 8, 'Delete': 46, 'Space': 32,
}


class GeneralBrowser:
    def __init__(self, endpoint: str, task: Task, uploads: dict[str, str], *, tab_id: str | None = None):
        parsed = urlsplit(endpoint)
        if parsed.scheme not in ('http', 'https', 'ws', 'wss') or not parsed.hostname or parsed.username or parsed.password:
            raise ValueError('Explicit existing browser CDP endpoint required')
        self.endpoint, self.task, self.uploads = endpoint, task, uploads
        self.session: Any = None
        self.observation: Any = None
        self._captures: dict[str, tuple[dict, dict, str | None]] = {}
        self.tab_id = tab_id
        self.created_tab_id: str | None = None
        self._started_navigation = False

    async def start(self):
        os.environ['ANONYMIZED_TELEMETRY'] = 'false'
        os.environ['BROWSER_USE_CLOUD_SYNC'] = 'false'
        os.environ['BROWSER_USE_LOGGING_LEVEL'] = 'error'
        os.environ['BROWSER_USE_CLOUD_HINTS'] = 'false'
        from .session import make_session

        self.session = make_session(
            cdp_url=self.endpoint, keep_alive=True, enable_default_extensions=False,
            auto_download_pdfs=False, target_id=self.tab_id, no_viewport=True,
            highlight_elements=False, dom_highlight_elements=False,
            allowed_domains=self.task.allowed_origins, cross_origin_iframes=True,
        )
        await self.session.start()

    async def close(self):
        if self.session is not None:
            await self.session.stop()

    @staticmethod
    async def _send(cdp, domain: str, method: str, params: dict | None = None):
        command = getattr(getattr(cdp.cdp_client.send, domain), method)
        return await command(params=params or {}, session_id=cdp.session_id)

    async def _initial_blank(self):
        return not self._started_navigation and await self.session.get_current_page_url() == 'about:blank'

    def _check_url(self, url, initial_blank=False):
        if not (initial_blank and url == 'about:blank'):
            if url in ('', 'about:blank'):
                raise ObservationPending('Tab navigation is still pending')
            self.task.check_url(url)

    async def _frames(self, initial_blank=False):
        frames, _ = await self.session.get_all_frames()
        if not frames:
            raise NotDispatched('Frame scope unavailable')
        for frame in frames.values():
            if not initial_blank and frame.get('url', '') in ('', 'about:blank'):
                raise ObservationPending('Frame navigation is still pending')
            try:
                self._check_url(frame.get('url', ''), initial_blank)
            except ValueError as exc:
                raise NotDispatched('Frame outside task allowed_origins') from exc

    async def _metadata(self):
        cdp = await self.session.get_or_create_cdp_session(focus=False)
        result = await self._send(cdp, 'Runtime', 'evaluate', {
            'expression': '({url: location.href, title: document.title})', 'returnByValue': True,
        })
        if result.get('exceptionDetails') or not isinstance(result.get('result', {}).get('value'), dict):
            raise NotDispatched('Document metadata unavailable')
        return result['result']['value']

    async def observe(self) -> dict:
        for attempt in range(40):
            try:
                return await self._observe()
            except ObservationPending:
                if attempt == 39:
                    raise
                await asyncio.sleep(.1)

    async def _observe(self) -> dict:
        if self.session.agent_focus_target_id is None:
            view = {
                'url': 'about:blank', 'title': '', 'tabs': [], 'dom': '', 'text': '',
                'nodes': {}, 'target_id': None, 'dom_truncated': False,
                'dom_characters': 0, 'dom_sha256': hashlib.sha256(b'').hexdigest(),
                'nodes_truncated': False, 'nodes_total': 0,
            }
            fingerprint = hashlib.sha256(json.dumps(view, sort_keys=True).encode()).hexdigest()
            view['fingerprint'] = fingerprint
            self._captures[fingerprint] = (copy.deepcopy(view), {}, None)
            return view
        initial_blank = await self._initial_blank()
        await self._frames(initial_blank)
        raw = await self.session.get_browser_state_summary(include_screenshot=False, cached=False)
        metadata = await self._metadata()
        self._check_url(metadata['url'], initial_blank)
        self._check_url(raw.url, initial_blank)
        await self._frames(initial_blank)
        if raw.state_error:
            raise NotDispatched(raw.state_error)
        tabs = []
        for tab in raw.tabs:
            tab_id = str(tab.target_id)
            if tab_id not in self.session.task_target_ids:
                continue
            self._check_url(tab.url, initial_blank)
            tabs.append({'tab_id': tab_id, 'url': tab.url, 'title': tab.title})
        nodes, captured = {}, {}
        for index, node in list(raw.dom_state.selector_map.items())[:2000]:
            identity = {
                'backend_node_id': node.backend_node_id, 'session_id': str(node.session_id),
                'frame_id': node.frame_id, 'target_id': str(node.target_id),
                'tag': node.node_name.lower(), 'attrs': dict(node.attributes),
            }
            if identity['attrs'].get('type', '').lower() == 'password':
                identity['attrs'].pop('value', None)
            nodes[str(index)] = identity
            captured[index] = copy.copy(node)
            captured[index].attributes = dict(node.attributes)
            captured[index].snapshot_node = copy.copy(getattr(node, 'snapshot_node', None))
        dom = raw.dom_state.llm_representation()
        view = {
            'url': metadata['url'], 'title': metadata['title'], 'tabs': tabs,
            'dom': dom[:100000], 'text': dom[:100000], 'nodes': nodes,
            'dom_truncated': len(dom) > 100000, 'dom_characters': len(dom),
            'dom_sha256': hashlib.sha256(dom.encode()).hexdigest(),
            'nodes_truncated': len(raw.dom_state.selector_map) > 2000,
            'nodes_total': len(raw.dom_state.selector_map),
            'target_id': str(self.session.agent_focus_target_id),
        }
        fingerprint = hashlib.sha256(json.dumps(view, sort_keys=True).encode()).hexdigest()
        view['fingerprint'] = fingerprint
        self.observation = raw
        self._captures[fingerprint] = (copy.deepcopy(view), captured, view['target_id'])
        while len(self._captures) > 2:
            del self._captures[next(iter(self._captures))]
        return view

    async def content(self, start_from_char: int = 0) -> dict:
        from browser_use.dom.markdown_extractor import extract_clean_markdown

        if not isinstance(start_from_char, int) or start_from_char < 0:
            raise ValueError('Content offset must be a nonnegative integer')
        await self._frames()
        metadata = await self._metadata()
        url = metadata['url']
        self.task.check_url(url)
        markdown, _ = await extract_clean_markdown(
            browser_session=self.session, extract_links=True, extract_images=True,
        )
        await self._frames()
        current_url = (await self._metadata())['url']
        self.task.check_url(current_url)
        if current_url != url:
            raise NotDispatched('Page changed during extraction')
        if start_from_char > len(markdown):
            raise ValueError('Content offset exceeds document length')
        end = min(start_from_char + 100000, len(markdown))
        more = end < len(markdown)
        return {
            'url': url, 'title': metadata['title'],
            'text': markdown[start_from_char:end], 'start_from_char': start_from_char,
            'next_start_char': end if more else None,
            'truncated': more, 'total_characters': len(markdown),
        }

    async def screenshot(self) -> str:
        if self.session.agent_focus_target_id is None:
            raise NotDispatched('Navigate before taking a screenshot')
        await self._frames(await self._initial_blank())
        return base64.b64encode(await self.session.take_screenshot()).decode('ascii')

    async def _call(self, cdp, object_id, function, arguments=()):
        result = await self._send(cdp, 'Runtime', 'callFunctionOn', {
            'objectId': object_id, 'functionDeclaration': function,
            'arguments': [{'value': v} for v in arguments], 'returnByValue': True,
        })
        if result.get('exceptionDetails'):
            raise RuntimeError('Browser function failed')
        return result.get('result', {}).get('value')

    async def _node(self, index, captured):
        node = captured.get(index)
        if node is None or not node.session_id:
            raise NotDispatched('Index not in observation')
        try:
            cdp = await self.session.cdp_client_for_node(node)
            if str(cdp.session_id) != str(node.session_id):
                raise NotDispatched('Captured node session changed')
            result = await self._send(cdp, 'DOM', 'resolveNode', {'backendNodeId': node.backend_node_id})
            object_id = result.get('object', {}).get('objectId')
            if not object_id:
                raise NotDispatched('Captured node no longer resolves')
            snapshot = getattr(node, 'snapshot_node', None)
            text_input = node.node_name.upper() in ('INPUT', 'TEXTAREA')
            with_value = text_input and getattr(snapshot, 'input_value', None) is not None
            with_checked = text_input and getattr(snapshot, 'input_checked', None) is not None
            guard = await self._call(cdp, object_id, _GUARD, (with_value, with_checked))
            self.task.check_url(guard['url'])
            if (not guard['connected'] or guard['tag'] != node.node_name.lower()
                    or guard['attrs'] != node.attributes or guard['disabled']):
                raise NotDispatched('Captured node changed')
            for attribute in ('href', 'action', 'formaction'):
                value = guard['attrs'].get(attribute)
                if value:
                    self.task.check_url(urljoin(guard['url'], value))
            if guard.get('form_url'):
                self.task.check_url(guard['form_url'])
            return cdp, object_id, guard
        except NotDispatched:
            raise
        except Exception as exc:
            raise NotDispatched('Captured node validation failed') from exc

    async def _point(self, cdp, node, object_id):
        try:
            result = await self._send(cdp, 'DOM', 'getContentQuads', {'backendNodeId': node.backend_node_id})
            quads = result.get('quads', [])
            if not quads or len(quads[0]) != 8:
                raise NotDispatched('Captured node geometry unavailable')
            quad = quads[0]
            x, y = sum(quad[0::2]) / 4, sum(quad[1::2]) / 4
            metrics = await self._send(cdp, 'Page', 'getLayoutMetrics')
            viewport = metrics.get('cssLayoutViewport', metrics.get('layoutViewport', {}))
            hit = await self._send(cdp, 'DOM', 'getNodeForLocation', {
                'x': round(x + viewport.get('pageX', 0)), 'y': round(y + viewport.get('pageY', 0)),
                'includeUserAgentShadowDOM': True,
            })
            if hit.get('backendNodeId') != node.backend_node_id:
                other = await self._send(cdp, 'DOM', 'resolveNode', {'backendNodeId': hit['backendNodeId']})
                hit_id = other['object']['objectId']
                contains = await self._send(cdp, 'Runtime', 'callFunctionOn', {
                    'objectId': object_id,
                    'functionDeclaration': 'function(other) { return this.contains(other); }',
                    'arguments': [{'objectId': hit_id}], 'returnByValue': True,
                })
                if contains.get('exceptionDetails') or not contains.get('result', {}).get('value'):
                    raise NotDispatched('Captured node is covered at CDP coordinates')
            return x, y
        except NotDispatched:
            raise
        except Exception as exc:
            raise NotDispatched('Captured node geometry validation failed') from exc

    async def execute(self, action: Action, observed: dict) -> dict:
        capture = self._captures.get(observed.get('fingerprint'))
        if capture is None or capture[0] != observed:
            raise NotDispatched('Unknown observation')
        _, captured, target_id = capture
        focus = self.session.agent_focus_target_id
        if (str(focus) if focus is not None else None) != target_id:
            raise NotDispatched('Observed tab is no longer focused')
        kind = action.kind
        if target_id is None and kind != 'navigate':
            raise NotDispatched('Navigate before acting on a tab')
        if target_id is not None:
            await self._frames(await self._initial_blank())
        cdp = None if target_id is None else await self.session.get_or_create_cdp_session(target_id, focus=False)
        created_tab_id = None
        index = getattr(action, 'index', None)
        object_id = guard = None
        if index is not None:
            cdp, object_id, guard = await self._node(index, captured)
        if kind == 'navigate':
            try:
                self.task.check_url(action.url)
            except ValueError as exc:
                raise NotDispatched(str(exc)) from exc
            self._started_navigation = True
            if target_id is None or action.new_tab:
                root = await self.session.get_root_cdp_session()
                self._captures.pop(observed['fingerprint'])
                result = await self._send(root, 'Target', 'createTarget', {'url': 'about:blank', 'background': True})
                created_tab_id = self.created_tab_id = str(result['targetId'])
                await self.session.attach_task_target(created_tab_id, created=True)
                self.session.agent_focus_target_id = created_tab_id
                cdp = await self.session.get_or_create_cdp_session(created_tab_id, focus=False)
            result = await self._send(cdp, 'Page', 'navigate', {'url': action.url})
            if result.get('errorText'):
                raise RuntimeError(result['errorText'])
        elif kind == 'click':
            await self._send(cdp, 'DOM', 'scrollIntoViewIfNeeded', {'backendNodeId': captured[index].backend_node_id})
            cdp, object_id, guard = await self._node(index, captured)
            if not guard['visible'] or not guard['hit']:
                raise NotDispatched('Captured node is not hit-testable')
            x, y = await self._point(cdp, captured[index], object_id)
            for event in ('mousePressed', 'mouseReleased'):
                await self._send(cdp, 'Input', 'dispatchMouseEvent', {
                    'type': event, 'x': x, 'y': y, 'button': 'left', 'clickCount': 1,
                })
        elif kind == 'input':
            if guard['tag'] not in ('input', 'textarea') and not guard['editable']:
                raise NotDispatched('Node is not editable')
            if guard['attrs'].get('type', 'text').lower() in ('password', 'file', 'hidden', 'checkbox', 'radio', 'submit', 'button'):
                raise NotDispatched('Input type is not text')
            if 'readonly' in guard['attrs']:
                raise NotDispatched('Input is read-only')
            await self._send(cdp, 'DOM', 'focus', {'backendNodeId': captured[index].backend_node_id})
            await self._send(cdp, 'Input', 'dispatchKeyEvent', {'type': 'keyDown', 'key': 'a', 'code': 'KeyA', 'windowsVirtualKeyCode': 65, 'modifiers': 2, 'commands': ['selectAll']})
            await self._send(cdp, 'Input', 'dispatchKeyEvent', {'type': 'keyUp', 'key': 'a', 'code': 'KeyA', 'windowsVirtualKeyCode': 65, 'modifiers': 2})
            await self._send(cdp, 'Input', 'insertText', {'text': action.text})
        elif kind == 'select':
            if guard['tag'] != 'select' or not await self._call(cdp, object_id, _SELECT_CHECK, (action.value,)):
                raise NotDispatched('Select value is unavailable')
            await self._call(cdp, object_id, _SELECT, (action.value,))
        elif kind == 'upload':
            path = self.uploads.get(action.file)
            if not path:
                raise NotDispatched('Upload not in staged task files')
            if guard['tag'] != 'input' or guard['attrs'].get('type', '').lower() != 'file':
                raise NotDispatched('Node is not a file input')
            await self._send(cdp, 'DOM', 'setFileInputFiles', {'backendNodeId': captured[index].backend_node_id, 'files': [path]})
        elif kind == 'key':
            if index is not None:
                focused_id = object_id
            else:
                focused = await self._send(cdp, 'Runtime', 'evaluate', {
                    'expression': 'document.activeElement', 'returnByValue': False,
                })
                focused_id = focused.get('result', {}).get('objectId')
            if not focused_id:
                raise NotDispatched('Focused element unavailable')
            active = await self._call(cdp, focused_id, _GUARD)
            try:
                self.task.check_url(active['url'])
                if active.get('form_url'):
                    self.task.check_url(active['form_url'])
            except ValueError as exc:
                raise NotDispatched('Focused frame outside task scope') from exc
            if active['attrs'].get('type', '').lower() == 'password':
                raise NotDispatched('Password input requires a separate credential workflow')
            if active['tag'] in ('iframe', 'frame') or not active['connected']:
                raise NotDispatched('Focused frame cannot be safely targeted')
            if active['attrs'].get('href'):
                self.task.check_url(urljoin(active['url'], active['attrs']['href']))
            if index is not None:
                await self._send(cdp, 'DOM', 'focus', {'backendNodeId': captured[index].backend_node_id})
            key = ' ' if action.key == 'Space' else action.key
            params = {'key': key, 'code': action.key, 'windowsVirtualKeyCode': _KEY_CODES.get(action.key, 65), 'modifiers': 0}
            if action.key == 'Control+a':
                params = {'key': 'a', 'code': 'KeyA', 'windowsVirtualKeyCode': 65, 'modifiers': 2, 'commands': ['selectAll']}
            elif action.key in ('Enter', 'Space'):
                params['text'] = '\r' if action.key == 'Enter' else ' '
            await self._send(cdp, 'Input', 'dispatchKeyEvent', {**params, 'type': 'keyDown'})
            params.pop('text', None)
            params.pop('commands', None)
            await self._send(cdp, 'Input', 'dispatchKeyEvent', {**params, 'type': 'keyUp'})
        elif kind == 'scroll':
            if guard and (not guard['visible'] or not guard['hit']):
                raise NotDispatched('Scroll target is not hit-testable')
            await self._send(cdp, 'Page', 'bringToFront')
            if guard:
                cdp, object_id, guard = await self._node(index, captured)
                if not guard['visible'] or not guard['hit']:
                    raise NotDispatched('Scroll target is not hit-testable')
            x, y = await self._point(cdp, captured[index], object_id) if guard else (1, 1)
            await self._send(cdp, 'Input', 'dispatchMouseEvent', {
                'type': 'mouseWheel', 'x': x,
                'y': y, 'deltaX': action.dx, 'deltaY': action.dy,
            })
        elif kind in ('switch_tab', 'close_tab'):
            tabs = {t['tab_id']: t for t in observed['tabs']}
            if action.tab_id not in tabs or action.tab_id not in self.session.task_target_ids:
                raise NotDispatched('Tab not in scoped observation')
            if kind == 'close_tab' and action.tab_id not in self.session.created_target_ids:
                raise NotDispatched('Borrowed tabs cannot be closed')
            method = 'activateTarget' if kind == 'switch_tab' else 'closeTarget'
            await self._send(cdp, 'Target', method, {'targetId': action.tab_id})
            if kind == 'switch_tab':
                self.session.agent_focus_target_id = action.tab_id
            else:
                remaining = [t for t in tabs if t != action.tab_id]
                self.session.agent_focus_target_id = remaining[0] if remaining else None
        elif kind in ('back', 'forward'):
            history = await self._send(cdp, 'Page', 'getNavigationHistory')
            position = history['currentIndex'] + (-1 if kind == 'back' else 1)
            if position < 0 or position >= len(history['entries']):
                raise NotDispatched('History entry unavailable')
            entry = history['entries'][position]
            try:
                self.task.check_url(entry['url'])
            except ValueError as exc:
                raise NotDispatched('History entry outside task scope') from exc
            await self._send(cdp, 'Page', 'navigateToHistoryEntry', {'entryId': entry['id']})
        elif kind == 'reload':
            await self._send(cdp, 'Page', 'reload')
        elif kind == 'wait':
            await asyncio.sleep(action.seconds)
        else:
            raise NotDispatched('Action belongs to runtime')
        receipt = {'kind': kind, 'returned': True}
        if created_tab_id is not None:
            receipt.update(created_tab_id=created_tab_id, user_browser_kept_alive=True)
        return receipt
