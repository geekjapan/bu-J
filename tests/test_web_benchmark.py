import hashlib
import json
from pathlib import Path

from benchmarks.web import summarize


def test_failed_fast_attempts_do_not_become_speed_wins():
    rows = []
    for trial in range(5):
        for arm, verified, elapsed in [('upstream', True, 7000+trial), ('bu-j', False, 150)]:
            rows.append({'task':'hotel','arm':arm,'verified':verified,'status':'SUCCEEDED' if verified else 'ESCALATED',
                'elapsed_ms':elapsed,'decision_calls':6 if verified else 1,'helper_calls':1 if verified else 0,'tokens':100})
    report = summarize(rows)
    assert report['hotel/upstream']['successful_median_ms'] == 7002
    assert report['hotel/upstream']['successful_range_ms'] == [7000,7004]
    assert report['hotel/bu-j']['successful_median_ms'] is None
    assert report['hotel/bu-j']['attempts'] == 5 and report['hotel/bu-j']['successes'] == 0
    assert report['hotel/bu-j']['outcomes'] == {'ESCALATED':5}


def test_baseline_agent_matches_pinned_original_bytes():
    folder = Path(__file__).parents[1]/'benchmarks/upstream_runtime'
    manifest=json.loads((folder/'UPSTREAM.json').read_text())
    assert hashlib.sha256((folder/'agent.py').read_bytes()).hexdigest() == manifest['files']['agent.py']
    assert manifest['commit'] == '1231850a0bf1a0c0341fe408ef1668dbbfdfac46'
    assert 'MIT License' in (folder/'LICENSE').read_text()


def test_lost_child_usage_stops_suite_before_any_later_request(tmp_path,monkeypatch):
    import argparse
    import subprocess
    from benchmarks import web
    executions=[]
    class Chrome:
        def __init__(self,path):self.manifest_path=path/'manifest.json'
        def __enter__(self):return self
        def __exit__(self,*args):return False
    def killed(command,**kwargs):
        executions.append(command)
        raise subprocess.TimeoutExpired(command,180)
    monkeypatch.setattr(web,'ManagedChrome',Chrome)
    monkeypatch.setattr(web.subprocess,'run',killed)
    monkeypatch.setattr(web.platform,'platform',lambda:'test')
    args=argparse.Namespace(output=str(tmp_path/'suite'),tasks=['hotel'],trials=5,flight_date='2027-09-20')
    assert web.suite(args)==1
    assert len(executions)==1
    folder=tmp_path/'suite'
    rows=json.loads((folder/'runs.json').read_text())
    assert rows[0]['decision_calls'] is None and rows[0]['helper_calls'] is None
    assert rows[0]['api_usage_unknown'] and not rows[0]['verified']
    stop=json.loads((folder/'suite-stop.json').read_text())
    assert stop['remaining_attempts_unrun']==9
