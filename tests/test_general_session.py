import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from decision_browser.general import session as bounded


class Commands:
    def __init__(self, client, domain=''):
        self.client, self.domain = client, domain

    def __getattr__(self, name):
        if not self.domain:
            return Commands(self.client, name)

        async def send(params=None, session_id=None):
            self.client.calls.append((self.domain, name, params or {}, session_id))
            if name == 'getTargets':
                return {'targetInfos': self.client.targets}
            if name == 'getTargetInfo':
                return {'targetInfo': next(info for info in self.client.targets if info['targetId'] == params['targetId'])}
            if name == 'attachToTarget':
                return {'sessionId': 's-' + params['targetId']}
            if name == 'getFrameTree':
                return {'frameTree': {'frame': {'id': 'f-' + session_id}}}
            return {}
        return send


class Registrations:
    def __init__(self, client, domain=''):
        self.client, self.domain = client, domain

    def __getattr__(self, name):
        if not self.domain:
            return Registrations(self.client, name)
        return lambda callback: self.client.callbacks.__setitem__((self.domain, name), callback)


def fake_client():
    client = Mock(spec=bounded.TimeoutWrappedCDPClient)
    client.calls, client.callbacks, client.targets = [], {}, []
    client.start, client.stop = AsyncMock(), AsyncMock()
    client.send, client.register = Commands(client), Registrations(client)
    return client


@pytest.fixture
def session():
    instance = bounded.make_session(cdp_url='ws://owned:9222/devtools/browser/id', allowed_domains=['https://example.com'])
    instance._cdp_client_root = fake_client()
    instance.session_manager = bounded.BoundedSessionManager(instance)
    instance.session_manager._attach_lock = asyncio.Lock()
    instance.session_manager._frame_ids = {}
    object.__setattr__(instance, 'event_bus', SimpleNamespace(dispatch=Mock(), stop=AsyncMock()))
    return instance


async def attach(session, target_id='t', target_type='page', parent=None, **info):
    await session.session_manager._handle_target_attached({
        'targetInfo': {'targetId': target_id, 'type': target_type, 'url': 'about:blank', **info},
        'sessionId': 's-' + target_id, 'waitingForDebugger': True,
    }, parent)


async def test_only_observation_watchdogs_attach(session):
    from browser_use.browser.session import BrowserSession
    real_bus = BrowserSession(cdp_url='ws://owned:9222/devtools/browser/id').event_bus
    object.__setattr__(session, 'event_bus', real_bus)
    await session.attach_all_watchdogs()
    await session.attach_all_watchdogs()
    assert session._watchdogs_attached
    assert session._dom_watchdog is not None
    assert session._screenshot_watchdog is not None
    for name in ('_popups_watchdog', '_aboutblank_watchdog', '_security_watchdog',
                 '_default_action_watchdog', '_downloads_watchdog', '_local_browser_watchdog'):
        assert getattr(session, name, None) is None
    assert session._cdp_client_root.calls == []


async def test_focus_loss_does_not_activate_or_create(session):
    session.agent_focus_target_id = 'lost'
    await session.session_manager._recover_agent_focus('lost')
    assert session.agent_focus_target_id is None
    assert session._cdp_client_root.calls == []


async def test_reconnect_is_never_retried(session):
    session._attach_ws_drop_callback()
    with pytest.raises(RuntimeError, match='cannot reconnect'):
        await session._auto_reconnect()
    with pytest.raises(RuntimeError, match='cannot reconnect'):
        await session.reconnect()
    assert session._cdp_client_root.calls == []


@pytest.mark.parametrize('url,method', [
    ('https://example.com/path', 'continueRequest'),
    ('https://example.com.evil/path', 'failRequest'),
    ('https://elsewhere.example/', 'failRequest'),
    ('http://example.com/', 'failRequest'),
    ('file:///etc/passwd', 'failRequest'),
    ('chrome://newtab/', 'failRequest'),
    ('about:blank', 'failRequest'),
])
async def test_document_interception_checks_exact_origin(session, url, method):
    session.session_manager._sessions['s'] = object()
    await session.session_manager._request_paused({'requestId': 'r', 'request': {'url': url}}, 's')
    assert session._cdp_client_root.calls[0][0:2] == ('Fetch', method)
    assert len(session._cdp_client_root.calls) == 1


