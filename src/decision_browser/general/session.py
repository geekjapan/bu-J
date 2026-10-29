"""CDP session without autonomous browser actions, pinned to browser-use 0.13.14."""
from __future__ import annotations

import asyncio
from importlib.metadata import version
from types import SimpleNamespace
from urllib.parse import urlsplit

from pydantic import PrivateAttr

from browser_use.browser.events import AgentFocusChangedEvent
from browser_use.browser.session import BrowserSession, CDPSession, Target, TimeoutWrappedCDPClient
from browser_use.browser.session_manager import SessionManager

from .models import origin


class BoundedSessionManager(SessionManager):
    async def start_monitoring(self):
        client = self.browser_session._cdp_client_root
        self._request_tasks = set()
        self._attach_lock = asyncio.Lock()
        self._frame_ids = {}

        def schedule(coroutine):
            task = asyncio.create_task(coroutine)
            self._request_tasks.add(task)
            task.add_done_callback(self._request_tasks.discard)

        client.register.Fetch.requestPaused(lambda event, session_id=None: schedule(self._request_paused(event, session_id)))
        client.register.Target.targetCreated(lambda event, session_id=None: schedule(self._target_created(event)))
        await super().start_monitoring()
        # cdp-use has one callback slot; preserve the parent session the upstream callback drops.
        client.register.Target.attachedToTarget(
            lambda event, session_id=None: schedule(self._handle_target_attached(event, session_id)),
        )
        client.register.Page.frameNavigated(self._frame_navigated)
        client.register.Page.downloadWillBegin(self._download_started)
        client.register.Page.downloadProgress(self._download_progress)
        client.register.Browser.downloadWillBegin(self._download_started)
        client.register.Browser.downloadProgress(self._download_progress)

    async def _initialize_existing_targets(self):
        pass

    async def _target_created(self, event):
        info = event['targetInfo']
        browser = self.browser_session
        target_id = info['targetId']
        if (info['type'] != 'page' or info.get('openerId') not in browser._task_target_ids
                or target_id in browser._existing_page_ids or target_id in browser._task_target_ids):
            return
        result = await browser._cdp_client_root.send.Target.attachToTarget(
            params={'targetId': target_id, 'flatten': True},
        )
        await self._handle_target_attached({
            'targetInfo': info, 'sessionId': result['sessionId'], 'waitingForDebugger': False,
        })
        current = await browser._cdp_client_root.send.Target.getTargetInfo(params={'targetId': target_id})
        await self._handle_target_info_changed(current)

    def _frame_navigated(self, event, session_id=None):
        if session_id in self._sessions:
            self._frame_ids[event['frame']['id']] = session_id

    def _download_started(self, event, session_id=None):
        if self._frame_ids.get(event.get('frameId')) not in self._sessions or (
            session_id is not None and session_id not in self._sessions
        ):
            return
        downloads = self.browser_session._scoped_downloads
        if not any(item['guid'] == event['guid'] for item in downloads):
            downloads.append({
                'guid': event['guid'], 'url': event.get('url', ''),
                'suggested_filename': event.get('suggestedFilename', ''), 'state': 'inProgress',
            })

    def _download_progress(self, event, session_id=None):
        for download in self.browser_session._scoped_downloads:
            if download['guid'] == event['guid']:
                download['state'] = event['state']
                if event['state'] == 'completed' and event.get('filePath'):
                    download['filePath'] = event['filePath']
                break

    async def clear_pending_tasks(self):
        tasks = list(getattr(self, '_request_tasks', ()))
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    async def clear(self):
        await self.clear_pending_tasks()
        await super().clear()

    async def _request_paused(self, event, session_id=None):
        if not session_id:
            raise RuntimeError('Document request without a CDP session')
        if session_id not in self._sessions:
            raise RuntimeError('Document request outside task scope')
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

    async def _handle_target_attached(self, event, parent_session_id=None):
        async with self._attach_lock:
            client = self.browser_session._cdp_client_root
            if client is None or self.browser_session._intentional_stop:
                return
            info, session_id = event['targetInfo'], event['sessionId']
            if session_id in self._sessions:
                return
            target_id, target_type = info['targetId'], info['type']
            browser = self.browser_session
            explicit = target_id in browser._task_root_ids
            related = (parent_session_id in self._sessions
                       or info.get('openerId') in browser._task_target_ids
                       or info.get('parentId') in browser._task_target_ids)
            existing_page = target_id in browser._existing_page_ids
            if not explicit and (not related or existing_page or target_type not in ('page', 'iframe', 'tab', 'worker', 'service_worker')):
                # Only a scoped auto-attach may have paused a target we do not own.
                if event.get('waitingForDebugger') and parent_session_id in self._sessions:
                    await client.send.Runtime.runIfWaitingForDebugger(session_id=session_id)
                await client.send.Target.detachFromTarget(params={'sessionId': session_id})
                return
            browser._task_target_ids.add(target_id)
            if not explicit and target_type == 'page':
                browser._created_target_ids.add(target_id)
                browser._initial_blank_targets.add(target_id)
            # Children stay paused until recursive interception is installed.
            await client.send.Target.setAutoAttach(
                params={'autoAttach': True, 'waitForDebuggerOnStart': True, 'flatten': True}, session_id=session_id,
            )
            cdp_session = CDPSession(cdp_client=client, target_id=target_id, session_id=session_id)
            async with self._lock:
                self._target_sessions.setdefault(target_id, set()).add(session_id)
                self._session_to_target[session_id] = target_id
                self._targets[target_id] = Target(
                    target_id=target_id, target_type=target_type,
                    url=info.get('url', 'about:blank'), title=info.get('title', ''),
                )
                self._sessions[session_id] = cdp_session
            if target_type in ('page', 'iframe', 'tab'):
                await client.send.Fetch.enable(params={'patterns': [
                    {'urlPattern': '*', 'resourceType': 'Document', 'requestStage': 'Request'},
                ]}, session_id=session_id)
            if target_type in ('page', 'iframe', 'tab'):
                await self._enable_page_monitoring(cdp_session)
                tree = await client.send.Page.getFrameTree(session_id=session_id)
                def remember(frame_tree):
                    if frame_tree.get('frame', {}).get('id'):
                        self._frame_ids[frame_tree['frame']['id']] = session_id
                    for child in frame_tree.get('childFrames', []):
                        remember(child)
                remember(tree.get('frameTree', {}))
            if event.get('waitingForDebugger'):
                await client.send.Runtime.runIfWaitingForDebugger(session_id=session_id)

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
    _selected_target_id: str | None = PrivateAttr(default=None)
    _task_root_ids: set[str] = PrivateAttr(default_factory=set)
    _task_target_ids: set[str] = PrivateAttr(default_factory=set)
    _created_target_ids: set[str] = PrivateAttr(default_factory=set)
    _existing_page_ids: set[str] = PrivateAttr(default_factory=set)
    _scoped_downloads: list[dict] = PrivateAttr(default_factory=list)

    @property
    def task_target_ids(self):
        return set(self._task_target_ids)

    @property
    def created_target_ids(self):
        return set(self._created_target_ids)

    @property
    def downloads(self):
        return [dict(item) for item in self._scoped_downloads]

    async def start(self):
        await self.attach_all_watchdogs()
        await self.connect()

    async def get_root_cdp_session(self):
        return SimpleNamespace(cdp_client=self.cdp_client, target_id=None, session_id=None)

    async def attach_task_target(self, target_id, created=False):
        client = self.cdp_client
        info = (await client.send.Target.getTargetInfo(params={'targetId': target_id}))['targetInfo']
        if info['type'] != 'page':
            raise ValueError('Task target must be a page')
        self._task_root_ids.add(target_id)
        if created:
            self._created_target_ids.add(target_id)
            self._initial_blank_targets.add(target_id)
        attached = await client.send.Target.attachToTarget(params={'targetId': target_id, 'flatten': True})
        await self.session_manager._handle_target_attached({
            'targetInfo': info, 'sessionId': attached['sessionId'], 'waitingForDebugger': False,
        })
        await client.send.Target.autoAttachRelated(params={'targetId': target_id, 'waitForDebuggerOnStart': True})
        self.agent_focus_target_id = target_id
        self.event_bus.dispatch(AgentFocusChangedEvent(target_id=target_id, url=info.get('url', 'about:blank')))

    async def stop(self):
        self._intentional_stop = True
        self.agent_focus_target_id = None
        client = self._cdp_client_root
        if client is None:
            return
        manager = self.session_manager
        try:
            if manager is not None:
                await manager.clear_pending_tasks()
                for session_id in list(manager._sessions):
                    for domain, method, params in (
                        ('Fetch', 'disable', {}),
                        ('Target', 'setAutoAttach', {'autoAttach': False, 'waitForDebuggerOnStart': False, 'flatten': True}),
                        ('Target', 'detachFromTarget', {'sessionId': session_id}),
                    ):
                        try:
                            await getattr(getattr(client.send, domain), method)(
                                params=params, session_id=None if method == 'detachFromTarget' else session_id,
                            )
                        except Exception:
                            self.logger.debug('Scoped detach command failed', exc_info=True)
                await manager.clear()
        finally:
            try:
                await client.stop()
            finally:
                self._cdp_client_root = None
                self.agent_focus_target_id = None
                await self.event_bus.stop(clear=True, timeout=5)

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
            result = await self._cdp_client_root.send.Target.getTargets()
            self._existing_page_ids = {
                info['targetId'] for info in result.get('targetInfos', []) if info['type'] == 'page'
            }
            if self._selected_target_id is not None:
                if self._selected_target_id not in self._existing_page_ids:
                    raise ValueError('Selected target not found')
                await self.attach_task_target(self._selected_target_id)
            return self
        except BaseException:
            await self._cdp_client_root.stop()
            self._cdp_client_root = None
            raise


def make_session(*, target_id: str | None = None, **options) -> BoundedBrowserSession:
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
    # Display detection must not resize an existing CDP browser on a displayless host.
    options['headless'] = False
    options['no_viewport'] = True
    options['highlight_elements'] = False
    options['dom_highlight_elements'] = False
    options['keep_alive'] = True
    options['enable_default_extensions'] = False
    options['auto_download_pdfs'] = False
    session = BoundedBrowserSession(**options)
    session._bounded_origins = frozenset(allowed)
    session._selected_target_id = target_id
    return session
