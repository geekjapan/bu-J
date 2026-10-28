from copy import deepcopy
import json
import sqlite3
import subprocess
import sys
from types import SimpleNamespace

import pytest

from decision_browser.web import agent as module
from decision_browser.web.agent import WebAgent
from decision_browser.web.browser import NotDispatched
from decision_browser.web.journal import WebJournal


class StalePage(RuntimeError):
    pass


class Browser:
    def __init__(self):
        self.actions = []
        self.observations = 0
        self.page = {"url": "https://example.test", "fingerprint": "one", "page_key": [1, "https://example.test"],
                     "marker": "document", "metadata": {"1": {"tag": "input"}},
                     "actions": [{"id": "F1", "kind": "fill", "node": 1, "label": "Search"}]}

    def observe(self):
        self.observations += 1
        return deepcopy(self.page)

    def fresh(self, page, action=None):
        return True

    def act(self, action, page, text=None):
        self.actions.append((action, text))
        if action["kind"] == "fill":
            self.page["actions"][0]["value"] = text


class Model:
    total_tokens = 7

    def __init__(self, choices):
        self.choices = iter(choices)
        self.calls = []
        self.text_calls = []

    def choose(self, page, goal, history):
        choice = next(self.choices)
        decision = {"choice": choice, "accepted": True, "operation": "TYPE_TEXT" if choice == "F1" else choice,
                    "target": "1", "raw_answers": {"raw": choice}, "usage": {"total_tokens": 7}}
        self.calls.append(deepcopy(decision))
        return decision

    def field_context(self, goal, action, page, history):
        return dict(goal=goal, action=action, page=page, history=history)

    def field_text(self, context):
        self.text_calls.append(context)
        return "query", {"model": "helper", "latency_ms": 1}


@pytest.fixture(autouse=True)
def task_boundaries(monkeypatch):
    monkeypatch.setattr(module, "filter_page", lambda task, page, metadata: deepcopy(page))
    monkeypatch.setattr(module, "verify_task", lambda task, page: {"passed": bool(page.get("complete")), "checks": {}})
    monkeypatch.setattr(module.time, "sleep", lambda seconds: None)


def build(tmp_path, choices=("F1", "DONE"), browser=None):
    return WebAgent(SimpleNamespace(goal="Search"), browser or Browser(), Model(choices), tmp_path / "journal.db", "task")


def test_verified_done_preserves_calls_history_and_events(tmp_path):
    browser = Browser()
    browser.page["complete"] = True
    agent = build(tmp_path, browser=browser)
    original_act = browser.act

    def act(*args, **kwargs):
        assert sqlite3.connect(tmp_path / "journal.db").execute("SELECT state FROM web_operations").fetchone()[0] == "DISPATCHED"
        original_act(*args, **kwargs)

    browser.act = act
    original_observe = browser.observe

    def observe():
        if browser.actions:
            events = agent.journal.db.execute("SELECT event_type FROM web_events").fetchall()
            assert ("ACTION_EXECUTED",) in events
        return original_observe()

    browser.observe = observe
    result = agent.run()
    assert result["status"] == "SUCCEEDED"
    assert result["action_count"] == 1
    assert len(result["calls"]) == 2
    assert result["decisions"][0]["raw_answers"] == {"raw": "F1"}
    assert result["history"][0]["text"] == "query"
    assert agent.run() is result
    assert agent.journal.db.execute("SELECT state FROM web_operations").fetchone() == ("VERIFIED",)


def test_wrong_done_and_abstention_never_act(tmp_path):
    agent = build(tmp_path, choices=("DONE",))
    assert agent.run()["reason"] == "DONE_NOT_VERIFIED"
    assert agent.browser.actions == []
    agent.close()
    agent = build(tmp_path)
    agent.model.choose = lambda *args: {"choice": "F1", "accepted": False, "reason": "low confidence"}
    assert agent.run()["status"] == "ESCALATED"
    assert agent.result["reason"] == "DECISION_ABSTAINED"
    assert agent.browser.actions == []