async def test_foreign_session_request_is_untouched(session):
    with pytest.raises(RuntimeError, match='outside task scope'):
        await session.session_manager._request_paused({'requestId': 'r', 'request': {'url': 'https://elsewhere.example/'}}, 'foreign')
    assert session._cdp_client_root.calls == []


async def test_initial_blank_permission_expires_on_first_navigation(session):
    manager = session.session_manager
    manager._sessions['s'] = object()
    manager._session_to_target['s'] = 't'
    session._initial_blank_targets.add('t')
    for url in ('about:blank', 'https://example.com/', 'about:blank'):
        await manager._request_paused({'requestId': 'r', 'request': {'url': url}}, 's')
    assert [call[1] for call in session._cdp_client_root.calls] == ['continueRequest', 'continueRequest', 'failRequest']


@pytest.mark.parametrize('target_type', ['page', 'iframe'])
async def test_paused_target_gets_interception_before_resume(session, target_type):
    session._task_root_ids.add('t')
    await attach(session, target_type=target_type)
    calls = session._cdp_client_root.calls
    assert [m for _, m, _, _ in calls][:2] == ['setAutoAttach', 'enable']
    assert calls[-1][1] == 'runIfWaitingForDebugger'
    assert calls[0][2]['waitForDebuggerOnStart'] is True
    assert calls[0][3] == 's-t'
    assert calls[1][2]['patterns'] == [{'urlPattern': '*', 'resourceType': 'Document', 'requestStage': 'Request'}]


async def test_interception_failure_leaves_new_target_paused(session):
    session._task_root_ids.add('t')
    session._cdp_client_root.send = SimpleNamespace(
        Target=Commands(session._cdp_client_root, 'Target'),
        Fetch=SimpleNamespace(enable=AsyncMock(side_effect=RuntimeError('broken'))),
        Runtime=Commands(session._cdp_client_root, 'Runtime'),
    )
    with pytest.raises(RuntimeError, match='broken'):
        await attach(session)
    assert all(m != 'runIfWaitingForDebugger' for _, m, _, _ in session._cdp_client_root.calls)


async def test_scope_adopts_children_and_new_popups_but_never_preexisting_pages(session):
    session._task_root_ids.add('t')
    session._existing_page_ids.update({'t', 'old-popup'})
    await attach(session)
    await attach(session, 'frame', 'iframe', parent='s-t')
    await attach(session, 'popup', openerId='t')
    before = len(session._cdp_client_root.calls)
    await attach(session, 'old-popup', parent='s-t', openerId='t')
    await attach(session, 'foreign')
    assert session.task_target_ids == {'t', 'frame', 'popup'}
    assert session.created_target_ids == {'popup'}
    assert all(m in ('runIfWaitingForDebugger', 'detachFromTarget') for _, m, _, _ in session._cdp_client_root.calls[before:])


async def test_monitoring_replacement_preserves_parent_session(session):
    await session.session_manager.start_monitoring()
    session._task_root_ids.add('t')
    await attach(session)
    callback = session._cdp_client_root.callbacks['Target', 'attachedToTarget']
    callback({'targetInfo': {'targetId': 'frame', 'type': 'iframe'}, 'sessionId': 's-frame'}, 's-t')
    await asyncio.gather(*session.session_manager._request_tasks)
    assert 'frame' in session.task_target_ids


@pytest.mark.parametrize('selected', [None, 't'])
async def test_connect_ignores_unrelated_foreign_urls_and_never_navigates(monkeypatch, selected):
    instance = bounded.make_session(cdp_url='ws://owned:9222/devtools/browser/id', target_id=selected, allowed_domains=['https://example.com'])
    client = fake_client()
    client.targets = [
        {'targetId': 't', 'type': 'page', 'url': 'https://example.com/form', 'title': 'Unsaved'},
        {'targetId': 'foreign', 'type': 'page'},
    ]
    object.__setattr__(instance, 'event_bus', SimpleNamespace(dispatch=Mock(), stop=AsyncMock()))
    monkeypatch.setattr(bounded, 'TimeoutWrappedCDPClient', lambda *a, **k: client)
    await instance.connect()
    assert instance.cdp_client is client
    assert instance.agent_focus_target_id == selected
    assert instance.task_target_ids == ({'t'} if selected else set())
    assert instance.created_target_ids == set()
    assert not any(m in ('navigate', 'createTarget', 'setDownloadBehavior', 'setDeviceMetricsOverride', 'activateTarget') for _, m, _, _ in client.calls)
    assert not any(m == 'setAutoAttach' and sid is None for _, m, _, sid in client.calls)
    assert all(params.get('targetId') != 'foreign' for _, _, params, _ in client.calls)
    assert not any(type(call.args[0]).__name__ == 'TabCreatedEvent' for call in instance.event_bus.dispatch.call_args_list)
    if selected:
        assert any(m == 'autoAttachRelated' and params['targetId'] == selected for _, m, params, _ in client.calls)
    await instance.stop()


