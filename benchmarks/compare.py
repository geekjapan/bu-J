"""Offline tools only. Generate plans; never fabricate or execute measurements.

Usage: python -m benchmarks.compare workload --seed 42 > plan.jsonl
       python -m benchmarks.compare report plan.jsonl results.jsonl

Result rows copy every plan field, then add versions, measurement ('real' or
'mock'), status, misoperation, elapsed_ms, timeout_ms, phases_ms, model_calls,
input_tokens, and cost. Unknown usage/cost is null; cost is an observed USD
amount, never a price estimate. phases_ms has exactly PHASES; decision includes
API transport, Retry-After and retries. All elapsed time, including human
approval, must be assigned to phases. B–E versions share COMMON_VERSIONS; model
and implementation versions may differ. A uses its own isolated fixture.
"""

import argparse
from collections import Counter, defaultdict
import itertools
import json
import math
from pathlib import Path
import random
import statistics

SERIES = tuple('ABCDE')
PHASES = ('queue', 'observe', 'normalize', 'decision', 'policy', 'execute',
          'browser_wait', 'verify', 'recovery', 'approval_wait')
COMMON_VERSIONS = ('controller', 'driver', 'browser', 'network', 'data',
                   'candidates', 'completion', 'approval')
STATUSES = ('success', 'failure', 'timeout', 'stopped', 'unknown')


def workload(seed=42, tasks_per_split=30, trials=5):
    if tasks_per_split < 30 or trials < 5:
        raise ValueError('Need at least 30 tasks per split and 5 trials')
    conditions = list(itertools.product(('cold', 'warm'), ('first', 'reuse'),
                      ('normal', 'fault'), ('short', 'long'), ('local', 'remote')))
    workflows = ('list', 'paginate', 'filter', 'detail', 'extract', 'recover')
    rows = []
    for split in ('tuning', 'heldout'):
        for task in range(tasks_per_split):
            task_id = f'{split}-{task:03d}'
            for trial in range(trials):
                pair_id = f'{task_id}-{trial:02d}'
                for series in SERIES:
                    rows.append(dict(schema_version=1, seed=seed, split=split,
                        task_id=task_id, pair_id=pair_id, trial=trial, series=series,
                        fixture_id=f'{task_id}-{"isolated-A" if series == "A" else "shared"}',
                        workflow=workflows[task % len(workflows)],
                        fixture=dict(site=f'{split}.example.test', variant=task,
                                     expected_ids=[f'item-{task}-{i}' for i in range(3)]),
                        conditions=dict(zip(('cache', 'use', 'scenario', 'page_length',
                                             'distance'), conditions[task % len(conditions)]))))
    random.Random(seed).shuffle(rows)
    return rows


def read_jsonl(path):
    return [json.loads(line) for line in Path(path).read_text().splitlines() if line.strip()]


def number(value, name):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
        raise ValueError(f'{name} must be finite and nonnegative')
    return value


def percentile(values, fraction):
    ordered = sorted(values)
    index = (len(ordered) - 1) * fraction
    lo, hi = math.floor(index), math.ceil(index)
    return ordered[lo] + (ordered[hi] - ordered[lo]) * (index - lo)


def validate(plan, results):
    expected = {(r['pair_id'], r['series']): r for r in plan}
    if not expected or len(expected) != len(plan):
        raise ValueError('Empty or duplicate plan')
    pairs = defaultdict(dict)
    for row in results:
        key = (row['pair_id'], row['series'])
        if key not in expected or row['series'] in pairs[row['pair_id']]:
            raise ValueError('Unexpected or duplicate result')
        if any(row.get(field) != value for field, value in expected[key].items()):
            raise ValueError('Result does not match its planned trial')
        if row.get('status') not in STATUSES or type(row.get('misoperation')) is not bool:
            raise ValueError('Invalid outcome')
        if row.get('measurement') not in ('real', 'mock'):
            raise ValueError('Missing measurement provenance')
        versions = row.get('versions', {})
        if any(not isinstance(versions.get(k), str) or not versions[k]
               for k in (*COMMON_VERSIONS, 'model', 'implementation')):
            raise ValueError('Missing exact versions/conditions')
        phases = row.get('phases_ms', {})
        if set(phases) != set(PHASES):
            raise ValueError('Incomplete phase timing')
        for key, value in phases.items():
            number(value, key)
        elapsed = number(row.get('elapsed_ms'), 'elapsed_ms')
        if not math.isclose(sum(phases.values()), elapsed, abs_tol=0.01):
            raise ValueError('Phases must account for total elapsed time')
        if number(row.get('timeout_ms'), 'timeout_ms') <= 0:
            raise ValueError('Positive time limit required for every outcome')
        for key in ('model_calls', 'input_tokens', 'cost'):
            if key not in row:
                raise ValueError(f'{key} must be supplied or null')
            if row[key] is not None:
                number(row[key], key)
                if key != 'cost' and not isinstance(row[key], int):
                    raise ValueError('Usage counts must be integers')
        pairs[row['pair_id']][row['series']] = row
    if len(results) != len(plan):
        raise ValueError('Missing planned trials (including failures/timeouts)')
    for group in pairs.values():
        if set(group) != set(SERIES):
            raise ValueError('Every trial requires A–E pairs')
        base = group['B']
        for other in group.values():
            for field in ('seed', 'split', 'task_id', 'trial', 'workflow', 'fixture', 'conditions'):
                if other[field] != base[field]:
                    raise ValueError('Incomparable paired task conditions')
        if group['A']['fixture_id'] == base['fixture_id']:
            raise ValueError('A requires an isolated fixture')
        for series in 'CDE':
            other = group[series]
            if any(other['versions'][key] != base['versions'][key] for key in COMMON_VERSIONS):
                raise ValueError('Incomparable B–E conditions')
            if other['measurement'] != base['measurement'] or other['timeout_ms'] != base['timeout_ms']:
                raise ValueError('Incomparable measurement/time limits')
    return pairs


