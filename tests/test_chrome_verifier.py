"""Verifier boundary tests do not launch a browser or Docker."""
from copy import deepcopy
import importlib.util
import json
from pathlib import Path
import subprocess
import time

import httpx
import pytest

from decision_browser.policy import Policy, PolicyError

spec = importlib.util.spec_from_file_location('chrome_verifier', Path(__file__).parents[1] / 'verification/chrome/run.py')
verifier = importlib.util.module_from_spec(spec)
spec.loader.exec_module(verifier)


async def test_live_evidence_uses_production_contract_caps_http_and_omits_credentials(tmp_path):
    from test_azure import azure_request, azure_response
    requests = []

    def handler(request):
        requests.append(request)
        assert request.headers['authorization'] == 'Bearer test-secret-token'
        return httpx.Response(200, json=azure_response(), headers={'apim-request-id': 'receipt-id'})

    async with verifier.EvidenceAzureProvider('https://resource.cognitiveservices.azure.com',
            'decision-1', 'test-secret-token', tmp_path, transport=httpx.MockTransport(handler)) as provider:
        for _ in range(12):
            answer = await provider.decide(azure_request(), time.monotonic() + 1)
            assert answer.resolved_model == 'microsoft-decision-1'
        with pytest.raises(verifier.ProviderError):
            await provider.decide(azure_request(), time.monotonic() + 1)
        assert provider.http_requests == len(requests) == 12
        records = json.loads((tmp_path / 'azure-calls.json').read_text())
        assert records[0]['http'] == [{'status': 200, 'request_id': 'receipt-id'}]
        assert records[0]['wire_response']['model'] == 'microsoft-decision-1'
        assert records[0]['wire_request']['body']['model'] == 'decision-1'
        assert 'validated_answer' in records[0] and 'validated_answer' not in records[-1]
        assert 'test-secret-token' not in (tmp_path / 'azure-calls.json').read_text()


@pytest.mark.parametrize('status', [429, 503])
async def test_live_evidence_retains_failure_without_retry_or_validated_answer(tmp_path, status):
    from test_azure import azure_request
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(status, json={'error': 'unavailable'})

    async with verifier.EvidenceAzureProvider('https://resource.cognitiveservices.azure.com',
            'decision-1', 'test-secret-token', tmp_path, transport=httpx.MockTransport(handler)) as provider:
        with pytest.raises(verifier.ProviderError):
            await provider.decide(azure_request(), time.monotonic() + 1)
    records = json.loads((tmp_path / 'azure-calls.json').read_text())
    assert len(requests) == 1
    assert records[0]['http'][0]['status'] == status
    assert records[0]['error_type'] == 'ProviderError' and 'validated_answer' not in records[0]


@pytest.mark.parametrize('diagnostic_error', [subprocess.TimeoutExpired('docker logs', 60), OSError('log unavailable')])
@pytest.mark.parametrize('discover', [False, True])
def test_cleanup_removes_exact_owned_container_after_log_failure(tmp_path, monkeypatch, diagnostic_error, discover):
    container = 'a' * 64
    calls = []

    def fake_docker(*args, **kwargs):
        calls.append(args)
        if args[0] == 'inspect':
            return subprocess.CompletedProcess(args, 0, json.dumps([{'Id': container, 'Name': '/owned',
                'Config': {'Labels': {verifier.OWNER: 'run'}}}]), '')
        if args[0] == 'logs':
            raise diagnostic_error
        return subprocess.CompletedProcess(args, 0, '', '')

    monkeypatch.setattr(verifier, 'docker', fake_docker)
    verifier.cleanup(tmp_path, 'owned', 'run', None if discover else container)
    assert ('rm', '-f', container) in calls
    assert ('ps', '-a', '--no-trunc', '--filter', 'id=' + container, '--format', '{{.ID}}') in calls
    receipt = json.loads((tmp_path / 'cleanup.json').read_text())
    assert receipt['removed'] and receipt['absent'] and receipt['evidence_retained']
    assert receipt['diagnostic_error'] == repr(diagnostic_error)


def test_cleanup_does_not_remove_unowned_discovered_container(tmp_path, monkeypatch):
    calls = []

    def fake_docker(*args, **kwargs):
        calls.append(args)
        return subprocess.CompletedProcess(args, 0, json.dumps([{'Id': 'b' * 64, 'Name': '/owned',
            'Config': {'Labels': {verifier.OWNER: 'someone-else'}}}]), '')

    monkeypatch.setattr(verifier, 'docker', fake_docker)
    with pytest.raises(AssertionError):
        verifier.cleanup(tmp_path, 'owned', 'run', None)
    assert all(args[0] != 'rm' for args in calls)
    receipt = json.loads((tmp_path / 'cleanup.json').read_text())
    assert not receipt['removed'] and 'cleanup_error' in receipt


