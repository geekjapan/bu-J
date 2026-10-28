import copy
import json

import pytest

from benchmarks.compare import COMMON_VERSIONS, PHASES, aggregate, read_jsonl, report, workload


def measured(plan, measurement='mock'):
    return [dict(row, versions={k: 'test-version' for k in
                 (*COMMON_VERSIONS, 'model', 'implementation')},
                 measurement=measurement, status='success', misoperation=False,
                 elapsed_ms=10, timeout_ms=100, phases_ms={p: 10 if p == 'decision' else 0 for p in PHASES},
                 model_calls=1, input_tokens=None, cost=None) for row in plan]


def small_plan():
    return [r for r in workload() if r['split'] == 'heldout' and r['task_id'] == 'heldout-000']


def test_reproducible_shuffled_plan_has_separate_tasks_and_exact_pairs():
    plan = workload()
    assert plan == workload()
    assert plan != workload(43)
    assert len(plan) == 60 * 5 * 5
    assert len({r['task_id'] for r in plan}) == 60
    assert len({r['fixture']['site'] for r in plan}) == 2
    assert all(sum(r['pair_id'] == row['pair_id'] for r in plan) == 5 for row in plan)
    assert {r['series'] for r in plan} == set('ABCDE')
    assert all('elapsed_ms' not in row and 'status' not in row for row in plan)
    with pytest.raises(ValueError):
        workload(tasks_per_split=29)
    with pytest.raises(ValueError):
        workload(trials=4)


def test_all_failures_and_timeouts_count_and_unknown_cost_is_not_estimated():
    rows = measured(small_plan())[:5]
    for row, status, elapsed in zip(rows, ('success', 'failure', 'timeout', 'stopped', 'unknown'), (1, 2, 100, 4, 5)):
        row.update(status=status, elapsed_ms=elapsed)
        row['phases_ms']['decision'] = elapsed
    rows[1]['misoperation'] = True
    result = aggregate(rows)
    assert result['trials'] == 5
    assert result['success_rate'] == .2
    assert result['misoperation_rate'] == .2
    assert result['stop_rate'] == result['unknown_rate'] == .2
    assert result['statuses']['timeout'] == 1
    assert result['p50_ms'] == 4
    assert result['p95_ms'] == pytest.approx(81)
    assert result['usage']['cost'] == dict(known_total=0, unknown_trials=5, total=None)


@pytest.mark.parametrize('mutation', ['missing', 'duplicate', 'version', 'conditions', 'phase', 'nan', 'usage', 'provenance', 'isolation'])
def test_invalid_or_incomparable_results_are_rejected(mutation):
    plan = small_plan()
    results = measured(plan)
    e = next(r for r in results if r['series'] == 'E')
    if mutation == 'missing':
        results.pop()
    elif mutation == 'duplicate':
        results[-1] = copy.deepcopy(results[0])
    elif mutation == 'version':
        e['versions']['browser'] = 'different'
    elif mutation == 'conditions':
        e['conditions'] = dict(e['conditions'], cache='different')
    elif mutation == 'phase':
        del e['phases_ms']['decision']
    elif mutation == 'nan':
        e['elapsed_ms'] = float('nan')
    elif mutation == 'usage':
        del e['cost']
    elif mutation == 'provenance':
        del e['measurement']
    else:
        a = next(r for r in plan if r['series'] == 'A')
        a['fixture_id'] = next(r for r in plan if r['pair_id'] == a['pair_id'] and r['series'] == 'B')['fixture_id']
        results = measured(plan)
    with pytest.raises(ValueError):
        report(plan, results, bootstrap_samples=100)


def test_small_perfect_mock_samples_cannot_claim_noninferiority_or_speed():
    plan = small_plan()
    result = report(plan, measured(plan), bootstrap_samples=100)
    assert result['noninferiority']['verdict'] == 'insufficient_sample'
    assert result['noninferiority']['task_clusters'] == 1
    assert result['production_speed_claim'] is False
    assert len(result['paired_trial_ids']) == 5
    assert all(row['elapsed_ms'] == 0 for row in result['paired_differences'])


def test_full_mock_plan_preserves_evidence_and_blocks_production_claims():
    plan = workload()
    results = measured(plan)
    result = report(plan, results, bootstrap_samples=100)
    assert result['noninferiority']['verdict'] == 'mock_only'
    assert result['noninferiority']['task_clusters'] == 30
    assert result['noninferiority']['conservative_ci95'][0] < -.02
    assert result['trial_records'] == results
    assert len(result['strata']) > 10
    assert result['production_speed_claim'] is False


def test_real_small_initial_set_is_inconclusive_even_with_perfect_success():
    plan = workload()
    result = report(plan, measured(plan, 'real'), bootstrap_samples=100)
    assert result['noninferiority']['verdict'] == 'inconclusive'
    assert result['noninferiority']['bootstrap_ci95'] == [0, 0]


def test_jsonl_roundtrip_does_not_invent_measurements(tmp_path):
    path = tmp_path / 'plan.jsonl'
    plan = small_plan()
    path.write_text('\n'.join(json.dumps(r) for r in plan))
    assert read_jsonl(path) == plan
    with pytest.raises(ValueError):
        report(plan, plan, bootstrap_samples=100)


def test_real_clear_success_regression_fails_gate():
    plan = workload()
    rows = measured(plan, 'real')
    for row in rows:
        if row['series'] == 'E':
            row['status'] = 'failure'
    result = report(plan, rows, bootstrap_samples=100)
    assert result['noninferiority']['verdict'] == 'fail'
    assert result['summaries']['heldout/E']['success_rate'] == 0
    assert result['noninferiority']['success_difference'] == -1


def test_machine_time_and_human_wait_are_separated_without_dropping_total():
    rows = measured(small_plan())
    for row in rows:
        row['phases_ms']['decision'] = 7
        row['phases_ms']['approval_wait'] = 3
    summary = aggregate(rows)
    assert summary['machine_mean_ms'] == 7
    assert summary['human_wait_mean_ms'] == 3
    assert summary['p50_ms'] == 10
