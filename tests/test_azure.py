import asyncio
import json
import subprocess
import time

import httpx
import pytest

from decision_browser import cli
from decision_browser.audit import Journal
from decision_browser.contracts import Budget, TaskSpec
from decision_browser.controller import Controller
from decision_browser.drivers.mock import DEFAULT_RECORDS, MockBrowserDriver
from decision_browser.providers.azure import AzureDecisionProvider
from decision_browser.providers.systemone import ProviderError
from test_jev import request, response


ENDPOINT = 'https://resource.cognitiveservices.azure.com'


def azure_request():
    return request().model_copy(update={'requested_model': AzureDecisionProvider.MODEL})


def azure_response():
    return {**response(), 'model': AzureDecisionProvider.MODEL}


@pytest.mark.parametrize('auth,header', [({'api_key': 'test-key'}, ('api-key', 'test-key')),
                                       ({'entra_token': 'test-token'}, ('Authorization', 'Bearer test-token'))])
async def test_request_deployment_auth_and_canonical_model(auth, header):
    calls = []
    async def handler(req):
        calls.append(req)
        assert str(req.url) == ENDPOINT + '/providers/microsoft/v1/systemone'
        assert req.headers[header[0]] == header[1]
        assert ('api-key' in req.headers) != ('authorization' in req.headers)
        body = json.loads(req.content)
        assert body['model'] == 'reviewed-deployment'
        assert set(body) == {'model', 'state', 'questions'}
        assert 'goal' not in body['state']
        return httpx.Response(200, json=azure_response())
    async with AzureDecisionProvider(ENDPOINT, 'reviewed-deployment', **auth,
                                    transport=httpx.MockTransport(handler)) as provider:
        result = await provider.decide(azure_request(), time.monotonic() + 1)
        assert result.provider == 'azure'
        assert result.requested_model == result.resolved_model == AzureDecisionProvider.MODEL
        assert result.usage == 23
    assert provider.client.is_closed
    assert len(calls) == 1


@pytest.mark.parametrize('endpoint', [
    'http://resource.cognitiveservices.azure.com', 'https://localhost',
    'https://169.254.169.254', 'https://resource.cognitiveservices.azure.com.evil.test',
    'https://a.b.cognitiveservices.azure.com', 'https://resource.services.ai.azure.com',
    'https://user:secret@resource.cognitiveservices.azure.com',
    ENDPOINT + ':8443', ENDPOINT + '/providers/microsoft/v1/systemone',
    ENDPOINT + '?api-version=1', ENDPOINT + '#fragment', ENDPOINT + '?', ENDPOINT + '#',
    'https://résource.cognitiveservices.azure.com', 'https://-bad.cognitiveservices.azure.com',
    'https://bad-.cognitiveservices.azure.com', ENDPOINT + '\n',
])
def test_unapproved_endpoint_rejected(endpoint):
    with pytest.raises(ValueError):
        AzureDecisionProvider(endpoint, api_key='test')


@pytest.mark.parametrize('auth', [{}, {'api_key': 'a', 'entra_token': 'b'}, {'api_key': ''},
                                 {'api_key': 'key\nsecret'}, {'entra_token': 'token\rsecret'},
                                 {'api_key': 'a\x00b'}, {'entra_token': 'token secret'}])
def test_auth_requires_exactly_one_valid_secret(auth):
    with pytest.raises(ValueError):
        AzureDecisionProvider(ENDPOINT, **auth)


async def test_deployment_name_is_not_an_approved_response_model():
    data = azure_response()
    data['model'] = 'decision-1'
    async with AzureDecisionProvider(ENDPOINT, api_key='secret', transport=httpx.MockTransport(
        lambda req: httpx.Response(200, json=data))) as provider:
        with pytest.raises(ProviderError):
            await provider.decide(azure_request(), time.monotonic() + 1)
        with pytest.raises(ProviderError):
            await provider.decide(request(), time.monotonic() + 1)