async def test_connect_empty_browser_leaves_bootstrap_focus_unset(monkeypatch):
    instance = bounded.make_session(cdp_url='ws://owned:9222/devtools/browser/id', allowed_domains=['https://example.com'])
    client = fake_client()
    monkeypatch.setattr(bounded, 'TimeoutWrappedCDPClient', lambda *a, **k: client)
    await instance.connect()
    root = await instance.get_root_cdp_session()
    assert root.cdp_client is client and root.session_id is None
    assert instance.agent_focus_target_id is None
    assert not any(m in ('createTarget', 'attachToTarget', 'setAutoAttach') for _, m, _, _ in client.calls)
    await instance.stop()


async def test_missing_selected_tab_fails_without_attaching(monkeypatch):
    instance = bounded.make_session(cdp_url='ws://owned:9222/devtools/browser/id', target_id='missing', allowed_domains=['https://example.com'])
    client = fake_client()
    monkeypatch.setattr(bounded, 'TimeoutWrappedCDPClient', lambda *a, **k: client)
    with pytest.raises(ValueError, match='Selected target not found'):
        await instance.connect()
    client.stop.assert_awaited_once()
    assert not any(m == 'attachToTarget' for _, m, _, _ in client.calls)


async def test_created_and_borrowed_ownership_and_detach_only_stop(session):
    client = session.cdp_client
    client.targets = [{'targetId': tid, 'type': 'page', 'url': 'about:blank'} for tid in ('borrowed', 'created')]
    await session.attach_task_target('borrowed')
    await session.attach_task_target('created', created=True)
    assert session.task_target_ids == {'borrowed', 'created'}
    assert session.created_target_ids == {'created'}
    scope = session.task_target_ids
    scope.add('foreign')
    assert 'foreign' not in session.task_target_ids
    before = len(client.calls)
    await session.stop()
    calls = client.calls[before:]
    assert {m for _, m, _, _ in calls} == {'disable', 'setAutoAttach', 'detachFromTarget'}
    assert all(params.get('autoAttach') is False for _, m, params, _ in calls if m == 'setAutoAttach')
    assert {params['sessionId'] for _, m, params, _ in calls if m == 'detachFromTarget'} == {'s-borrowed', 's-created'}
    client.stop.assert_awaited_once()
    await session.stop()
    client.stop.assert_awaited_once()


async def test_download_metadata_requires_scoped_frame_and_known_guid(session):
    session._task_root_ids.add('t')
    await attach(session)
    manager = session.session_manager
    manager._download_started({'guid': 'foreign', 'frameId': 'other', 'url': 'https://private.example/'})
    manager._download_progress({'guid': 'foreign', 'state': 'completed'})
    event = {'guid': 'owned', 'frameId': 'f-s-t', 'url': 'https://example.com/file', 'suggestedFilename': 'report.txt'}
    manager._download_started(event, 's-t')
    manager._download_started(event)
    manager._download_progress({'guid': 'owned', 'state': 'completed'})
    assert session.downloads == [{'guid': 'owned', 'url': event['url'], 'suggested_filename': 'report.txt', 'state': 'completed'}]
    manager._sessions.clear()
    manager._download_started({**event, 'guid': 'detached'})
    assert len(session.downloads) == 1
    assert not any(m == 'setDownloadBehavior' for _, m, _, _ in session.cdp_client.calls)


def test_factory_preserves_viewport_and_disables_highlights(session):
    assert session.browser_profile.no_viewport
    assert not session.browser_profile.highlight_elements
    assert not session.browser_profile.dom_highlight_elements


def test_factory_rejects_version_drift(monkeypatch):
    monkeypatch.setattr(bounded, 'version', lambda name: '0.13.15')
    with pytest.raises(RuntimeError, match='0.13.14'):
        bounded.make_session(allowed_domains=['https://example.com'])


@pytest.mark.parametrize('allowed', [[], ['https://example.com/path'], ['https://example.com/']])
def test_factory_requires_exact_origins(allowed):
    with pytest.raises(ValueError, match='Exact allowed origins'):
        bounded.make_session(allowed_domains=allowed)


