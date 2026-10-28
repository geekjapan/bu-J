#!/usr/bin/env python3
"""Exercise the installed synthetic CLI; keep proof outside disposable runtime."""
import argparse
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import uuid

ROOT = Path(__file__).resolve().parents[4]
CLI = ROOT / '.venv/bin/decision-browser'
EXPECTED = [
    {'id': '001', 'name': 'Alpha', 'updated_at': '2026-10-01'},
    {'id': '002', 'name': 'Beta', 'updated_at': '2026-10-02'},
    {'id': '003', 'name': 'Gamma', 'updated_at': '2026-10-03'},
    {'id': '004', 'name': 'Delta', 'updated_at': '2026-10-04'},
    {'id': '005', 'name': 'Epsilon', 'updated_at': '2026-10-05'},
]


def write(path, value):
    path.write_text(json.dumps(value, indent=2) + '\n')


def doctor():
    assert sys.version_info >= (3, 12), 'Use .venv/bin/python (Python >=3.12)'
    versions = {name: importlib.metadata.version(name) for name in ('decision-browser', 'pydantic', 'httpx')}
    process = subprocess.run([str(CLI), '--help'], cwd=ROOT, capture_output=True, text=True, timeout=15)
    assert process.returncode == 0 and '--provider {mock,azure}' in process.stdout, process.stderr
    return {'python': sys.executable, 'versions': versions, 'cli': str(CLI),
            'argv': [str(CLI), '--help'], 'stdout': process.stdout,
            'stderr': process.stderr, 'exit_code': process.returncode,
            'auth_checked': False, 'external_connections_checked': False}


def load_run(path):
    evidence = Path(path).resolve()
    assert evidence.parent == ROOT / '.audit/verification-skill', 'Unexpected evidence directory'
    manifest = json.loads((evidence / 'run.json').read_text())
    runtime = Path(manifest['runtime'])
    assert runtime.parent == Path(tempfile.gettempdir()).resolve(), 'Unexpected runtime parent'
    assert runtime.name.startswith('decision-browser-verify-'), 'Unexpected runtime name'
    return evidence, runtime, manifest


def snapshot(db, destination):
    with sqlite3.connect(f'file:{db}?mode=ro', uri=True) as conn:
        operations = conn.execute('SELECT task_id,state,reason FROM operations ORDER BY rowid').fetchall()
        events = conn.execute('SELECT event_type,payload FROM events ORDER BY sequence').fetchall()
        observations = [json.loads(row[0]) for row in conn.execute('SELECT snapshot FROM observations ORDER BY rowid')]
        with sqlite3.connect(destination / 'journal.sqlite3') as copy:
            conn.backup(copy)
    data = {'operations': operations, 'events': [(kind, json.loads(payload)) for kind, payload in events],
            'observations': observations}
    write(destination / 'sqlite-state.json', data)
    return data


def invoke(evidence, runtime, label, *, query='', mode='enforced', routing='rules',
           journal=None, task_id=None, failure=None):
    folder = evidence / label
    folder.mkdir()
    db = runtime / (journal or f'{label}.sqlite3')
    task_id = task_id or f'{evidence.name}-{label}-{uuid.uuid4().hex}'
    output = folder / 'result.json'
    audit = folder / 'audit.jsonl'
    if failure == 'export':
        audit = runtime / 'absent-parent' / 'audit.jsonl'
    if failure == 'output':
        output = runtime / 'absent-parent' / 'result.json'
    argv = [str(CLI), '--provider', 'mock', '--query', query, '--mode', mode,
            '--routing', routing, '--journal', str(db), '--task-id', task_id,
            '--output', str(output), '--audit-jsonl', str(audit)]
    env = {key: value for key, value in os.environ.items() if not key.startswith('AZURE_')}
    process = subprocess.run(argv, cwd=runtime, env=env, capture_output=True, text=True, timeout=30)
    write(folder / 'command.json', {'argv': argv, 'cwd': str(runtime), 'stdout': process.stdout,
          'stderr': process.stderr, 'exit_code': process.returncode, 'task_id': task_id})
    state = snapshot(db, folder)
    result = json.loads(output.read_text()) if output.exists() else None
    if failure:
        assert process.returncode == 1 and 'SQLite journal remains authoritative' in process.stderr
        assert result is None and not process.stdout
        assert state['operations'] and all(row[1] == 'VERIFIED' for row in state['operations'])
        assert state['events'][-1][0] == 'task_succeeded'
    else:
        assert process.returncode == 0, (label, process.stderr, process.stdout)
        assert result == json.loads(process.stdout), label
        exported = [json.loads(line) for line in audit.read_text().splitlines()]
        assert [(event['event_type'], event['metadata']) for event in exported] == state['events']
        assert all(event['task_id'] == task_id for event in exported)
    return result, state, task_id


def success(result, state, records, steps):
    assert result['state'] == 'SUCCEEDED' and result['records'] == records, result
    assert result['steps'] == steps and len(state['operations']) == steps, result
    assert all(row[1] == 'VERIFIED' for row in state['operations'])


