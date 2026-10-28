import asyncio
import json
import time

import httpx
import pytest

from decision_browser.contracts import Candidate, DecisionRequest
from decision_browser.providers.jev import JevProvider, ProviderError, parse_response


def request():
    return DecisionRequest(task_id='t', goal='synthetic', phase='collect', page_kind='results',
                           page_index=1, has_next=True, candidates=(Candidate(candidate_id='next', description='Next page'),
                           Candidate(candidate_id='abstain', description='Ambiguous state')),
                           requested_model='jev-1.14.0', request_digest='digest')


def response():
    return {'model': 'jev-1.14.0', 'answers': {
        'next_action': {'type': 'choice', 'choice': 'next', 'confidence': 0.7,
                        'probabilities': {'next': 0.98, 'abstain': 0.02}},
        'state_supported': {'type': 'noul', 'noul': 0.99}},
        'usage': {'input_tokens': 20, 'output_tokens': 3}}


def test_official_response_contract():
    result = parse_response(json.dumps(response()).encode(), request(), 10.0)
    assert result.choice == 'next'
    assert result.provider_confidence == 0.7
    assert result.state_supported == 0.99
    assert result.usage == 23


@pytest.mark.parametrize('mutation', ['extra', 'nan', 'tie', 'unknown', 'model', 'sum', 'bool', 'question'])
def test_invalid_responses(mutation):
    data = response()
    answer = data['answers']['next_action']
    if mutation == 'extra':
        data['execute'] = 'evil'
    elif mutation == 'nan':
        answer['probabilities']['next'] = float('nan')
    elif mutation == 'tie':
        answer['probabilities'] = {'next': 0.5, 'abstain': 0.5}
    elif mutation == 'unknown':
        answer['choice'] = 'delete'
    elif mutation == 'model':
        data['model'] = 'jev-latest'
    elif mutation == 'sum':
        answer['probabilities']['next'] = 0.9
    elif mutation == 'bool':
        data['answers']['state_supported']['noul'] = True
    else:
        data['answers']['other'] = {'type': 'noul', 'noul': 1.0}
    with pytest.raises((ValueError, ProviderError)):
        parse_response(json.dumps(data).encode(), request(), 1.0)


async def test_reused_transport_and_retry():
    calls = []
    async def handler(req):
        calls.append(req)
        assert req.url.host == 'api.typesafe.ai'
        assert req.headers['Authorization'] == 'Bearer test-only'
        if len(calls) == 1:
            return httpx.Response(429, headers={'Retry-After': '0'})
        return httpx.Response(200, json=response())
    async with JevProvider('test-only', transport=httpx.MockTransport(handler)) as provider:
        assert (await provider.decide(request(), time.monotonic() + 1)).choice == 'next'
    assert len(calls) == 2
    payload = json.loads(calls[-1].content)
    assert set(payload) == {'model', 'state', 'questions'}
    assert 'goal' not in payload['state']


@pytest.mark.parametrize('status', [401, 403, 302])
async def test_no_auth_retry_or_redirect(status):
    calls = []
    async def handler(req):
        calls.append(req)
        return httpx.Response(status, headers={'Location': 'https://evil.test'})
    async with JevProvider('test-only', transport=httpx.MockTransport(handler)) as provider:
        with pytest.raises(ProviderError):
            await provider.decide(request(), time.monotonic() + 1)
    assert len(calls) == 1


async def test_retry_after_deadline_and_response_limit():
    for status, headers, content in [(429, {'Retry-After': '10'}, b''), (200, {}, b'x' * 65537)]:
        async with JevProvider('test-only', transport=httpx.MockTransport(
            lambda req: httpx.Response(status, headers=headers, content=content))) as provider:
            with pytest.raises(ProviderError):
                await provider.decide(request(), time.monotonic() + 0.1)


async def test_breaker_stops_after_five_and_cancel_propagates():
    calls = []
    async def handler(req):
        calls.append(req)
        return httpx.Response(401)
    async with JevProvider('test-only', transport=httpx.MockTransport(handler)) as provider:
        for _ in range(6):
            with pytest.raises(ProviderError):
                await provider.decide(request(), time.monotonic() + 1)
    assert len(calls) == 5
    async def cancelled(req):
        raise asyncio.CancelledError
    async with JevProvider('test-only', transport=httpx.MockTransport(cancelled)) as provider:
        with pytest.raises(asyncio.CancelledError):
            await provider.decide(request(), time.monotonic() + 1)


async def test_deadline_includes_waiting_for_provider_lock():
    entered = asyncio.Event()
    release = asyncio.Event()
    async def blocked(req):
        entered.set()
        await release.wait()
        return httpx.Response(200, json=response())
    async with JevProvider('test-only', transport=httpx.MockTransport(blocked)) as provider:
        first = asyncio.create_task(provider.decide(request(), time.monotonic() + 2))
        await entered.wait()
        try:
            with pytest.raises(TimeoutError):
                await provider.decide(request(), time.monotonic() + 0.01)
            assert not first.done()
        finally:
            release.set()
            await first