def aggregate(rows):
    elapsed = [r['elapsed_ms'] for r in rows]
    statuses = Counter(r['status'] for r in rows)
    usage = {}
    for field in ('model_calls', 'input_tokens', 'cost'):
        known = [r[field] for r in rows if r[field] is not None]
        usage[field] = dict(known_total=sum(known), unknown_trials=len(rows) - len(known),
                            total=sum(known) if len(known) == len(rows) else None)
    return dict(trials=len(rows), statuses={s: statuses[s] for s in STATUSES},
                success_rate=statuses['success'] / len(rows),
                misoperation_rate=sum(r['misoperation'] for r in rows) / len(rows),
                stop_rate=statuses['stopped'] / len(rows),
                unknown_rate=statuses['unknown'] / len(rows),
                p50_ms=percentile(elapsed, .5), p95_ms=percentile(elapsed, .95),
                time_limits_ms=sorted({r['timeout_ms'] for r in rows}),
                machine_mean_ms=statistics.mean(r['elapsed_ms'] - r['phases_ms']['approval_wait'] for r in rows),
                human_wait_mean_ms=statistics.mean(r['phases_ms']['approval_wait'] for r in rows),
                phases_mean_ms={p: statistics.mean(r['phases_ms'][p] for r in rows) for p in PHASES},
                usage=usage)


def report(plan, results, seed=42, bootstrap_samples=2000):
    if bootstrap_samples < 100:
        raise ValueError('At least 100 bootstrap samples required')
    pairs = validate(plan, results)
    grouped = defaultdict(list)
    for row in results:
        grouped[(row['split'], row['series'])].append(row)
    summaries = {f'{split}/{series}': aggregate(rows) for (split, series), rows in sorted(grouped.items())}
    strata = defaultdict(list)
    for row in results:
        key = f"{row['split']}/{row['series']}/" + json.dumps(row['conditions'], sort_keys=True)
        strata[key].append(row)
    paired_differences = [dict(pair_id=pair_id, task_id=group['B']['task_id'],
        split=group['B']['split'],
        elapsed_ms=group['E']['elapsed_ms'] - group['B']['elapsed_ms'],
        success=int(group['E']['status'] == 'success') - int(group['B']['status'] == 'success'))
        for pair_id, group in sorted(pairs.items())]
    clusters = defaultdict(list)
    for group in pairs.values():
        b, e = group['B'], group['E']
        if b['split'] == 'heldout':
            clusters[b['task_id']].append(int(e['status'] == 'success') - int(b['status'] == 'success'))
    if not clusters:
        raise ValueError('No heldout paired tasks')
    differences = [statistics.mean(values) for values in clusters.values()]
    rng = random.Random(seed)
    draws = [statistics.mean(rng.choices(differences, k=len(differences))) for _ in range(bootstrap_samples)]
    mean = statistics.mean(differences)
    # A degenerate bootstrap cannot certify unseen task failures. Bound [-1, 1] clusters too.
    radius = math.sqrt(2 * math.log(40) / len(differences))
    interval = [max(-1, min(percentile(draws, .025), mean - radius)),
                min(1, max(percentile(draws, .975), mean + radius))]
    sufficient = len(clusters) >= 30 and all(len(v) >= 5 for v in clusters.values())
    real = all(r['measurement'] == 'real' for r in results)
    e_summary = summaries['heldout/E']
    verdict = ('insufficient_sample' if not sufficient else
               'mock_only' if not real else
               'pass' if interval[0] >= -.02 and e_summary['success_rate'] >= .95 else
               'fail' if interval[1] < -.02 else 'inconclusive')
    return dict(schema_version=1, seed=seed, bootstrap_samples=bootstrap_samples,
                summaries=summaries, strata={k: aggregate(v) for k, v in sorted(strata.items())},
                paired_trial_ids=sorted(pairs), paired_differences=paired_differences,
                trial_records=results,
                noninferiority=dict(verdict=verdict, task_clusters=len(clusters),
                    success_difference=mean, bootstrap_ci95=[percentile(draws, .025), percentile(draws, .975)],
                    conservative_ci95=interval, margin=-.02,
                    method='paired task-cluster bootstrap with bounded-cluster Hoeffding guard'),
                production_speed_claim=False,
                note='All trial times include failures and timeouts. No production speed conclusion is automated.')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    generate = commands.add_parser('workload')
    generate.add_argument('--seed', type=int, default=42)
    analyze = commands.add_parser('report')
    analyze.add_argument('plan')
    analyze.add_argument('results')
    analyze.add_argument('--seed', type=int, default=42)
    args = parser.parse_args()
    if args.command == 'workload':
        for row in workload(args.seed):
            print(json.dumps(row, sort_keys=True, allow_nan=False))
    else:
        print(json.dumps(report(read_jsonl(args.plan), read_jsonl(args.results), args.seed),
                         indent=2, sort_keys=True, allow_nan=False))


if __name__ == '__main__':
    main()