async def test_connect_cannot_be_repeated_after_transport_failure(session):
    session._cdp_client_root = None
    session._bounded_connected = True
    with pytest.raises(RuntimeError, match='connect only once'):
        await session.connect()


@pytest.mark.parametrize('endpoint', [None, 'http://owned:9222', 'ws://user:password@owned:9222'])
def test_factory_cannot_launch_or_discover_browser(endpoint):
    with pytest.raises(ValueError, match='Existing websocket CDP endpoint required'):
        bounded.make_session(cdp_url=endpoint, allowed_domains=['https://example.com'])


async def test_interception_scope_registered_before_fetch_enable(session):
    session._task_root_ids.add('t')
    client = session._cdp_client_root

    async def enable(**kwargs):
        await session.session_manager._request_paused({
            'requestId': 'during-enable', 'request': {'url': 'https://example.com/frame'},
        }, kwargs['session_id'])

    client.send = SimpleNamespace(
        Target=Commands(client, 'Target'), Fetch=SimpleNamespace(enable=enable,
            continueRequest=Commands(client, 'Fetch').continueRequest),
        Page=Commands(client, 'Page'), Network=Commands(client, 'Network'),
        Runtime=Commands(client, 'Runtime'),
    )
    await attach(session)
    assert any(method == 'continueRequest' for _, method, _, _ in client.calls)
    assert client.calls[-1][1] == 'runIfWaitingForDebugger'


async def test_completed_scoped_download_retains_browser_reported_file_path(session):
    session._scoped_downloads.append({'guid': 'owned', 'state': 'inProgress'})
    manager = session.session_manager
    manager._download_progress({'guid': 'unknown', 'state': 'completed', 'filePath': '/private/unrelated'})
    assert session.downloads == [{'guid': 'owned', 'state': 'inProgress'}]
    manager._download_progress({'guid': 'owned', 'state': 'completed', 'filePath': r'C:\Downloads\report.txt'})
    assert session.downloads == [{'guid': 'owned', 'state': 'completed', 'filePath': r'C:\Downloads\report.txt'}]


async def test_root_delivered_related_iframe_is_scoped_and_resumed(session):
    session._task_root_ids.add('t')
    await attach(session)
    await attach(session, 'frame', 'iframe', parentId='t')
    assert session.task_target_ids == {'t', 'frame'}
    assert session.created_target_ids == set()
    calls = [call for call in session.cdp_client.calls if call[3] == 's-frame']
    assert any(method == 'enable' and domain == 'Fetch' for domain, method, _, _ in calls)
    assert calls[-1][1] == 'runIfWaitingForDebugger'
    assert not any(method == 'detachFromTarget' for _, method, _, _ in session.cdp_client.calls)


async def test_noopener_popup_discovery_uses_cdp_opener_not_url(session):
    session._task_root_ids.add('t')
    await attach(session)
    session.cdp_client.targets = [{'targetId': 'popup', 'type': 'page',
                                   'url': 'https://example.com/detail', 'title': 'Loaded quotation'}]
    await session.session_manager._target_created({'targetInfo': {
        'targetId': 'popup', 'type': 'page', 'openerId': 't',
        'openerFrameId': 't', 'canAccessOpener': False, 'url': '',
    }})
    assert session.task_target_ids == {'t', 'popup'}
    assert session.created_target_ids == {'popup'}
    popup = session.session_manager._targets['popup']
    assert popup.url == 'https://example.com/detail' and popup.title == 'Loaded quotation'
    assert any(method == 'attachToTarget' and params['targetId'] == 'popup'
               for _, method, params, _ in session.cdp_client.calls)


@pytest.mark.parametrize('info', [
    {'targetId': 'foreign', 'type': 'page', 'url': 'https://example.com'},
    {'targetId': 'foreign', 'type': 'page', 'openerId': 'unrelated'},
    {'targetId': 'old', 'type': 'page', 'openerId': 't'},
    {'targetId': 'worker', 'type': 'worker', 'openerId': 't'},
])
async def test_popup_discovery_never_adopts_unrelated_or_existing_targets(session, info):
    session._task_target_ids.add('t')
    session._existing_page_ids.add('old')
    await session.session_manager._target_created({'targetInfo': info})
    assert session.cdp_client.calls == []
    assert session.task_target_ids == {'t'}
