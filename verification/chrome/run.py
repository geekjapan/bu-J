"""Actual Chrome acceptance. Run: .venv/bin/python verification/chrome/run.py."""
from __future__ import annotations

from decision_browser._runtime import azure_cli_executable

import asyncio
import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import traceback
from uuid import uuid4

from decision_browser.audit import Journal
from decision_browser.contracts import Budget, TaskSpec
from decision_browser.controller import Controller
from decision_browser.drivers.harness_mcp import (
    HarnessError, HarnessMCPDriver, HarnessSettings, MCPStdioTransport, UPSTREAM_SOURCE_SHA256,
)
from decision_browser.executor import BrowserLane
from decision_browser.policy import Policy, PolicyError
from decision_browser.providers.mock import MockDecisionProvider
from decision_browser.providers.azure import AzureDecisionProvider, validate_endpoint
from decision_browser.providers.systemone import ProviderError

ROOT = Path(__file__).resolve().parents[2]
ORIGIN = 'http://127.0.0.1:8765'
IMAGE = 'bu-j-chrome-verifier:local'
OWNER = 'bu-j.chrome-verifier'
DISTRIBUTED_MCP_SHA256 = '8b11bc6058098329b6badc14982ffa9fb0fea5d2be24e8819fb9e73e3512fa32'
ENV = {'PATH': os.environ['PATH']}
EXPECTED = [
    {'id': 'R001', 'name': 'Synthetic Alpha', 'updated_at': '2026-10-01'},
    {'id': 'R002', 'name': 'Synthetic Beta', 'updated_at': '2026-10-02'},
    {'id': 'R003', 'name': 'Synthetic Gamma', 'updated_at': '2026-10-03'},
    {'id': 'R004', 'name': 'Synthetic Delta', 'updated_at': '2026-10-04'},
    {'id': 'R005', 'name': 'Synthetic Epsilon', 'updated_at': '2026-10-05'},
]


def save(path, value):
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + '\n', encoding='utf-8')


def docker(*args, check=True, timeout=60):
    return subprocess.run(['docker', *args], cwd=ROOT, env=ENV, text=True, capture_output=True,
                          check=check, timeout=timeout, encoding='utf-8')


def cleanup(evidence, name, run_id, container):
    receipt = {'removed': False, 'absent': False, 'evidence_retained': evidence.exists()}
    try:
        if container is None:
            inspected = json.loads(docker('inspect', name).stdout)[0]
            assert inspected['Name'] == '/' + name
            assert inspected['Config']['Labels'].get(OWNER) == run_id
            container = inspected['Id']
        # A successful run returns our immutable ID; a timed-out run requires ownership discovery above.
        assert re.fullmatch(r'[0-9a-f]{64}', container)
        receipt['container'] = container
        try:
            logs = docker('logs', container, check=False)
            (evidence / 'container.log').write_text(logs.stdout + logs.stderr, encoding='utf-8')
        except (subprocess.SubprocessError, OSError) as error:
            receipt['diagnostic_error'] = repr(error)
        finally:
            removed = docker('rm', '-f', container, check=False)
            receipt['removed'] = removed.returncode == 0
            remaining = docker('ps', '-a', '--no-trunc', '--filter', 'id=' + container, '--format', '{{.ID}}')
            receipt['absent'] = container not in remaining.stdout.splitlines()
    except BaseException as error:
        receipt['cleanup_error'] = repr(error)
        raise
    finally:
        save(evidence / 'cleanup.json', receipt)
    assert receipt['removed'] and receipt['absent'], 'OWNED_CONTAINER_CLEANUP_FAILED'


def attest(info, name, run_id, image_id):
    """Only the exact newly created, unmounted, offline disposable container qualifies."""
    host, config = info['HostConfig'], info['Config']
    assert info['Image'] == image_id
    assert info['Name'] == '/' + name
    assert config['Labels'].get(OWNER) == run_id
    assert host['NetworkMode'] == 'none' and not info['NetworkSettings']['Ports']
    assert host['ReadonlyRootfs'] and not info['Mounts']
    assert host['PidMode'] != 'host' and host['IpcMode'] != 'host'
    assert not any(host.get(key) for key in ('CapAdd', 'Devices', 'DeviceRequests', 'VolumesFrom', 'Binds'))
    assert set(info['NetworkSettings']['Networks']) == {'none'}
    assert not host['Privileged'] and host['CapDrop'] == ['ALL']
    assert host['SecurityOpt'] == ['no-new-privileges']
    assert set(host['Tmpfs']) == {'/tmp'}
    assert config['Cmd'] == ['python', '/opt/verification/container.py']
    assert info['State']['Running']