@pytest.mark.parametrize("boundary", ["prepare", "dispatch", "act", "post_observe"])
def test_real_sqlite_crash_boundaries_block_restart(tmp_path, boundary):
    path = tmp_path / "crash.db"
    receipt = tmp_path / "receipt"
    script = r'''
import os, sqlite3, sys
from types import SimpleNamespace
import decision_browser.web.agent as module
from decision_browser.web.agent import WebAgent
from decision_browser.web.journal import WebJournal
path, receipt, boundary = sys.argv[1:]
module.filter_page = lambda task, page, metadata: page
class Browser:
    def observe(self):
        if os.path.exists(receipt):
            assert sqlite3.connect(path).execute("SELECT event_type FROM web_events WHERE event_type='ACTION_EXECUTED'").fetchone()
            os._exit(75)
        return {'actions':[{'id':'C1','kind':'click','node':1}], 'fingerprint':'page'}
    def fresh(self, page, action=None): return True
    def act(self, action, page, text=None):
        assert sqlite3.connect(path).execute('SELECT state FROM web_operations').fetchone()[0] == 'DISPATCHED'
        with open(receipt, 'a') as output: output.write('act\n')
        if boundary == 'act': os._exit(75)
class Model:
    calls = []
    total_tokens = 0
    def choose(self, *args): return {'accepted': True, 'choice':'C1'}
a = WebAgent(SimpleNamespace(goal='click'), Browser(), Model(), path, 'task')
original_prepare = a.journal.prepare
original_transition = a.journal.transition
def prepare(*args):
    original_prepare(*args)
    if boundary == 'prepare': os._exit(75)
def transition(*args):
    original_transition(*args)
    if boundary == 'dispatch' and args[2] == 'DISPATCHED': os._exit(75)
a.journal.prepare = prepare
a.journal.transition = transition
a.run()
'''
    completed = subprocess.run([sys.executable, "-c", script, str(path), str(receipt), boundary], check=False)
    assert completed.returncode == 75
    agent = WebAgent(SimpleNamespace(goal="Search"), Browser(), Model([]), path, "task")
    assert agent.run()["status"] == "UNKNOWN_EFFECT"
    assert agent.browser.observations == 0
    assert agent.model.calls == []
    assert (receipt.read_text().splitlines() if receipt.exists() else []) == ([] if boundary in {"prepare", "dispatch"} else ["act"])
    assert agent.journal.db.execute("SELECT COUNT(*) FROM web_operations").fetchone()[0] == 1


def test_stale_exception_inside_act_is_never_retried(tmp_path):
    agent = build(tmp_path)

    def act(*args, **kwargs):
        agent.browser.actions.append("began")
        raise StalePage("secret from provider")

    agent.browser.act = act
    result = agent.run()
    assert result["status"] == "UNKNOWN_EFFECT"
    assert agent.browser.actions == ["began"]
    assert "secret" not in json.dumps(result)
    assert result["exception_type"] == "StalePage"


def test_stale_after_text_reuses_only_identical_full_context(tmp_path):
    agent = build(tmp_path, choices=("F1", "F1", "DONE"))
    calls = 0

    def fresh(page, action=None):
        nonlocal calls
        calls += 1
        return calls != 3

    agent.browser.fresh = fresh
    agent.run()
    assert len(agent.model.text_calls) == 1
    assert len(agent.browser.actions) == 1


def test_changed_full_context_regenerates_text(tmp_path):
    agent = build(tmp_path, choices=("F1", "F1", "DONE"))
    calls = 0

    def fresh(page, action=None):
        nonlocal calls
        calls += 1
        if len(agent.model.text_calls) == 1 and agent.browser.page.get("title") != "changed":
            agent.browser.page["title"] = "changed"
            return False
        return True

    agent.browser.fresh = fresh
    agent.run()
    assert len(agent.model.text_calls) == 2


def test_deadline_after_helper_prevents_dispatch(tmp_path, monkeypatch):
    agent = build(tmp_path)
    clock = [0]
    monkeypatch.setattr(module.time, "monotonic", lambda: clock[0])
    original = agent.model.field_text

    def field_text(context):
        clock[0] = 121
        return original(context)

    agent.model.field_text = field_text
    assert agent.run()["reason"] == "DEADLINE_EXCEEDED"
    assert agent.browser.actions == []
    assert agent.journal.db.execute("SELECT COUNT(*) FROM web_operations").fetchone() == (0,)