@pytest.mark.parametrize('status', [401, 403, 302, 307])
async def test_auth_and_redirect_fail_without_retry(status):
    calls = []
    def handler(req):
        calls.append(req)
        return httpx.Response(status, headers={'Location': 'https://evil.test'})
    async with AzureDecisionProvider(ENDPOINT, api_key='secret', transport=httpx.MockTransport(handler)) as provider:
        with pytest.raises(ProviderError) as error:
            await provider.decide(azure_request(), time.monotonic() + 1)
    assert len(calls) == 1
    assert 'secret' not in str(error.value)


async def test_retry_timeout_and_size_budgets():
    calls = []
    def retry(req):
        calls.append(req)
        return httpx.Response(429, headers={'Retry-After': '0'}) if len(calls) == 1 else httpx.Response(200, json=azure_response())
    async with AzureDecisionProvider(ENDPOINT, api_key='secret', transport=httpx.MockTransport(retry)) as provider:
        assert (await provider.decide(azure_request(), time.monotonic() + 1)).choice == 'next'
    assert len(calls) == 2
    async def timeout(req):
        raise httpx.ReadTimeout('secret server details')
    for handler in (timeout, lambda req: httpx.Response(200, content=b'x' * 65537),
                    lambda req: httpx.Response(429, headers={'Retry-After': '10'})):
        async with AzureDecisionProvider(ENDPOINT, api_key='secret', transport=httpx.MockTransport(handler)) as provider:
            with pytest.raises(ProviderError) as error:
                await provider.decide(azure_request(), time.monotonic() + 0.1)
            assert 'secret' not in str(error.value)


async def test_controller_azure_fake_completes_fixture():
    calls = []
    def handler(req):
        body = json.loads(req.content)
        criteria = body['questions']['next_action']['criteria']
        choice = next(c for c in criteria if c != 'abstain')
        data = azure_response()
        data['answers']['next_action']['choice'] = choice
        data['answers']['next_action']['probabilities'] = {c: 0.99 if c == choice else 0.01 for c in criteria}
        calls.append(req)
        return httpx.Response(200, json=data)
    journal = Journal()
    async with AzureDecisionProvider(ENDPOINT, api_key='test', transport=httpx.MockTransport(handler)) as provider:
        result = await Controller(MockBrowserDriver(), provider, journal=journal, mode='enforced', routing='model',
                                  model=provider.MODEL).run(TaskSpec(task_id='azure', budget=Budget(max_cost_units=1000)))
    assert result.state == 'SUCCEEDED'
    assert result.records == DEFAULT_RECORDS
    assert result.model_calls == len(calls) == 6
    payloads = [json.loads(row[0]) for row in journal.db.execute("SELECT payload FROM events WHERE event_type='decision'")]
    assert all(p['requested_model'] == p['resolved_model'] == AzureDecisionProvider.MODEL for p in payloads)
    journal.close()


def cli_args(monkeypatch, tmp_path, *args):
    monkeypatch.setattr('sys.argv', ['decision-browser', '--journal', str(tmp_path / 'journal.sqlite3'), *args])


def test_cli_mock_and_explicit_rules_need_no_azure_credentials(monkeypatch, tmp_path, capsys):
    def forbidden(*args):
        raise AssertionError('Azure authentication must not run')
    monkeypatch.setattr(cli, 'azure_provider', forbidden)
    cli_args(monkeypatch, tmp_path)
    assert cli.main() == 0
    cli_args(monkeypatch, tmp_path, '--provider', 'azure', '--routing', 'rules', '--mode', 'enforced')
    assert cli.main() == 0
    assert 'SUCCEEDED' in capsys.readouterr().out