class FixturePolicy(Policy):
    # This exact fixture origin is a verifier adaptation; authorize remains inherited.
    origins = (ORIGIN,)

    def check_url(self, url):
        if url != ORIGIN:
            raise PolicyError('UNAPPROVED_DESTINATION')


class EvidenceTransport(MCPStdioTransport):
    def __init__(self, command, evidence):
        super().__init__(command, env=ENV, timeout=60)
        self.evidence = evidence
        self.calls = []

    async def request(self, method, params):
        result = await super().request(method, params)
        if method in ('initialize', 'tools/list'):
            save(self.evidence / (method.replace('/', '-') + '.json'), result)
        entry = {'method': method, 'params': params}
        if params.get('arguments', {}).get('method') != 'Page.captureScreenshot':
            entry['result'] = result
        self.calls.append(entry)
        return result


class EvidenceAzureProvider(AzureDecisionProvider):
    def __init__(self, endpoint, deployment, token, evidence, *, transport=None):
        super().__init__(endpoint, deployment, entra_token=token, max_retries=0, transport=transport)
        self.evidence, self.records, self.http_requests = evidence, [], 0
        self.client.event_hooks['response'] = [self.record_status]

    async def record_status(self, response):
        self.records[-1]['http'].append({'status': response.status_code,
            'request_id': response.headers.get('apim-request-id') or response.headers.get('x-ms-request-id')})

    async def _request(self, method, path, body, deadline):
        if self.http_requests >= 12:
            raise ProviderError('LIVE_HTTP_REQUEST_LIMIT')
        self.http_requests += 1
        self.records[-1]['wire_request'] = {'method': method, 'path': path, 'body': json.loads(body)}
        raw = await super()._request(method, path, body, deadline)
        self.records[-1]['response_sha256'] = hashlib.sha256(raw).hexdigest()
        self.records[-1]['wire_response'] = json.loads(raw)
        return raw

    async def decide(self, request, deadline):
        record = {'request': request.model_dump(mode='json'), 'http': []}
        self.records.append(record)
        try:
            answer = await super().decide(request, deadline)
            record['validated_answer'] = answer.model_dump(mode='json')
            return answer
        except BaseException as error:
            record['error_type'] = type(error).__name__
            raise
        finally:
            save(self.evidence / 'azure-calls.json', self.records)


def live_azure_provider(evidence):
    endpoint = validate_endpoint(os.environ.get('AZURE_ENDPOINT', ''))
    token = subprocess.run([azure_cli_executable(), 'account', 'get-access-token', '--resource',
        'https://cognitiveservices.azure.com/', '--query', 'accessToken', '--output', 'tsv'],
        capture_output=True, text=True, check=True, timeout=10, encoding='utf-8').stdout.strip()
    return EvidenceAzureProvider(endpoint, os.environ.get('AZURE_DEPLOYMENT_NAME', 'decision-1'), token, evidence)


async def prepare_fixture(transport, tab_id, variant, directory):
    """Trusted test setup only; no waits are inserted into the product action path."""
    expression = """(() => ({url:location.href,epoch:performance.timeOrigin,ready:document.readyState,
      kind:document.querySelector('[data-fixture]')?.dataset.kind,
      facts:JSON.parse(document.querySelector('#fixture-facts')?.textContent || 'null')}))()"""
    before = await transport.call('browser_js', {'expression': expression, 'target_id': tab_id})
    destination = ORIGIN + '/?variant=' + variant
    await transport.call('browser_goto', {'url': destination})
    await transport.call('browser_wait_for_load', {})
    observed = []
    for _ in range(100):
        try:
            after = await transport.call('browser_js', {'expression': expression, 'target_id': tab_id})
            observed.append(after)
            expected_kind = {'login': 'login', 'wait': 'unsupported'}.get(variant, 'search')
            if (after['url'] == destination and after['epoch'] != before['epoch'] and
                    after['ready'] == 'complete' and after['kind'] == expected_kind and
                    after['facts']['searched'] is False and after['facts']['page_index'] == 0):
                save(directory / 'setup.json', {'before': before, 'observations': observed, 'expected_url': destination})
                return
        except HarnessError as error:
            observed.append({'error': str(error)})
        await asyncio.sleep(.05)
    save(directory / 'setup.json', {'before': before, 'observations': observed, 'expected_url': destination})
    raise AssertionError('FIXTURE_SETUP_NOT_READY')


