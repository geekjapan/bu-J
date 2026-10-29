"""CDP session without autonomous browser actions, pinned to browser-use 0.13.14."""
from __future__ import annotations

import asyncio
from importlib.metadata import version
from urllib.parse import urlsplit

from pydantic import PrivateAttr

from browser_use.browser.events import AgentFocusChangedEvent, TabCreatedEvent
from browser_use.browser.session import BrowserSession, CDPSession, Target, TimeoutWrappedCDPClient
from browser_use.browser.session_manager import SessionManager

from .models import origin


class BoundedSessionManager(SessionManager):
    async def start_monitoring(self):
        client = self.browser_session._cdp_client_root
        self._request_tasks = set()
        def paused(event, session_id=None):
            task = asyncio.create_task(self._request_paused(event, session_id))
            self._request_tasks.add(task)
            task.add_done_callback(self._request_tasks.discard)
        client.register.Fetch.requestPaused(paused)
        await super().start_monitoring()

    async def clear(self):
        tasks = list(getattr(self, '_request_tasks', ()))
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await super().clear()

    async def _request_paused(self, event, session_id=None):
        if not session_id:
            raise RuntimeError('Document request without a CDP session')
        client = self.browser_session._cdp_client_root
        url = event['request']['url']
        try:
            allowed = origin(url) in self.browser_session._bounded_origins
        except ValueError:
            allowed = False
        if url == 'about:blank':
            target_id = self.get_target_id_from_session_id(session_id)
            allowed = target_id in self.browser_session._initial_blank_targets
        if allowed:
            if url != 'about:blank':
                self.browser_session._initial_blank_targets.discard(self.get_target_id_from_session_id(session_id))
            await client.send.Fetch.continueRequest(params={'requestId': event['requestId']}, session_id=session_id)
        else:
            await client.send.Fetch.failRequest(
                params={'requestId': event['requestId'], 'errorReason': 'BlockedByClient'}, session_id=session_id,
            )

    async def _handle_target_attached(self, event):
        client = self.browser_session._cdp_client_root
        if client is None:
            return
        info, session_id = event['targetInfo'], event['sessionId']
        target_id, target_type = info['targetId'], info['type']
        # Children stay paused until their own interception is installed.
        await client.send.Target.setAutoAttach(
            params={'autoAttach': True, 'waitForDebuggerOnStart': True, 'flatten': True}, session_id=session_id,
        )
        if target_type in ('page', 'iframe', 'tab'):
            await client.send.Fetch.enable(params={'patterns': [
                {'urlPattern': '*', 'resourceType': 'Document', 'requestStage': 'Request'},
            ]}, session_id=session_id)
        cdp_session = CDPSession(cdp_client=client, target_id=target_id, session_id=session_id)
        async with self._lock:
            self._target_sessions.setdefault(target_id, set()).add(session_id)
            self._session_to_target[session_id] = target_id
            self._targets[target_id] = Target(
                target_id=target_id, target_type=target_type,
                url=info.get('url', 'about:blank'), title=info.get('title', ''),
            )
            self._sessions[session_id] = cdp_session
        if event.get('waitingForDebugger'):
            await client.send.Runtime.runIfWaitingForDebugger(session_id=session_id)
        if target_type in ('page', 'tab'):
            await self._enable_page_monitoring(cdp_session)

    async def _recover_agent_focus(self, crashed_target_id):
        if self.browser_session.agent_focus_target_id == crashed_target_id:
            self.browser_session.agent_focus_target_id = None
        self._recovery_in_progress = False
        if self._recovery_complete_event:
            self._recovery_complete_event.set()


