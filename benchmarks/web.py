"""Matched web-task comparison using scoped Chrome and real Azure APIs."""
import argparse
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import platform
import resource
import statistics
import subprocess
import sys
import time
from uuid import uuid4

from decision_browser.web.environment import ManagedChrome, validate_manifest
from decision_browser.web.provider import ModelClient
from decision_browser.web.tasks import get_task, verify_task

ROOT = Path(__file__).resolve().parents[1]


def save(path, value):
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + '\n')


def upstream_run(args):
    from benchmarks.upstream_runtime import browser as browser_adapter, model as model_adapter
    from benchmarks.upstream_runtime.agent import Agent
    manifest = validate_manifest(args.manifest)
    task = get_task(args.task, args.flight_date)
    browser_adapter.TASK, browser_adapter.ENDPOINT = task, manifest['endpoint']
    model = ModelClient(*(os.environ.get(name, '') for name in (
        'AZURE_ENDPOINT', 'AZURE_DEPLOYMENT_NAME', 'AZURE_TEXT_ENDPOINT', 'AZURE_TEXT_DEPLOYMENT')),
        max_calls=int(os.environ.get('WEB_DECISION_CALL_LIMIT','120')),
        max_text_calls=int(os.environ.get('WEB_TEXT_CALL_LIMIT','20')))
    model.authenticate()
    model_adapter.CLIENT = model
    agent = None
    result = {'status': 'FAILED', 'reason': 'INITIALIZATION_FAILED', 'elapsed_ms': None}
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    cpu_start = time.process_time()
    try:
        agent = Agent(task.url, task.goal, screenshots=False)
        before = Counter(agent.browser.cdp_counts)
        for state in agent.run():
            if state['elapsed_ms'] > 120000:
                raise TimeoutError('TASK_DEADLINE')
        result = agent.snapshot()
        result['cdp_counts_run'] = dict(Counter(agent.browser.cdp_counts) - before)
        result['browser_version'] = agent.browser.browser_version
        result['independent_verification'] = verify_task(task, agent.browser.raw_observe())
        result['status'] = 'SUCCEEDED' if result['status'] == 'done' and result['independent_verification']['passed'] else 'FAILED'
    except Exception as error:
        if agent is not None:
            result = {**agent.snapshot(), 'status': 'FAILED', 'reason': 'RUNTIME_ERROR'}
        result['exception_type'] = type(error).__name__
    finally:
        result['calls'], result['model_text_calls'], result['total_tokens'] = model.calls, model.text_calls, model.total_tokens
        result['cpu_seconds_process'] = time.process_time() - cpu_start
        if agent is not None:
            try:
                if 'independent_verification' not in result:
                    result['independent_verification'] = verify_task(task, agent.browser.raw_observe())
                output.with_suffix('.png').write_bytes(agent.browser.screenshot())
                result['cdp_counts_total'] = dict(agent.browser.cdp_counts)
            except Exception as error:
                result['diagnostic_error'] = type(error).__name__
            finally:
                agent.close()
        model.close()
        save(output, result)
    return 0 if result['status'] == 'SUCCEEDED' else 1


def summarize(rows):
    summary = {}
    for task in ('flights', 'wikipedia', 'hotel'):
        for arm in ('upstream', 'bu-j'):
            group = [r for r in rows if r['task'] == task and r['arm'] == arm]
            success = [r for r in group if r['verified']]
            times = [r['elapsed_ms'] for r in success if isinstance(r.get('elapsed_ms'), (int, float))]
            summary[task + '/' + arm] = {'attempts': len(group), 'successes': len(success),
                'outcomes': dict(Counter(r['status'] for r in group)),
                'successful_median_ms': statistics.median(times) if times else None,
                'successful_range_ms': [min(times), max(times)] if times else None,
                'decision_http_calls_known': sum(r['decision_calls'] or 0 for r in group),
                'helper_http_calls_known': sum(r['helper_calls'] or 0 for r in group),
                'reported_tokens_known': sum(r['tokens'] or 0 for r in group),
                'unknown_usage_attempts': sum(r.get('api_usage_unknown', False) for r in group)}
    return summary


