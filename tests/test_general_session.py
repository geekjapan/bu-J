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
            return {}
        return send


@pytest.fixture
def session():
    instance = bounded.make_session(cdp_url='ws://owned:9222/devtools/browser/id', allowed_domains=['https://example.com'])
    client = Mock(spec=bounded.TimeoutWrappedCDPClient)
    client.calls, client.start, client.stop = [], AsyncMock(), AsyncMock()
    client.send = Commands(client)
    instance._cdp_client_root = client
    instance.session_manager = bounded.BoundedSessionManager(instance)
    return instance


async def test_only_observation_watchdogs_attach(session):
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
    await session.session_manager._request_paused({'requestId': 'r', 'request': {'url': url}}, 's')
    assert session._cdp_client_root.calls[0][0:2] == ('Fetch', method)
    assert len(session._cdp_client_root.calls) == 1


async def test_initial_blank_permission_expires_on_first_navigation(session):
    manager = session.session_manager
    manager._session_to_target['s'] = 't'
    session._initial_blank_targets.add('t')
    for url in ('about:blank', 'https://example.com/', 'about:blank'):
        await manager._request_paused({'requestId': 'r', 'request': {'url': url}}, 's')
    assert [call[1] for call in session._cdp_client_root.calls] == ['continueRequest', 'continueRequest', 'failRequest']


@pytest.mark.parametrize('target_type', ['page', 'iframe'])
async def test_paused_target_gets_interception_before_resume(session, target_type):
    session.session_manager._enable_page_monitoring = AsyncMock()
    await session.session_manager._handle_target_attached({
        'targetInfo': {'targetId': 't', 'type': target_type, 'url': 'about:blank'},
        'sessionId': 's', 'waitingForDebugger': True,
    })
    calls = session._cdp_client_root.calls
    assert [(d, m) for d, m, _, _ in calls] == [
        ('Target', 'setAutoAttach'), ('Fetch', 'enable'), ('Runtime', 'runIfWaitingForDebugger'),
    ]
    assert calls[0][2]['waitForDebuggerOnStart'] is True
    assert calls[1][2]['patterns'] == [{'urlPattern': '*', 'resourceType': 'Document', 'requestStage': 'Request'}]


async def test_interception_failure_leaves_new_target_paused(session):
    session._cdp_client_root.send = SimpleNamespace(
        Target=Commands(session._cdp_client_root, 'Target'),
        Fetch=SimpleNamespace(enable=AsyncMock(side_effect=RuntimeError('broken'))),
        Runtime=Commands(session._cdp_client_root, 'Runtime'),
    )
    with pytest.raises(RuntimeError, match='broken'):
        await session.session_manager._handle_target_attached({
            'targetInfo': {'targetId': 't', 'type': 'page'}, 'sessionId': 's', 'waitingForDebugger': True,
        })
    assert all(m != 'runIfWaitingForDebugger' for _, m, _, _ in session._cdp_client_root.calls)


@pytest.mark.parametrize('url', ['about:blank', 'chrome://newtab/', 'https://outside.example/'])
async def test_connect_requires_existing_scoped_page_without_navigation(monkeypatch, url):
    instance = bounded.make_session(cdp_url='ws://owned:9222/devtools/browser/id', allowed_domains=['https://example.com'])
    client = SimpleNamespace(calls=[], start=AsyncMock(), stop=AsyncMock())
    client.send = Commands(client)
    object.__setattr__(instance, 'event_bus', SimpleNamespace(dispatch=Mock()))
    monkeypatch.setattr(bounded, 'TimeoutWrappedCDPClient', lambda *a, **k: client)
    async def monitor(manager):
        manager._targets['t'] = bounded.Target(target_id='t', target_type='page', url=url, title='Existing')
        manager._sessions['s'] = SimpleNamespace(target_id='t', session_id='s', cdp_client=client)
        manager._target_sessions['t'] = {'s'}
    monkeypatch.setattr(bounded.BoundedSessionManager, 'start_monitoring', monitor)
    if url == 'about:blank':
        await instance.connect()
        assert instance.agent_focus_target_id == 't'
        assert sum(m == 'setDownloadBehavior' for _, m, _, _ in client.calls) == 1
    else:
        with pytest.raises(ValueError):
            await instance.connect()
        client.stop.assert_awaited_once()
    assert not any(m in ('navigate', 'createTarget', 'handleJavaScriptDialog', 'activateTarget') for _, m, _, _ in client.calls)


async def test_connect_empty_browser_fails_without_creating_page(monkeypatch):
    instance = bounded.make_session(cdp_url='ws://owned:9222/devtools/browser/id', allowed_domains=['https://example.com'])
    client = SimpleNamespace(calls=[], start=AsyncMock(), stop=AsyncMock())
    client.send = Commands(client)
    object.__setattr__(instance, 'event_bus', SimpleNamespace(dispatch=Mock()))
    monkeypatch.setattr(bounded, 'TimeoutWrappedCDPClient', lambda *a, **k: client)
    monkeypatch.setattr(bounded.BoundedSessionManager, 'start_monitoring', AsyncMock())
    with pytest.raises(RuntimeError, match='already have a page'):
        await instance.connect()
    assert not any(m == 'createTarget' for _, m, _, _ in client.calls)


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