class BoundedBrowserSession(BrowserSession):
    _bounded_origins: frozenset[str] = PrivateAttr(default_factory=frozenset)
    _initial_blank_targets: set[str] = PrivateAttr(default_factory=set)
    _bounded_connected: bool = PrivateAttr(default=False)

    async def attach_all_watchdogs(self):
        if self._watchdogs_attached:
            return
        from browser_use.browser.watchdogs.dom_watchdog import DOMWatchdog
        from browser_use.browser.watchdogs.screenshot_watchdog import ScreenshotWatchdog
        for attribute, watchdog in (('_dom_watchdog', DOMWatchdog), ('_screenshot_watchdog', ScreenshotWatchdog)):
            watchdog.model_rebuild()
            instance = watchdog(event_bus=self.event_bus, browser_session=self)
            setattr(self, attribute, instance)
            instance.attach_to_session()
        self._watchdogs_attached = True

    def _attach_ws_drop_callback(self):
        pass

    async def _auto_reconnect(self, max_attempts=3):
        raise RuntimeError('Bounded browser sessions cannot reconnect')

    async def reconnect(self):
        raise RuntimeError('Bounded browser sessions cannot reconnect')

    async def connect(self, cdp_url=None):
        endpoint = cdp_url or self.cdp_url
        parsed = urlsplit(endpoint or '')
        if parsed.scheme not in ('ws', 'wss') or not parsed.hostname or parsed.username or parsed.password:
            raise ValueError('Existing websocket CDP endpoint required')
        if self._bounded_connected or self._cdp_client_root is not None:
            raise RuntimeError('Bounded browser sessions connect only once')
        self._bounded_connected = True
        self.browser_profile.cdp_url = endpoint
        self._cdp_client_root = TimeoutWrappedCDPClient(
            endpoint, additional_headers=dict(self.browser_profile.headers or {}) or None,
            max_ws_frame_size=200 * 1024 * 1024,
        )
        try:
            await self._cdp_client_root.start()
            self.session_manager = BoundedSessionManager(self)
            await self.session_manager.start_monitoring()
            await self._cdp_client_root.send.Target.setAutoAttach(
                params={'autoAttach': True, 'waitForDebuggerOnStart': True, 'flatten': True},
            )
            pages = self.session_manager.get_all_page_targets()
            if not pages:
                raise RuntimeError('Owned browser must already have a page')
            for target in pages:
                if target.url == 'about:blank':
                    self._initial_blank_targets.add(target.target_id)
                elif origin(target.url) not in self._bounded_origins:
                    raise ValueError('Existing page outside allowed origins')
            await self._cdp_client_root.send.Browser.setDownloadBehavior(
                params={'behavior': 'allow', 'downloadPath': '/tmp/bu-j-downloads', 'eventsEnabled': False},
            )
            await self.get_or_create_cdp_session(pages[0].target_id, focus=True)
            for target in pages:
                self.event_bus.dispatch(TabCreatedEvent(url=target.url, target_id=target.target_id))
            self.event_bus.dispatch(AgentFocusChangedEvent(target_id=pages[0].target_id, url=pages[0].url))
            return self
        except BaseException:
            await self._cdp_client_root.stop()
            self._cdp_client_root = None
            raise


def make_session(**options) -> BoundedBrowserSession:
    if version('browser-use') != '0.13.14':
        raise RuntimeError('Bounded session requires browser-use 0.13.14')
    allowed = options.get('allowed_domains') or []
    if not allowed or any(origin(value) != value for value in allowed):
        raise ValueError('Exact allowed origins required')
    endpoint = urlsplit(options.get('cdp_url') or '')
    if endpoint.scheme not in ('ws', 'wss') or not endpoint.hostname or endpoint.username or endpoint.password:
        raise ValueError('Existing websocket CDP endpoint required')
    if options.get('proxy'):
        raise ValueError('Proxy authentication is unsupported by bounded sessions')
    options['downloads_path'] = '/tmp/bu-j-downloads'
    options['keep_alive'] = True
    options['enable_default_extensions'] = False
    options['auto_download_pdfs'] = False
    session = BoundedBrowserSession(**options)
    session._bounded_origins = frozenset(allowed)
    return session
