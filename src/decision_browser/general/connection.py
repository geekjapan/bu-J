from __future__ import annotations

import asyncio
import ipaddress
import json
import os
import unicodedata
from urllib.parse import urlsplit
from uuid import UUID

import httpx

SETUP_HINT = 'Enable remote debugging in your running Chrome and approve its Allow remote debugging prompt.'
MAX_DISCOVERY_BYTES = 64 * 1024


def _validate_endpoint(value: str, schemes: tuple[str, ...]) -> str:
    if not isinstance(value, str) or any(char.isspace() or unicodedata.category(char).startswith('C') for char in value):
        raise ValueError('CDP endpoint must not contain whitespace or control characters')
    try:
        parsed = urlsplit(value)
        valid_port = parsed.port
    except ValueError as exc:
        raise ValueError('Invalid CDP endpoint') from exc
    if (parsed.scheme not in schemes or not parsed.hostname or parsed.username is not None
            or parsed.password is not None or parsed.fragment or valid_port == 0):
        raise ValueError('CDP endpoint must be an HTTP(S) or WebSocket URL without credentials or fragments')
    try:
        loopback = ipaddress.ip_address(parsed.hostname).is_loopback
    except ValueError:
        loopback = parsed.hostname.lower() == 'localhost'
    if parsed.scheme in ('http', 'ws') and not loopback:
        raise ValueError('Cleartext CDP endpoints must use localhost or a loopback IP; remote endpoints require TLS')
    return value


def browser_identity(endpoint: str) -> str:
    parsed = urlsplit(endpoint)
    prefix = '/devtools/browser/'
    if parsed.path.startswith(prefix):
        try:
            return 'browser:' + UUID(parsed.path[len(prefix):]).hex
        except ValueError:
            pass
    host = parsed.hostname.lower()
    try:
        loopback = ipaddress.ip_address(host).is_loopback
    except ValueError:
        loopback = host == 'localhost'
    return json.dumps((parsed.scheme, 'loopback' if loopback else host,
                       parsed.port or (443 if parsed.scheme == 'wss' else 80), parsed.path, parsed.query))


def resolve_endpoint(value: str | None = None) -> str:
    value = value or os.environ.get('BU_CDP_WS') or os.environ.get('BU_CDP_URL')
    if value is None:
        try:
            from browser_harness.daemon import get_ws_url
            value = get_ws_url()
        except Exception as exc:
            raise RuntimeError(f'Could not discover the running Chrome. {SETUP_HINT}') from exc
    _validate_endpoint(value, ('ws', 'wss', 'http', 'https'))
    parsed = urlsplit(value)
    if parsed.scheme in ('ws', 'wss'):
        return value
    if parsed.query or parsed.path not in ('', '/', '/json/version'):
        raise ValueError('HTTP CDP endpoint must be the browser origin or /json/version')
    discovery = value.rstrip('/') if parsed.path == '/json/version' else value.rstrip('/') + '/json/version'
    try:
        with httpx.Client(trust_env=False, follow_redirects=False, timeout=5) as client:
            with client.stream('GET', discovery) as response:
                response.raise_for_status()
                body = bytearray()
                for chunk in response.iter_bytes(chunk_size=4096):
                    body.extend(chunk)
                    if len(body) > MAX_DISCOVERY_BYTES:
                        raise ValueError('CDP discovery response exceeds 64 KiB')
        endpoint = json.loads(body)['webSocketDebuggerUrl']
        return _validate_endpoint(endpoint, ('ws', 'wss'))
    except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
        raise RuntimeError(f'Could not resolve the Chrome CDP endpoint. {SETUP_HINT}') from exc


async def list_tabs(endpoint: str) -> list[dict]:
    from cdp_use import CDPClient

    _validate_endpoint(endpoint, ('ws', 'wss'))
    client = CDPClient(endpoint, max_ws_frame_size=MAX_DISCOVERY_BYTES * 16)
    try:
        async with asyncio.timeout(10):
            await client.start()
            result = await client.send.Target.getTargets()
        return [dict(id=target['targetId'], url=target['url'], title=target['title'])
                for target in result['targetInfos'] if target['type'] == 'page']
    finally:
        await client.stop()
