import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from decision_browser.general import connection

WS = 'ws://127.0.0.1:9222/devtools/browser/full-browser-id'


@pytest.fixture(autouse=True)
def clear_endpoint_environment(monkeypatch):
    monkeypatch.delenv('BU_CDP_WS', raising=False)
    monkeypatch.delenv('BU_CDP_URL', raising=False)


@pytest.mark.parametrize('endpoint', [WS, 'ws://[::1]:9222/devtools/browser/id', 'wss://remote.example/devtools/browser/id'])
def test_direct_websocket_is_unchanged_without_http(endpoint, monkeypatch):
    monkeypatch.setattr(connection.httpx, 'Client', lambda **kwargs: pytest.fail('HTTP used for WS endpoint'))
    assert connection.resolve_endpoint(endpoint) == endpoint


@pytest.mark.parametrize('endpoint', ['ws://remote.example/id', 'http://192.168.1.1:9222',
                                      'ws://user@localhost/id', 'wss://user:password@remote.example/id',
                                      'ws://localhost/id\n', 'ws://localhost/id\x85', 'ws://localhost/id#fragment',
                                      'file:///tmp/port', 'http://localhost:bad', 'ws://localhost:0/id'])
def test_invalid_endpoint_is_rejected(endpoint):
    with pytest.raises(ValueError):
        connection.resolve_endpoint(endpoint)


def install_http(monkeypatch, handler):
    real_client = httpx.Client

    def client(**kwargs):
        assert kwargs == {'trust_env': False, 'follow_redirects': False, 'timeout': 5}
        return real_client(transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr(connection.httpx, 'Client', client)


def test_http_resolves_unchanged_advertised_websocket(monkeypatch):
    advertised = 'ws://localhost:9333/devtools/browser/unchanged'

    def handler(request):
        assert str(request.url) == 'http://127.0.0.1:9222/json/version'
        return httpx.Response(200, json={'webSocketDebuggerUrl': advertised})

    install_http(monkeypatch, handler)
    assert connection.resolve_endpoint('http://127.0.0.1:9222/') == advertised


@pytest.mark.parametrize('response', [
    httpx.Response(302, headers={'location': 'http://remote.example/'}),
    httpx.Response(403), httpx.Response(200, content=b'not json'), httpx.Response(200, json={}),
    httpx.Response(200, json={'webSocketDebuggerUrl': 'ws://remote.example/private'}),
    httpx.Response(200, content=b'x' * (connection.MAX_DISCOVERY_BYTES + 1)),
])
def test_http_failure_has_normal_chrome_setup_hint(monkeypatch, response):
    install_http(monkeypatch, lambda request: response)
    with pytest.raises(RuntimeError, match='Enable remote debugging') as error:
        connection.resolve_endpoint('http://localhost:9222')
    assert 'approve' in str(error.value)
    assert 'Docker' not in str(error.value)
    assert 'profile' not in str(error.value)


def test_explicit_and_environment_precedence(monkeypatch):
    monkeypatch.setenv('BU_CDP_WS', WS)
    monkeypatch.setenv('BU_CDP_URL', 'ws://localhost:9333/second')
    assert connection.resolve_endpoint() == WS
    assert connection.resolve_endpoint('ws://localhost:9444/explicit') == 'ws://localhost:9444/explicit'
    monkeypatch.delenv('BU_CDP_WS')
    assert connection.resolve_endpoint() == 'ws://localhost:9333/second'


def test_discovery_only_calls_get_ws_url(monkeypatch):
    called = []
    monkeypatch.setitem(sys.modules, 'browser_harness.daemon', SimpleNamespace(
        get_ws_url=lambda: called.append('discover') or WS,
        ensure_daemon=lambda: pytest.fail('must never launch a daemon'),
    ))
    assert connection.resolve_endpoint() == WS
    assert called == ['discover']


def test_discovery_failure_does_not_repeat_dependency_isolation_advice(monkeypatch):
    def fail():
        raise RuntimeError('launch Docker using a dedicated profile')
    monkeypatch.setitem(sys.modules, 'browser_harness.daemon', SimpleNamespace(get_ws_url=fail))
    with pytest.raises(RuntimeError, match='running Chrome') as error:
        connection.resolve_endpoint()
    assert 'Docker' not in str(error.value)
    assert 'dedicated' not in str(error.value)


async def test_list_tabs_uses_root_metadata_only_and_full_ids(monkeypatch):
    target = SimpleNamespace(getTargets=AsyncMock(return_value={'targetInfos': [
        {'targetId': 'full-page-target-id', 'type': 'page', 'url': 'https://example.test', 'title': 'Page'},
        {'targetId': 'worker-id', 'type': 'worker', 'url': 'https://example.test/worker', 'title': 'Worker'},
    ]}))
    client = SimpleNamespace(start=AsyncMock(), stop=AsyncMock(), send=SimpleNamespace(Target=target))
    monkeypatch.setitem(sys.modules, 'cdp_use', SimpleNamespace(CDPClient=lambda *args, **kwargs: client))
    assert await connection.list_tabs(WS) == [{'id': 'full-page-target-id', 'url': 'https://example.test', 'title': 'Page'}]
    target.getTargets.assert_awaited_once_with()
    client.start.assert_awaited_once()
    client.stop.assert_awaited_once()


async def test_list_tabs_disconnects_after_failed_query(monkeypatch):
    client = SimpleNamespace(start=AsyncMock(), stop=AsyncMock(), send=SimpleNamespace(
        Target=SimpleNamespace(getTargets=AsyncMock(side_effect=RuntimeError('gone')))))
    monkeypatch.setitem(sys.modules, 'cdp_use', SimpleNamespace(CDPClient=lambda *args, **kwargs: client))
    with pytest.raises(RuntimeError, match='gone'):
        await connection.list_tabs(WS)
    client.stop.assert_awaited_once()


@pytest.mark.parametrize('second', [
    'ws://127.0.0.1:9222/devtools/browser/194807a1-5fc2-46d6-ae43-9f9ce78d28d4',
    'ws://[::1]:9222/devtools/browser/194807a1-5fc2-46d6-ae43-9f9ce78d28d4',
    'wss://forward.example/devtools/browser/194807a1-5fc2-46d6-ae43-9f9ce78d28d4',
])
def test_same_browser_has_one_lease_identity_across_endpoint_aliases(second):
    first = 'ws://localhost:9222/devtools/browser/194807a1-5fc2-46d6-ae43-9f9ce78d28d4'
    assert connection.browser_identity(first) == connection.browser_identity(second)
    assert connection.browser_identity(first) != connection.browser_identity(first.replace('194807a1', '294807a1'))


def test_non_uuid_endpoint_loopback_aliases_share_one_lease_identity():
    assert connection.browser_identity(WS) == connection.browser_identity(WS.replace('127.0.0.1', 'localhost'))
    assert connection.browser_identity(WS) != connection.browser_identity(WS.replace(':9222', ':9333'))