def suite(args):
    folder = Path(args.output or ROOT / '.audit/web-benchmark' / uuid4().hex).resolve()
    folder.mkdir(parents=True, exist_ok=False)
    sources = {str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest()
               for base in ('src/decision_browser/web', 'benchmarks', 'verification/web_benchmark', 'fixtures/web_benchmark')
               for p in (ROOT / base).rglob('*') if p.is_file() and '__pycache__' not in p.parts}
    save(folder / 'conditions.json', {'upstream_commit': '1231850a0bf1a0c0341fe408ef1668dbbfdfac46',
        'date': args.flight_date, 'trials_per_arm': args.trials, 'tasks': args.tasks,
        'decision_model': 'microsoft-decision-1', 'text_model': 'gpt-4.1-mini',
        'viewport': [1120,780], 'profiles': 'Fresh disposable profile for every attempt; order alternates AB/BA.',
        'timing': 'First prediction after initial observation through fresh accepted DONE; setup and independent verifier excluded.',
        'baseline': 'Unchanged pinned Agent loop; shared Azure, scoped DOM/CDP transport and safety filtering adapters. Not pristine historical TypeSafe result.',
        'historical_differences': ['future date2027-09-20','AzureDecision1 instead ofTypeSafeJev','GPT4.1mini instead ofMercury','Docker headlessChrome156 instead ofChrome152existingprofile','shared scopedproxy/CDP/controlpolicy','strict independentWikiarticlecontent andadult/economy checks'],
        'machine': {'platform': platform.platform(), 'cores': os.cpu_count(), 'loadavg': os.getloadavg()},
        'source_sha256': sources, 'created_at': datetime.now(timezone.utc).isoformat()})
    rows = []
    print(str(folder), flush=True)
    for task in args.tasks:
        for trial in range(args.trials):
            arms = ('upstream', 'bu-j') if trial % 2 == 0 else ('bu-j', 'upstream')
            for arm in arms:
                directory = folder / f'{task}-{trial + 1}-{arm}'
                directory.mkdir()
                output = directory / 'result.json'
                command = [sys.executable, '-m', 'benchmarks.web', 'upstream' if arm == 'upstream' else 'bu-j',
                    '--task', task, '--flight-date', args.flight_date, '--journal', str(directory/'journal.sqlite3'),
                    '--task-id', directory.name, '--output', str(output)]
                cpu = resource.getrusage(resource.RUSAGE_CHILDREN)
                process = None
                infrastructure_error = False
                child_started = False
                spent_decisions = sum(r['decision_calls'] for r in rows)
                spent_helpers = sum(r['helper_calls'] for r in rows)
                environment = {**os.environ,
                    'WEB_DECISION_CALL_LIMIT': str(min(120, max(0, 360-spent_decisions))),
                    'WEB_TEXT_CALL_LIMIT': str(min(20, max(0, 120-spent_helpers)))}
                try:
                    with ManagedChrome(directory / 'chrome') as chrome:
                        command += ['--manifest', str(chrome.manifest_path)]
                        save(directory / 'command.json', {'argv': command})
                        child_started = True
                        process = subprocess.run(command, cwd=ROOT, env=environment, capture_output=True, text=True, timeout=180)
                        save(directory / 'process.json', {'returncode': process.returncode, 'stdout': process.stdout, 'stderr': process.stderr})
                except Exception as error:
                    infrastructure_error = True
                    save(directory / 'infrastructure-error.json', {'exception_type': type(error).__name__})
                    if not output.exists():
                        save(output, {'status': 'FAILED', 'reason': 'INFRASTRUCTURE_ERROR', 'exception_type': type(error).__name__})
                data = json.loads(output.read_text())
                calls, helpers = data.get('calls', []), data.get('model_text_calls', [])
                usage_unknown = child_started and (process is None or 'calls' not in data or 'model_text_calls' not in data)
                cpu_after = resource.getrusage(resource.RUSAGE_CHILDREN)
                row = {'task': task, 'trial': trial+1, 'arm': arm,
                    'status': 'INFRASTRUCTURE_ERROR' if infrastructure_error else data.get('status','FAILED'), 'reason': data.get('reason',''),
                    'verified': not infrastructure_error and data.get('status') == 'SUCCEEDED' and data.get('independent_verification',{}).get('passed') is True,
                    'elapsed_ms': data.get('elapsed_ms'), 'decision_calls': None if usage_unknown else len(calls),
                    'helper_calls': None if usage_unknown else len(helpers), 'api_usage_unknown': usage_unknown,
                    'tokens': None if usage_unknown else data.get('total_tokens',0), 'api_error_calls': sum(c.get('http_status') != 200 for c in calls+helpers),
                    'model_latency_ms': sum(c.get('latency_ms',0) for c in calls+helpers),
                    'cpu_seconds_children': (cpu_after.ru_utime+cpu_after.ru_stime)-(cpu.ru_utime+cpu.ru_stime),
                    'cdp_calls_run': sum(data.get('cdp_counts_run',{}).values()), 'evidence': str(directory)}
                rows.append(row)
                save(folder / 'runs.json', rows)
                save(folder / 'summary.json', summarize(rows))
                print(json.dumps(row), flush=True)
                if usage_unknown:
                    save(folder / 'suite-stop.json', {'reason': 'API_USAGE_UNKNOWN_NO_FURTHER_REQUESTS',
                        'remaining_attempts_unrun': len(args.tasks)*args.trials*2-len(rows)})
                    return 1
    return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=('suite','upstream','bu-j'))
    parser.add_argument('--task', choices=('flights','wikipedia','hotel'))
    parser.add_argument('--tasks', nargs='+', choices=('flights','wikipedia','hotel'), default=['flights','wikipedia','hotel'])
    parser.add_argument('--flight-date', default='2027-09-20')
    parser.add_argument('--trials', type=int, default=5)
    for name in ('manifest','journal','task-id','output'):
        parser.add_argument('--'+name)
    args = parser.parse_args()
    if args.command == 'suite':
        if not 1 <= args.trials <= 5:
            parser.error('trials must be between1and5')
        return suite(args)
    if args.command == 'upstream':
        return upstream_run(args)
    from decision_browser.web.cli import main as cli_main
    return cli_main(['--task',args.task,'--flight-date',args.flight_date,'--manifest',args.manifest,
        '--journal',args.journal,'--task-id',args.task_id,'--output',args.output])


if __name__ == '__main__':
    raise SystemExit(main())