def test_initial_observe_excluded_from_clock_and_done_included(tmp_path, monkeypatch):
    agent = build(tmp_path, choices=("DONE",))
    clock = [0]
    monkeypatch.setattr(module.time, "monotonic", lambda: clock[0])
    original = agent.browser.observe

    def observe():
        clock[0] += 150
        page = original()
        page["complete"] = True
        return page

    agent.browser.observe = observe

    def choose(*args):
        clock[0] += 2
        return {"accepted": True, "choice": "DONE"}

    agent.model.choose = choose
    assert agent.run()["elapsed_ms"] == 2000
    assert agent.result["status"] == "SUCCEEDED"


def test_filter_excludes_unapproved_choice(tmp_path, monkeypatch):
    agent = build(tmp_path)
    monkeypatch.setattr(module, "filter_page", lambda task, page, metadata: {**page, "actions": []})
    assert agent.run()["reason"] == "INVALID_ACTION_CHOICE"
    assert agent.browser.actions == []


@pytest.mark.parametrize("fault", ["dispatch", "execution_event", "finish_event"])
def test_real_sqlite_write_failures_stop_safely(tmp_path, fault):
    agent = build(tmp_path)
    if fault == "dispatch":
        agent.journal.db.execute("CREATE TRIGGER fault BEFORE UPDATE ON web_operations WHEN NEW.state='DISPATCHED' BEGIN SELECT RAISE(ABORT,'injected'); END")
    else:
        event = "ACTION_EXECUTED" if fault == "execution_event" else "RUN_FINISHED"
        agent.journal.db.execute(f"CREATE TRIGGER fault BEFORE INSERT ON web_events WHEN NEW.event_type='{event}' BEGIN SELECT RAISE(ABORT,'injected'); END")
    result = agent.run()
    assert result["status"] == ("UNKNOWN_EFFECT" if fault == "execution_event" else "FAILED")
    assert len(agent.browser.actions) == (0 if fault == "dispatch" else 1)
    if fault != "finish_event":
        restarted = build(tmp_path, choices=())
        assert restarted.run()["status"] == "UNKNOWN_EFFECT"
        assert restarted.browser.actions == []


def test_stale_post_observation_retains_action_receipt_and_stops(tmp_path):
    agent = build(tmp_path)
    original = agent.browser.observe

    def observe():
        if agent.browser.actions:
            raise StalePage("changed after act")
        return original()

    agent.browser.observe = observe
    result = agent.run()
    assert result["status"] == "UNKNOWN_EFFECT"
    assert len(result["history"]) == 1
    assert len(agent.browser.actions) == 1
    assert agent.journal.db.execute("SELECT COUNT(*) FROM web_events WHERE event_type='ACTION_EXECUTED'").fetchone() == (1,)


def test_action_budget_allows_done_but_never_sixty_first_action(tmp_path):
    agent = build(tmp_path, choices=["F1"] * 61)
    result = agent.run()
    assert result["reason"] == "ACTION_BUDGET_EXCEEDED"
    assert len(agent.browser.actions) == 60
    assert len(result["decisions"]) == 61


def test_decision_budget_includes_stale_consumed_choices(tmp_path):
    agent = build(tmp_path, choices=["F1"] * 121)
    agent.browser.fresh = lambda page, action=None: action is None
    result = agent.run()
    assert result["reason"] == "DECISION_BUDGET_EXCEEDED"
    assert len(result["decisions"]) == 120
    assert agent.browser.actions == []


def test_provider_failure_preserves_sanitized_call_and_type(tmp_path):
    agent = build(tmp_path)

    def choose(*args):
        agent.model.calls.append({"http_status": 503, "request_id": "safe-id"})
        raise RuntimeError("Authorization: secret")

    agent.model.choose = choose
    result = agent.run()
    assert result["calls"] == [{"http_status": 503, "request_id": "safe-id"}]
    assert result["exception_type"] == "RuntimeError"
    assert "secret" not in json.dumps(result)


def test_perpetually_stale_preflight_stays_bounded_without_fake_decisions(tmp_path):
    agent = build(tmp_path)
    agent.browser.fresh = lambda *args: False
    result = agent.run()
    assert result["reason"] == "PREFLIGHT_BUDGET_EXCEEDED"
    assert result["decisions"] == []
    assert agent.model.calls == []