async def verify(evidence, name, run_id, container, image_id, provider_name='mock'):
    inspected = json.loads(docker('inspect', container).stdout)[0]
    save(evidence / 'container-inspect.json', inspected)
    attest(inspected, name, run_id, image_id)
    assert inspected['Id'] == container
    metadata = docker('exec', container, 'python', '-c', '''import hashlib,importlib.metadata,importlib.util,json,platform,subprocess
p=importlib.util.find_spec('mcp_server').origin
print(json.dumps({'python':platform.python_version(),'platform':platform.platform(),'harness':importlib.metadata.version('browser-harness'),'mcp':importlib.metadata.version('mcp'),'mcp_source_sha256':hashlib.sha256(open(p,'rb').read()).hexdigest(),'chrome':subprocess.check_output(['google-chrome','--version'],text=True).strip()}))''')
    versions = json.loads(metadata.stdout)
    save(evidence / 'versions.json', versions)
    (evidence / 'distributed-mcp-server.py').write_text(docker('exec', container, 'python', '-c', "import importlib.util; print(open(importlib.util.find_spec('mcp_server').origin).read(), end='')").stdout, encoding='utf-8')
    (evidence / 'chrome-deb.sha256').write_text(docker('exec', container, 'cat', '/opt/verification/chrome-deb.sha256').stdout, encoding='utf-8')
    (evidence / 'installed-packages.txt').write_text(docker('exec', container, 'pip', 'freeze', '--all').stdout, encoding='utf-8')
    (evidence / 'chrome-package.txt').write_text(docker('exec', container, 'dpkg-query', '-W', 'google-chrome-stable').stdout, encoding='utf-8')
    assert versions['harness'] == '0.1.13' and versions['mcp'] == '2.1.1'
    assert versions['chrome'].startswith('Google Chrome ')
    ready = None
    for _ in range(300):
        attempt = docker('exec', container, 'cat', '/tmp/ready.json', check=False)
        if attempt.returncode == 0:
            ready = json.loads(attempt.stdout)
            break
        await asyncio.sleep(.2)
    if ready is None:
        raise RuntimeError('CHROME_NOT_READY: ' + docker('logs', container, check=False).stdout)
    save(evidence / 'chrome-ready.json', ready)
    chrome_processes = json.loads(docker('exec', container, 'python', '-c', '''import glob,json
rows=[]
for path in glob.glob('/proc/[0-9]*/cmdline'):
 try: args=open(path,'rb').read().decode().split('\\0')
 except (FileNotFoundError,PermissionError): continue
 if '--remote-debugging-port=9222' in args: rows.append(args)
print(json.dumps(rows))''').stdout)
    save(evidence / 'chrome-processes.json', chrome_processes)
    assert len(chrome_processes) == 1
    assert '--user-data-dir=/tmp/chrome-profile' in chrome_processes[0]
    assert '--remote-debugging-address=127.0.0.1' in chrome_processes[0]
    egress = json.loads(docker('exec', container, 'python', '-c', '''import json,socket
out=[]
for host,port in [('1.1.1.1',443),('169.254.169.254',80)]:
 try:
  with socket.create_connection((host,port),timeout=2): pass
  out.append({'host':host,'port':port,'connected':True})
 except OSError as e: out.append({'host':host,'port':port,'connected':False,'error':str(e)})
print(json.dumps(out))''').stdout)
    save(evidence / 'egress-socket.json', egress)
    assert len(egress) == 2 and all(not row['connected'] for row in egress)
    endpoint = ready['version']['webSocketDebuggerUrl']
    tab_id = ready['tab']['id']
    command = ('docker', 'exec', '-i', '-e', 'BU_CDP_WS=' + endpoint, '-e', 'BU_NAME=' + name,
               container, 'browser-harness-mcp')
    transport = EvidenceTransport(command, evidence)
    results = []
    provider = None
    try:
        await transport.start()  # Real initialize and tools/list must pass the product schema contract.
        assert versions['mcp_source_sha256'] == DISTRIBUTED_MCP_SHA256, 'DISTRIBUTED_SOURCE_CHANGED'
        save(evidence / 'source-contract.json', {'reviewed_main_sha256': UPSTREAM_SOURCE_SHA256,
            'distributed_sha256': DISTRIBUTED_MCP_SHA256,
            'review_basis': 'Root independently reviewed source diff: only exception channel changes from MCP ToolError to JSON error. Existing transport rejects both; actual tools/list passes unchanged production validate_tools.',
            'composition': 'Pristine browser-harness[mcp]==0.1.13 distribution plus mcp==2.1.1; installed source is not patched.'})
        try:
            await transport.call('browser_cdp', {'method': 'DecisionBrowserVerification.invalidMethod', 'params': {}})
        except HarnessError as error:
            assert str(error) == 'MCP_OPERATION_FAILED', str(error)
            save(evidence / 'error-channel-proof.json', {'passed': True, 'reason': str(error)})
        else:
            raise AssertionError('REAL_MCP_ERROR_NOT_REJECTED')
        initial = await transport.call('browser_current_tab', {})
        save(evidence / 'initial-harness-tab.json', initial)
        await transport.call('browser_switch_tab', {'target': tab_id})
        current = await transport.call('browser_current_tab', {})
        save(evidence / 'bound-fixture-tab.json', current)
        assert current['targetId'] == tab_id, current
        browser_egress = await transport.call('browser_js', {'expression': '''(async()=>{const r=[];for(const url of ['https://1.1.1.1/','http://169.254.169.254/']){try{await fetch(url,{signal:AbortSignal.timeout(2000)});r.push({url,connected:true})}catch(e){r.push({url,connected:false,error:String(e)})}}return r})()''', 'target_id': tab_id})
        save(evidence / 'egress-chrome.json', browser_egress)
        assert len(browser_egress) == 2 and all(not row['connected'] for row in browser_egress)
        scenarios = [
            ('all', 'Synthetic', 'normal', 'enforced', EXPECTED, 'SUCCEEDED', 6, ''),
            ('filtered', 'a', 'normal', 'enforced', EXPECTED[:4], 'SUCCEEDED', 4, ''),
            ('empty', 'no-such-fixture', 'normal', 'enforced', [], 'SUCCEEDED', 2, ''),
            ('observe', 'Synthetic', 'normal', 'observe-only', [], 'OBSERVED', 0, 'OBSERVE_ONLY_NO_ACTION'),
            ('login', 'Synthetic', 'login', 'enforced', [], 'WAITING_APPROVAL', 0, 'HUMAN_REQUIRED'),
            ('wait', 'Synthetic', 'wait', 'enforced', [], 'ESCALATED', 0, 'UNSUPPORTED_OR_TRUNCATED_STATE'),
            ('labels', 'Synthetic', 'labels', 'enforced', EXPECTED, 'SUCCEEDED', 6, ''),
            ('injection', 'Synthetic', 'injection', 'enforced', EXPECTED, 'SUCCEEDED', 6, ''),
        ]
        if provider_name == 'azure':
            provider = live_azure_provider(evidence)
            save(evidence / 'azure-contract.json', {'model': provider.MODEL, 'auth': 'azure-cli',
                'max_http_requests': 12, 'max_retries': 0, 'max_task_cost_units': 12000,
                'thresholds': {'top': .90, 'margin': .20, 'state_supported': .95},
                'endpoint_sha256': hashlib.sha256(str(provider.client.base_url).encode()).hexdigest()})
            scenarios = [(mode, 'Synthetic', 'normal', mode, EXPECTED, 'SUCCEEDED', 6, '')
                         for mode in ('enforced', 'shadow')]
        for label, query, variant, mode, records, state, steps, reason in scenarios:
            directory = evidence / label
            directory.mkdir()
            try:
                await prepare_fixture(transport, tab_id, variant, directory)
            except Exception as error:
                save(directory / 'journey-failure.json', {'error': str(error), 'traceback': traceback.format_exc()})
                results.append({'journey': label, 'passed': False, 'error': str(error)})
                print(json.dumps(results[-1]), flush=True)
                continue
            settings = HarnessSettings(endpoint=endpoint, profile=Path('/tmp/chrome-profile'),
                runtime_dir=Path('/tmp/harness'), daemon_name=name, fixture_origin=ORIGIN, tab_id=tab_id)
            # Construction is verifier-scoped and follows attestation+egress checks above.
            # Production start() remains fail-closed and is never replaced or patched.
            driver = HarnessMCPDriver(settings, transport, session_id=run_id + '-' + label)
            journal = Journal(directory / 'journal.sqlite3')
            try:
                task = TaskSpec(task_id=run_id + '-' + label, query=query, allowed_origins=(ORIGIN,),
                    budget=Budget(max_steps=6, max_cost_units=12000) if provider else Budget())
                calls_before = len(provider.records) if provider else 0
                controller = Controller(driver, provider or MockDecisionProvider(), policy=FixturePolicy(), journal=journal,
                    lane=BrowserLane(driver.session_id, tab_id), mode=mode,
                    routing='model' if provider else 'rules', model=provider.MODEL if provider else 'mock-v1')
                result = (await controller.run(task)).model_dump(mode='json')
                save(directory / 'result.json', result)
                journal.export_events(directory / 'audit.jsonl')
                operations = [dict(zip(('state', 'reason'), row)) for row in journal.db.execute('SELECT state, reason FROM operations ORDER BY rowid')]
                events = [{'schema_version': '1.0', 'event_id': str(seq), 'timestamp': stamp, 'task_id': task_id, 'event_type': event, 'metadata': json.loads(payload)}
                    for seq, stamp, task_id, event, payload in journal.db.execute('SELECT * FROM events ORDER BY sequence')]
                snapshots = [json.loads(row[0]) for row in journal.db.execute('SELECT snapshot FROM observations ORDER BY rowid')]
                save(directory / 'sqlite-state.json', {'operations': operations, 'events': events, 'observations': snapshots})
                audit_matches = [json.loads(line) for line in (directory / 'audit.jsonl').read_text(encoding='utf-8').splitlines()] == events
                expected = {'state': state, 'steps': steps, 'records': records, 'model_calls': steps if provider else 0, 'reason': reason}
                actual = {key: result[key] for key in expected}
                passed = audit_matches and actual == expected and operations == [{'state': 'VERIFIED', 'reason': 'POSTCONDITION_VERIFIED'}] * steps
                proof = {'passed': passed, 'audit_matches_sqlite': audit_matches, 'expected': expected, 'actual': actual, 'operations': operations}
                if provider:
                    calls = provider.records[calls_before:]
                    proof['api_validated'] = bool(calls) and all('validated_answer' in call and
                        len(call['http']) == 1 and call['http'][0]['status'] == 200 for call in calls)
                    proof['mode'] = mode
                    decisions = [event for event in events if event['event_type'] == 'decision']
                    proof['validated_decisions'] = len(decisions)
                    proof['passed'] = passed = passed and proof['api_validated'] and len(calls) == len(decisions) == steps
                save(directory / 'proof.json', proof)
                results.append({'journey': label, 'passed': passed, 'state': result['state'], 'reason': result['reason']})
                try:
                    # Diagnostic capture happens after the outcome is persisted; it cannot change verification.
                    await transport.call('browser_wait_for_load', {})
                    screenshot = await transport.call('browser_cdp', {'method': 'Page.captureScreenshot', 'params': {'format': 'png'}})
                    (directory / 'screenshot.png').write_bytes(base64.b64decode(screenshot['data'], validate=True))
                    assert (directory / 'screenshot.png').read_bytes().startswith(b'\x89PNG\r\n\x1a\n')
                    results[-1]['screenshot_passed'] = True
                except HarnessError as error:
                    save(directory / 'screenshot-failure.json', {'reason': str(error)})
                    results[-1]['screenshot_passed'] = False
            except Exception as error:
                save(directory / 'journey-failure.json', {'error': str(error), 'traceback': traceback.format_exc()})
                if not results or results[-1]['journey'] != label:
                    results.append({'journey': label, 'passed': False, 'error': str(error)})
                else:
                    results[-1]['passed'] = False
            finally:
                journal.close()
            print(json.dumps(results[-1]), flush=True)
    finally:
        save(evidence / 'mcp-calls.json', transport.calls)
        save(evidence / 'summary.json', {'journeys': results, 'provider': provider_name,
            'passed': len(results) == (2 if provider_name == 'azure' else 8) and all(row['passed'] for row in results),
            'bounds': ['Actual Google Chrome in Docker, synthetic fixture only.',
                       'Exact loopback FixturePolicy adapter; production Policy/registry origin approval is not established.',
                       'HarnessMCPDriver.start remains fail-closed; verifier construction requires external container attestation.',
                       'Chrome fetch probes also encounter fixture CSP; socket probes independently establish blocked container egress.',
                       'Azure selected: real Microsoft-Decision-1 via host Azure CLI, enforced then shadow; shadow actions follow known rules.' if provider else 'Mock provider: rules routing and zero inference.',
                       'No personal browser/profile or production acceptance.']})
        if provider:
            await provider.client.aclose()
        await transport.close()
    assert all(row['passed'] for row in results), 'CHROME_JOURNEY_FAILED; see retained evidence'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--provider', choices=('mock', 'azure'), default='mock')
    args = parser.parse_args()
    run_id = uuid4().hex
    evidence = ROOT / '.audit/chrome' / run_id
    evidence.mkdir(parents=True)
    name = 'bu-j-chrome-' + run_id
    print(str(evidence), flush=True)
    for source in ('run.py', 'container.py', 'Dockerfile'):
        (evidence / source).write_bytes((ROOT / 'verification/chrome' / source).read_bytes())
    run_attempted = False
    container = None
    try:
        save(evidence / 'build-command.json', {'argv': ['docker', 'build', '--platform', 'linux/amd64', '-f', 'verification/chrome/Dockerfile', '-t', IMAGE, '.'], 'cwd': str(ROOT), 'runner_pid': os.getpid()})
        build = docker('build', '--platform', 'linux/amd64', '-f', 'verification/chrome/Dockerfile', '-t', IMAGE, '.', timeout=900)
        (evidence / 'build.log').write_text(build.stdout + build.stderr, encoding='utf-8')
        images = json.loads(docker('image', 'inspect', IMAGE).stdout)
        save(evidence / 'image-inspect.json', images)
        image_id = images[0]['Id']
        save(evidence / 'run-command.json', {'argv': ['docker', 'run', '-d', '--platform', 'linux/amd64', '--name', name, '--label', OWNER + '=' + run_id, '--network', 'none', '--read-only', '--tmpfs', '/tmp:rw,nosuid,nodev,size=1073741824', '--shm-size', '256m', '--cap-drop', 'ALL', '--security-opt', 'no-new-privileges', '--pids-limit', '256', image_id]})
        run_attempted = True
        started = docker('run', '-d', '--platform', 'linux/amd64', '--name', name, '--label', OWNER + '=' + run_id,
            '--network', 'none', '--read-only', '--tmpfs', '/tmp:rw,nosuid,nodev,size=1073741824', '--shm-size', '256m',
            '--cap-drop', 'ALL', '--security-opt', 'no-new-privileges', '--pids-limit', '256', image_id)
        container = started.stdout.strip()
        save(evidence / 'created.json', {'container': started.stdout.strip(), 'name': name, 'run_id': run_id})
        asyncio.run(verify(evidence, name, run_id, container, image_id, args.provider))
    except BaseException:
        (evidence / 'failure.txt').write_text(traceback.format_exc(), encoding='utf-8')
        raise
    finally:
        if run_attempted:
            cleanup(evidence, name, run_id, container)


if __name__ == '__main__':
    main()