def drive(evidence, runtime, feature):
    assert (runtime / 'owner').read_text() == evidence.name, 'Runtime ownership mismatch'
    if feature == 'search':
        result, state, _ = invoke(evidence, runtime, 'all-records')
        success(result, state, EXPECTED, 6)
        assert result['model_calls'] == 0
        pages = [payload['page_index'] for kind, payload in state['events'] if kind == 'observation']
        assert set(pages) == {0, 1, 2, 3}, pages
        result, state, _ = invoke(evidence, runtime, 'filtered', query='a')
        success(result, state, EXPECTED[:4], 4)
        result, state, _ = invoke(evidence, runtime, 'empty', query='no-such-fixture')
        success(result, state, [], 2)
    elif feature == 'observe':
        result, state, _ = invoke(evidence, runtime, 'observe-only', mode='observe-only')
        assert result['state'] == 'OBSERVED' and result['reason'] == 'OBSERVE_ONLY_NO_ACTION'
        assert result['steps'] == 0 and result['records'] == [] and result['model_calls'] == 0
        assert state['operations'] == []
        assert [kind for kind, _ in state['events']] == ['task_started', 'observation', 'proposal_only']
        assert all(not obs['facts']['searched'] and obs['facts']['page_index'] == 0 for obs in state['observations'])
    elif feature == 'routing':
        for mode, routing in [('enforced', 'rules'), ('enforced', 'model'), ('enforced', 'hybrid'),
                              ('shadow', 'rules'), ('shadow', 'model')]:
            result, state, _ = invoke(evidence, runtime, f'{mode}-{routing}', mode=mode, routing=routing)
            success(result, state, EXPECTED, 6)
            decisions = [payload for kind, payload in state['events'] if kind == 'decision']
            expected_calls = 6 if mode == 'shadow' or routing == 'model' else 0
            assert result['model_calls'] == len(decisions) == expected_calls
            assert all(d['resolved_model'] == 'mock-v1' and d['comparison_match'] and d['mode'] == mode for d in decisions)
    elif feature == 'audit':
        result, state, task_id = invoke(evidence, runtime, 'audit-success', query='a', journal='replay.sqlite3')
        success(result, state, EXPECTED[:4], 4)
        assert any(obs['facts']['query'] == '[redacted]' for obs in state['observations'])
        assert all(obs['redaction_profile'] == 'persisted-redacted-v1' for obs in state['observations'])
        assert all(record['name'] == '[redacted]' for obs in state['observations'] for record in obs['facts']['records'])
        folder = evidence / 'replay'
        folder.mkdir()
        argv = [str(CLI), '--provider', 'mock', '--query', 'a', '--mode', 'enforced', '--routing', 'rules',
                '--task-id', task_id, '--journal', str(runtime / 'replay.sqlite3')]
        process = subprocess.run(argv, cwd=runtime, capture_output=True, text=True, timeout=30)
        write(folder / 'command.json', {'argv': argv, 'cwd': str(runtime), 'stdout': process.stdout,
              'stderr': process.stderr, 'exit_code': process.returncode})
        replay = json.loads(process.stdout)
        assert process.returncode == 1 and replay['state'] == 'FAILED', replay
        assert replay['reason'] == 'AUDIT_OR_JOURNAL_PERSISTENCE_FAILED', replay
        after = snapshot(runtime / 'replay.sqlite3', folder)
        assert after['operations'] == state['operations'], 'Replay added or changed operations'
        invoke(evidence, runtime, 'export-failure', failure='export')
        invoke(evidence, runtime, 'output-failure', failure='output')
    write(evidence / f'{feature}-proof.json', {'feature': feature, 'status': 'PASS',
          'scope': 'installed CLI, synthetic Driver, fixed Mock provider; no live inference/browser claim'})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=('launch', 'doctor', 'drive', 'cleanup'))
    parser.add_argument('--run')
    parser.add_argument('--feature', choices=('search', 'observe', 'routing', 'audit'), default='search')
    args = parser.parse_args()
    if args.command == 'launch':
        report = doctor()
        evidence = ROOT / '.audit/verification-skill' / uuid.uuid4().hex
        evidence.mkdir(parents=True)
        runtime = Path(tempfile.mkdtemp(prefix='decision-browser-verify-')).resolve()
        (runtime / 'owner').write_text(evidence.name)
        write(evidence / 'run.json', {'runtime': str(runtime), 'root': str(ROOT)})
        write(evidence / 'doctor.json', report)
        boundary = {}
        for relative in ('src/decision_browser/cli.py', 'src/decision_browser/drivers/mock.py',
                         'src/decision_browser/providers/mock.py'):
            source = ROOT / relative
            content = source.read_bytes()
            (evidence / relative.replace('/', '__')).write_bytes(content)
            boundary[relative] = hashlib.sha256(content).hexdigest()
        write(evidence / 'boundary.json', {'sha256': boundary,
              'basis': 'cli.run with provider mock constructs MockBrowserDriver and MockDecisionProvider; '
                       'their observe/execute/decide paths contain no network, browser launch or az subprocess. '
                       'The azure_provider subprocess branch is not selected. This is source-path evidence, not an OS egress monitor.'})
        print(evidence)
    elif args.command == 'doctor':
        print(json.dumps(doctor(), indent=2))
    else:
        assert args.run, '--run is required'
        evidence, runtime, manifest = load_run(args.run)
        if args.command == 'drive':
            drive(evidence, runtime, args.feature)
            print(evidence / f'{args.feature}-proof.json')
        else:
            if runtime.exists():
                assert (runtime / 'owner').read_text() == evidence.name, 'Runtime ownership mismatch'
                shutil.rmtree(runtime)
            assert not runtime.exists()
            proofs = sorted(p.name for p in evidence.glob('*-proof.json'))
            write(evidence / 'cleanup.json', {'runtime_removed': True, 'evidence_retained': proofs})
            assert (evidence / 'run.json').exists() and all((evidence / name).exists() for name in proofs)
            print(json.dumps({'runtime_removed': True, 'evidence': str(evidence), 'proofs': proofs}))


if __name__ == '__main__':
    main()