@pytest.mark.parametrize("kind", ["fill", "select", "click"])
def test_noop_mutation_stops_and_cannot_replay(tmp_path, kind):
    browser = Browser()
    browser.page["actions"][0].update(kind=kind, value="requested", current_value="old")
    browser.act = lambda action, page, text=None: browser.actions.append((action, text))
    agent = build(tmp_path, choices=("F1", "F1"), browser=browser)
    result = agent.run()
    assert result["status"] == "UNKNOWN_EFFECT"
    assert len(browser.actions) == len(result["decisions"]) == 1
    assert agent.journal.db.execute("SELECT state FROM web_operations").fetchone() == ("UNKNOWN_EFFECT",)
    restarted = build(tmp_path, choices=())
    assert restarted.run()["status"] == "UNKNOWN_EFFECT"
    assert restarted.browser.actions == []


@pytest.mark.parametrize("effect", ["fill", "select", "click_fingerprint", "click_navigation", "click_guard", "scroll_down", "scroll_up", "wait"])
def test_independent_action_effects_allow_verified_progress(tmp_path, effect):
    browser = Browser()
    kind = effect.split("_")[0]
    action = browser.page["actions"][0]
    action.update(kind=kind, value="chosen", current_value="old", delta=-560 if effect == "scroll_up" else 560)
    browser.page.update(scroll={"y": 600}, guards={"1": [1, "combobox", "Name", "old"]}, complete=True)

    def act(action, page, text=None):
        browser.actions.append((action, text))
        if kind == "fill":
            browser.page["actions"][0]["value"] = text
        elif kind == "select":
            browser.page["guards"]["1"][3] = "chosen"
            # Snapshot current_value holds a label, and the selected option is no longer a candidate.
            browser.page["actions"] = []
        elif effect == "click_fingerprint":
            browser.page["fingerprint"] = "changed"
        elif effect == "click_navigation":
            browser.page["page_key"][0] = 2
        elif effect == "click_guard":
            browser.page["guards"]["1"][3] = "changed"
        elif kind == "scroll":
            browser.page["scroll"]["y"] += action["delta"]

    browser.act = act
    agent = build(tmp_path, browser=browser)
    result = agent.run()
    assert result["status"] == "SUCCEEDED"
    assert result["history"][0]["effect_verified"] is True
    assert agent.journal.db.execute("SELECT state FROM web_operations").fetchone() == ("VERIFIED",)


@pytest.mark.parametrize("delta", [0, -100])
def test_scroll_without_movement_in_intended_direction_is_unknown(tmp_path, delta):
    agent = build(tmp_path)
    agent.browser.page.update(scroll={"y": 200})
    agent.browser.page["actions"][0].update(kind="scroll", delta=560)

    def act(action, page, text=None):
        agent.browser.actions.append(action)
        agent.browser.page["scroll"]["y"] += delta

    agent.browser.act = act
    assert agent.run()["status"] == "UNKNOWN_EFFECT"
    assert len(agent.browser.actions) == 1


def test_freshness_failure_after_observed_effect_stops_without_reissue(tmp_path):
    agent = build(tmp_path)
    agent.browser.fresh = lambda page, action=None: not agent.browser.actions
    result = agent.run()
    assert result["status"] == "UNKNOWN_EFFECT"
    assert len(agent.browser.actions) == 1
    assert result["history"][0]["effect_verified"] is False
    assert agent.journal.db.execute("SELECT state FROM web_operations").fetchone() == ("UNKNOWN_EFFECT",)


def test_reused_node_id_in_new_document_does_not_verify_fill(tmp_path):
    agent = build(tmp_path)
    original = agent.browser.act

    def act(*args, **kwargs):
        original(*args, **kwargs)
        agent.browser.page["page_key"][0] = 2

    agent.browser.act = act
    assert agent.run()["status"] == "UNKNOWN_EFFECT"


@pytest.mark.parametrize("verification_failure", ["failed_check", "exception", "stale"])
def test_final_verification_failure_preserves_accepted_done_time(tmp_path, monkeypatch, verification_failure):
    agent = build(tmp_path, choices=("DONE",))
    clock = [0]
    monkeypatch.setattr(module.time, "monotonic", lambda: clock[0])
    original = agent.browser.observe

    def observe():
        clock[0] += 150
        return original()

    def choose(*args):
        clock[0] += 3
        return {"accepted": True, "choice": "DONE"}

    def verify(*args):
        clock[0] += 200
        if verification_failure == "exception":
            raise RuntimeError("verification failed")
        return {"passed": False}

    agent.browser.observe = observe
    agent.model.choose = choose
    agent.browser.fresh = lambda *args: verification_failure != "stale" or agent.browser.observations < 2
    monkeypatch.setattr(module, "verify_task", verify)
    result = agent.run()
    assert result["status"] == "FAILED"
    assert result["elapsed_ms"] == 3000
    assert agent.elapsed() == 3000