def test_attestation_rejects_each_broken_isolation_boundary():
    info = {'Name': '/owned', 'Image': 'sha256:expected', 'Config': {'Labels': {verifier.OWNER: 'run'},
        'Cmd': ['python', '/opt/verification/container.py']}, 'State': {'Running': True},
        'HostConfig': {'NetworkMode': 'none', 'PidMode': '', 'IpcMode': 'private', 'ReadonlyRootfs': True, 'Privileged': False,
            'CapDrop': ['ALL'], 'SecurityOpt': ['no-new-privileges'], 'Tmpfs': {'/tmp': 'rw'}},
        'NetworkSettings': {'Ports': {}, 'Networks': {'none': {}}}, 'Mounts': []}
    verifier.attest(info, 'owned', 'run', 'sha256:expected')
    for section, key, value in [
        ('HostConfig', 'NetworkMode', 'bridge'),
        ('HostConfig', 'PidMode', 'host'), ('HostConfig', 'IpcMode', 'host'),
        ('HostConfig', 'CapAdd', ['SYS_ADMIN']), ('HostConfig', 'Devices', ['/dev/disk']),
        ('HostConfig', 'DeviceRequests', [{}]), ('HostConfig', 'VolumesFrom', ['host']),
        ('HostConfig', 'Binds', ['/Users:/host']),
        ('NetworkSettings', 'Networks', {'none': {}, 'bridge': {}}), ('HostConfig', 'ReadonlyRootfs', False),
        ('HostConfig', 'Privileged', True), ('HostConfig', 'CapDrop', []),
        ('HostConfig', 'SecurityOpt', []), ('HostConfig', 'Tmpfs', {'/host': 'rw'}),
        ('Config', 'Labels', {}), ('Config', 'Cmd', ['sh']), ('State', 'Running', False),
        ('NetworkSettings', 'Ports', {'9222/tcp': [{'HostPort': '9222'}]}),
    ]:
        broken = deepcopy(info)
        broken[section][key] = value
        with pytest.raises(AssertionError):
            verifier.attest(broken, 'owned', 'run', 'sha256:expected')
    for key, value in [('Mounts', [{'Source': '/Users', 'Destination': '/host'}]), ('Name', '/unowned'), ('Image', 'sha256:substituted')]:
        broken = deepcopy(info)
        broken[key] = value
        with pytest.raises(AssertionError):
            verifier.attest(broken, 'owned', 'run', 'sha256:expected')


def test_fixture_policy_only_adapts_exact_origin_and_inherits_authorization():
    policy = verifier.FixturePolicy()
    assert verifier.FixturePolicy.authorize is Policy.authorize
    policy.check_url(verifier.ORIGIN)
    for url in ['http://localhost:8765', verifier.ORIGIN + '/', 'http://127.0.0.1:1',
                'https://fixture.test', 'http://169.254.169.254', 'https://example.com']:
        with pytest.raises(PolicyError, match='UNAPPROVED_DESTINATION'):
            policy.check_url(url)


async def test_setup_waits_for_exact_new_fixture_document(tmp_path):
    class SetupTransport:
        def __init__(self):
            self.responses = iter([
                {'url': verifier.ORIGIN + '/?variant=normal', 'epoch': 1, 'ready': 'complete', 'kind': 'search', 'facts': {'searched': False, 'page_index': 0}},
                {'url': verifier.ORIGIN + '/?variant=normal', 'epoch': 1, 'ready': 'complete', 'kind': 'search', 'facts': {'searched': False, 'page_index': 0}},
                {'url': verifier.ORIGIN + '/?variant=normal', 'epoch': 2, 'ready': 'complete', 'kind': 'search', 'facts': {'searched': False, 'page_index': 0}},
            ])
            self.calls = []

        async def call(self, name, arguments):
            self.calls.append((name, arguments))
            return next(self.responses) if name == 'browser_js' else None

    transport = SetupTransport()
    await verifier.prepare_fixture(transport, 'owned-tab', 'normal', tmp_path)
    import json
    evidence = json.loads((tmp_path / 'setup.json').read_text())
    assert [row['epoch'] for row in evidence['observations']] == [1, 2]
    assert transport.calls[1] == ('browser_goto', {'url': verifier.ORIGIN + '/?variant=normal'})