@pytest.mark.parametrize('broken', [False, True])
def test_cli_selects_azure_default_model_routing_and_closes(monkeypatch, tmp_path, capsys, broken):
    providers = []
    class FakeAzure(AzureDecisionProvider):
        def __init__(self, endpoint, deployment, **auth):
            def handler(req):
                if broken:
                    return httpx.Response(401, text='secret-value')
                body = json.loads(req.content)
                criteria = body['questions']['next_action']['criteria']
                choice = next(c for c in criteria if c != 'abstain')
                data = azure_response()
                data['answers']['next_action'].update(choice=choice, probabilities={c: 0.99 if c == choice else 0.01 for c in criteria})
                return httpx.Response(200, json=data)
            super().__init__(endpoint, deployment, **auth, transport=httpx.MockTransport(handler))
            providers.append(self)
    monkeypatch.setattr(cli, 'AzureDecisionProvider', FakeAzure)
    monkeypatch.setenv('AZURE_ENDPOINT', ENDPOINT)
    monkeypatch.setenv('AZURE_API_KEY', 'secret-value')
    cli_args(monkeypatch, tmp_path, '--provider', 'azure', '--azure-auth', 'api-key', '--mode', 'enforced', '--max-cost-units', '1000')
    assert cli.main() == (1 if broken else 0)
    assert len(providers) == 1 and providers[0].client.is_closed
    output = capsys.readouterr()
    assert 'secret-value' not in output.out + output.err
    assert '"model_calls": 1' in output.out if broken else '"model_calls": 6' in output.out


@pytest.mark.parametrize('args', [('--max-cost-units', '-1'), ('--provider', 'azure', '--azure-endpoint', 'https://evil.test')])
def test_cli_invalid_configuration_is_redacted(monkeypatch, tmp_path, capsys, args):
    monkeypatch.setenv('AZURE_API_KEY', 'secret-value')
    cli_args(monkeypatch, tmp_path, *args)
    assert cli.main() == 1
    output = capsys.readouterr()
    assert 'Invalid task or Azure configuration' in output.err
    assert 'secret-value' not in output.out + output.err


def test_azure_cli_auth_is_explicit_fixed_argv_and_failure_redacted(monkeypatch, tmp_path, capsys):
    calls = []
    def az(argv, **kwargs):
        calls.append((argv, kwargs))
        raise subprocess.CalledProcessError(1, argv, stderr='secret-value')
    monkeypatch.setattr(cli.subprocess, 'run', az)
    monkeypatch.setenv('AZURE_ENDPOINT', ENDPOINT)
    cli_args(monkeypatch, tmp_path, '--provider', 'azure', '--azure-auth', 'azure-cli')
    assert cli.main() == 1
    argv, kwargs = calls[0]
    assert argv == ['az', 'account', 'get-access-token', '--resource', 'https://cognitiveservices.azure.com/', '--query', 'accessToken', '--output', 'tsv']
    assert kwargs == {'check': True, 'capture_output': True, 'text': True, 'timeout': 10}
    assert 'secret-value' not in capsys.readouterr().err


def test_cli_default_azure_cli_auth_passes_captured_token_and_closes_provider(monkeypatch, tmp_path, capsys):
    constructed = []
    providers = []
    calls = []
    class ObservedAzure(AzureDecisionProvider):
        def __init__(self, endpoint, deployment, **auth):
            constructed.append((endpoint, deployment, auth))
            super().__init__(endpoint, deployment, **auth, transport=httpx.MockTransport(
                lambda req: httpx.Response(401)))
            providers.append(self)
    def az(argv, **kwargs):
        calls.append((argv, kwargs))
        return subprocess.CompletedProcess(argv, 0, 'test-token\n')
    monkeypatch.setattr(cli, 'AzureDecisionProvider', ObservedAzure)
    monkeypatch.setattr(cli.subprocess, 'run', az)
    monkeypatch.setenv('AZURE_ENDPOINT', ENDPOINT)
    monkeypatch.setenv('AZURE_DEPLOYMENT_NAME', 'configured-deployment')
    cli_args(monkeypatch, tmp_path, '--provider', 'azure', '--mode', 'enforced')
    assert cli.main() == 1
    assert calls == [(
        ['az', 'account', 'get-access-token', '--resource', 'https://cognitiveservices.azure.com/', '--query', 'accessToken', '--output', 'tsv'],
        {'check': True, 'capture_output': True, 'text': True, 'timeout': 10},
    )]
    assert constructed == [(ENDPOINT, 'configured-deployment', {'entra_token': 'test-token'})]
    assert len(providers) == 1 and providers[0].client.is_closed
    output = capsys.readouterr()
    assert 'test-token' not in output.out + output.err