def test_truncated_action_surface_is_unsupported_before_model(tmp_path):
    agent = build(tmp_path)
    agent.browser.page["omitted_actions"] = 1
    assert agent.run()["status"] == "UNSUPPORTED"
    assert agent.result["reason"] == "TRUNCATED_ACTIONS"
    assert agent.model.calls == agent.browser.actions == []



def test_select_matching_label_cannot_override_mismatching_dom_value(tmp_path):
    agent = build(tmp_path)
    agent.browser.page["actions"][0].update(kind="select", value="chosen", current_value="chosen")
    agent.browser.page["guards"] = {"1": [1, "combobox", "Name", "different-value"]}
    assert agent.run()["status"] == "UNKNOWN_EFFECT"
    assert len(agent.browser.actions) == 1


def test_delayed_navigation_settles_without_repeating_click(tmp_path):
    agent = build(tmp_path)
    browser = agent.browser
    browser.page["actions"][0].update(kind="click")
    original_observe = browser.observe
    post_reads = 0

    def act(action, page, text=None):
        browser.actions.append(action)

    def observe():
        nonlocal post_reads
        if browser.actions:
            post_reads += 1
            if post_reads == 1:
                raise StalePage("redirect in progress")
            if post_reads >= 4:
                browser.page.update(url="https://example.test/article", fingerprint="article", complete=True,
                                    page_key=[2, "https://example.test/article"])
        return original_observe()

    browser.act, browser.observe = act, observe
    browser.fresh = lambda page, action=None: not browser.actions or post_reads >= 3
    result = agent.run()
    assert result["status"] == "SUCCEEDED"
    assert len(browser.actions) == 1
    receipt = result["history"][0]
    assert receipt["effect_verified"] is True
    assert receipt["observation_count"] == 4
    assert receipt["observation"] == dict(url="https://example.test/article", fingerprint="article",
                                        page_key=[2, "https://example.test/article"])
    assert agent.journal.db.execute("SELECT state FROM web_operations").fetchone() == ("VERIFIED",)


def test_unchanged_post_action_observation_is_bounded_and_retained(tmp_path, monkeypatch):
    agent = build(tmp_path)
    monkeypatch.setattr(module.time, "monotonic", lambda: 0)
    agent.browser.act = lambda *args, **kwargs: agent.browser.actions.append("clicked once")
    result = agent.run()
    assert result["status"] == "UNKNOWN_EFFECT"
    assert agent.browser.actions == ["clicked once"]
    assert agent.browser.observations == 41
    receipt = result["history"][0]
    assert receipt["observation_count"] == 40
    assert receipt["effect_verified"] is False
    assert receipt["observation"]["fingerprint"] == "one"
    event = agent.journal.db.execute("SELECT payload FROM web_events WHERE event_type='ACTION_OBSERVED'").fetchone()
    assert event is not None


@pytest.mark.parametrize("remaining", [120, .1])
def test_post_action_settling_respects_time_and_task_deadline(tmp_path, monkeypatch, remaining):
    agent = build(tmp_path)
    clock, sleeps = [0], []
    monkeypatch.setattr(module.time, "monotonic", lambda: clock[0])

    def sleep(seconds):
        sleeps.append(seconds)
        clock[0] += seconds

    def act(*args, **kwargs):
        agent.browser.actions.append("once")
        clock[0] = 120 - remaining

    monkeypatch.setattr(module.time, "sleep", sleep)
    agent.browser.act = act
    result = agent.run()
    assert result["status"] == "UNKNOWN_EFFECT"
    assert agent.browser.actions == ["once"]
    assert sum(sleeps) <= min(2, remaining) + 1e-9
    assert result["history"][0]["observation_count"] <= 40
    if remaining < 2:
        assert clock[0] == 120
        assert result["history"][0]["observation_count"] < 40


def test_non_stale_post_observation_exception_is_not_polled(tmp_path):
    agent = build(tmp_path)
    original = agent.browser.observe
    reads = 0

    def observe():
        nonlocal reads
        reads += 1
        if agent.browser.actions:
            raise OSError("disconnected")
        return original()

    agent.browser.observe = observe
    result = agent.run()
    assert result["status"] == "UNKNOWN_EFFECT"
    assert result["exception_type"] == "OSError"
    assert reads == 2
    assert len(agent.browser.actions) == 1


def test_proven_unsent_action_reobserves_and_uses_new_operation_step(tmp_path):
    agent = build(tmp_path, choices=("F1", "F1", "DONE"))
    browser = agent.browser
    attempts = []
    original_act = browser.act

    def act(action, page, text=None):
        attempts.append(page["fingerprint"])
        if len(attempts) == 1:
            browser.page.update(fingerprint="fresh", complete=True)
            raise NotDispatched("changed before input")
        assert agent.journal.db.execute("SELECT state FROM web_operations WHERE step=1").fetchone() == ("NOT_SENT",)
        assert agent.journal.db.execute("SELECT state FROM web_operations WHERE step=2").fetchone() == ("DISPATCHED",)
        original_act(action, page, text)

    browser.act = act
    result = agent.run()
    assert result["status"] == "SUCCEEDED"
    assert attempts == ["one", "fresh"]
    assert len(browser.actions) == 1
    assert len(result["history"]) == 1
    assert result["history"][0]["step"] == 2
    assert [d["fingerprint"] for d in result["decisions"]] == ["one", "fresh", "fresh"]
    assert agent.journal.db.execute("SELECT step,state FROM web_operations ORDER BY step").fetchall() == [(1, "NOT_SENT"), (2, "VERIFIED")]
    assert not agent.journal.unresolved("task")
    assert agent.journal.db.execute("SELECT COUNT(*) FROM web_events WHERE event_type='ACTION_NOT_SENT'").fetchone() == (1,)
    # Restart keeps the existing primary-key guard instead of allocating past completed work.
    agent.close()
    restarted = build(tmp_path)
    assert restarted.run()["status"] == "FAILED"
    assert restarted.result["exception_type"] == "IntegrityError"
    assert restarted.browser.actions == []


def test_not_sent_transition_failure_preserves_uncertainty_and_blocks_restart(tmp_path):
    agent = build(tmp_path, choices=("F1", "F1"))
    calls = []
    agent.journal.db.execute("CREATE TRIGGER fault BEFORE UPDATE ON web_operations WHEN NEW.state='NOT_SENT' BEGIN SELECT RAISE(ABORT,'injected'); END")

    def act(*args, **kwargs):
        calls.append("pre-input")
        raise NotDispatched("no input sent")

    agent.browser.act = act
    result = agent.run()
    assert result["status"] == "UNKNOWN_EFFECT"
    assert result["exception_type"] == "IntegrityError"
    assert calls == ["pre-input"]
    assert result["history"] == []
    assert agent.journal.db.execute("SELECT state FROM web_operations").fetchone() == ("UNKNOWN_EFFECT",)
    assert agent.journal.unresolved("task")
    restarted = build(tmp_path)
    assert restarted.run()["status"] == "UNKNOWN_EFFECT"
    assert restarted.browser.actions == []


def test_repeated_known_unsent_attempts_are_bounded_by_decision_budget(tmp_path):
    agent = build(tmp_path, choices=["F1"] * 120)
    attempts = []

    def act(*args, **kwargs):
        attempts.append("not sent")
        raise NotDispatched("stale before input")

    agent.browser.act = act
    result = agent.run()
    assert result["reason"] == "DECISION_BUDGET_EXCEEDED"
    assert len(attempts) == len(result["decisions"]) == 120
    assert agent.browser.actions == result["history"] == []
    assert agent.journal.db.execute("SELECT COUNT(*),COUNT(DISTINCT step) FROM web_operations WHERE state='NOT_SENT'").fetchone() == (120, 120)
    assert not agent.journal.unresolved("task")


def test_not_sent_is_terminal_and_cannot_be_reopened(tmp_path):
    journal = WebJournal(tmp_path / "terminal.db")
    journal.prepare("task", 1, {"kind": "click", "node": 1}, {})
    journal.transition("task", 1, "DISPATCHED")
    journal.transition("task", 1, "NOT_SENT")
    assert not journal.unresolved("task")
    journal.recover("task")
    for state in ("DISPATCHED", "VERIFIED", "UNKNOWN_EFFECT", "NOT_SENT"):
        with pytest.raises(RuntimeError, match="INVALID_OPERATION_TRANSITION"):
            journal.transition("task", 1, state)
    assert journal.db.execute("SELECT state FROM web_operations").fetchone() == ("NOT_SENT",)
    journal.close()


def test_stale_abstention_is_reobserved_before_gate(tmp_path):
    browser=Browser()
    browser.page['complete']=True
    browser.fresh=lambda page,action=None: page['marker']==browser.page['marker']
    agent=build(tmp_path,choices=('DONE',),browser=browser)
    choose=agent.model.choose
    count=0
    def decide(page,goal,history):
        nonlocal count
        count+=1
        if count==1:
            browser.page['marker']='new-document-state'
            return {'choice':'BLOCKED','accepted':False}
        return choose(page,goal,history)
    agent.model.choose=decide
    result=agent.run()
    assert result['status']=='SUCCEEDED'
    assert count==2 and not browser.actions
    assert result['decisions'][0]['accepted'] is False
    agent.close()


def test_replaced_input_requires_same_unique_stable_field_identity():
    old={'page_key':[1], 'metadata':{'1':{'tag':'input','input_type':'search','form_action':'/search','form_method':'get','attributes':{'id':'searchInput'}}}}
    new={'page_key':[1], 'metadata':{'2':deepcopy(old['metadata']['1'])},'actions':[{'kind':'fill','node':2,'value':'query'}]}
    a={'kind':'fill','node':1}
    assert WebAgent.effect_verified(a,old,new,'query')
    new['metadata']['2']['attributes']['id']='different'
    assert not WebAgent.effect_verified(a,old,new,'query')
    new['metadata']['2']['attributes']['id']='searchInput'
    new['actions'].append({'kind':'fill','node':3,'value':'query'})
    new['metadata']['3']=deepcopy(new['metadata']['2'])
    assert not WebAgent.effect_verified(a,old,new,'query')


def test_existing_other_field_cannot_prove_replacement_input():
    metadata={'tag':'input','input_type':'search','attributes':{'id':'searchInput'}}
    before={'page_key':[1],'metadata':{'1':deepcopy(metadata),'2':deepcopy(metadata)}}
    after={'page_key':[1],'metadata':{'2':deepcopy(metadata)},'actions':[{'kind':'fill','node':2,'value':'query'}]}
    assert not WebAgent.effect_verified({'kind':'fill','node':1},before,after,'query')
    before['metadata']['2']['attributes']['id']='someOtherField'
    assert not WebAgent.effect_verified({'kind':'fill','node':1},before,after,'query')


def test_readonly_original_with_duplicate_id_is_not_a_replacement():
    meta={'tag':'input','input_type':'search','attributes':{'id':'searchInput'}}
    before={'page_key':[1],'metadata':{'1':deepcopy(meta)}}
    after={'page_key':[1],'metadata':{'1':deepcopy(meta),'2':deepcopy(meta)},'actions':[{'kind':'click','node':1,'value':''},{'kind':'fill','node':2,'value':'query'}]}
    assert not WebAgent.effect_verified({'kind':'fill','node':1},before,after,'query')


def test_flight_search_waits_for_results_without_model_or_input(tmp_path):
    browser=Browser();browser.page['complete']=True
    browser.page['completion']={'checks':dict(search_page=True,origin=True,destination=True,date=True,year=True,one_way=True,results=False)}
    observe=browser.observe
    def read():
        if browser.observations>=3:browser.page['completion']['checks']['results']=True
        return observe()
    browser.observe=read
    agent=build(tmp_path,choices=('DONE',),browser=browser)
    agent.task.name='flights';agent.history=[{'action':'Search'}]
    assert agent.run()['status']=='SUCCEEDED'
    assert browser.observations>=4 and len(agent.model.calls)==1 and not browser.actions
    agent.close()


def test_flight_results_wait_is_bounded_and_does_not_retry_search(tmp_path):
    b=Browser();b.page['completion']={'checks':dict(search_page=True,origin=True,destination=True,date=True,year=True,one_way=True,results=False)}
    agent=build(tmp_path,choices=('BLOCKED',),browser=b);agent.task.name='flights';agent.history=[{'action':'Search'}]
    choose=agent.model.choose
    def abstain(*args):
        d=choose(*args);d['accepted']=False;return d
    agent.model.choose=abstain
    assert agent.run()['reason']=='DECISION_ABSTAINED'
    assert b.observations==101 and len(agent.model.calls)==1 and not b.actions
    agent.close()
