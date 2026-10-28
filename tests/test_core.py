import asyncio
import json
import time
from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from decision_browser.audit import Journal, PersistenceError
from decision_browser.cache import DecisionCache
from decision_browser.contracts import ActionProposal, Budget, Candidate, DecisionRequest, DecisionResult, Lease, Parameters, Probability, TaskSpec, digest
from decision_browser.controller import Controller, DecisionGateError, accept_decision
from decision_browser.drivers.mock import DEFAULT_RECORDS, MockBrowserDriver
from decision_browser.executor import BrowserLane
from decision_browser.observer import minimize
from decision_browser.policy import ApprovalAuthority, ApprovalToken, Policy, PolicyError
from decision_browser.providers.mock import MockDecisionProvider
from decision_browser.skills.registry import FIXTURE_REGISTRY_KEY, SkillRegistry


async def observation(driver=None, task=None):
    driver = driver or MockBrowserDriver()
    task = task or TaskSpec(task_id="test")
    lease = Lease(session_id=driver.session_id, tab_id=driver.tab_id, fencing_token=1, expires_monotonic=time.monotonic() + 100)
    return driver, task, lease, await driver.observe(task, lease)


@pytest.mark.parametrize("mode,routing,calls", [("enforced", "rules", 0), ("enforced", "model", 6), ("shadow", "rules", 6)])
async def test_T01_complete_all_pages(mode, routing, calls):
    driver, provider, journal = MockBrowserDriver(), MockDecisionProvider(), Journal()
    result = await Controller(driver, provider, journal=journal, mode=mode, routing=routing).run(TaskSpec(task_id="complete"))
    assert result.state == "SUCCEEDED"
    assert result.records == DEFAULT_RECORDS
    assert result.model_calls == calls
    assert journal.db.execute("SELECT DISTINCT state FROM operations").fetchall() == [("VERIFIED",)]
    assert driver.actions == ["FILL_SEARCH", "EXTRACT", "CLICK_NEXT", "EXTRACT", "CLICK_NEXT", "EXTRACT"]


@pytest.mark.parametrize("query,names", [("Alp", ["Alpha"]), ("a", ["Alpha", "Beta", "Gamma", "Delta"]), ("missing", [])])
async def test_T01_search_filter(query, names):
    result = await Controller(MockBrowserDriver(), MockDecisionProvider(), mode="enforced").run(TaskSpec(task_id=query, query=query))
    assert result.state == "SUCCEEDED"
    assert [record.name for record in result.records] == names


async def test_observe_only_never_dispatches():
    driver = MockBrowserDriver()
    result = await Controller(driver, MockDecisionProvider(), mode="observe-only").run(TaskSpec(task_id="observe"))
    assert result.state == "OBSERVED"
    assert driver.actions == []


async def test_shadow_does_not_execute_model_proposal():
    driver = MockBrowserDriver()
    result = await Controller(driver, MockDecisionProvider(choice="abstain"), mode="shadow").run(TaskSpec(task_id="shadow"))
    assert result.state == "SUCCEEDED"
    assert driver.actions[0] == "FILL_SEARCH"


@pytest.mark.parametrize("payload", [
    {"action_id": "bad", "kind": "SEND", "observation_id": "obs"},
    {"action_id": "bad", "kind": "OBSERVE", "observation_id": "obs", "code": "print(1)"},
    {"action_id": "bad", "kind": "WAIT", "observation_id": "obs", "parameters": {"wait_ms": 10000}},
])
def test_T02_unknown_actions_and_parameters(payload):
    with pytest.raises(ValidationError):
        ActionProposal.model_validate(payload)


def test_T02_contracts_are_strict_frozen_and_finite():
    with pytest.raises(ValidationError):
        Budget(max_steps="10")
    with pytest.raises(ValidationError):
        Probability(candidate_id="x", probability=float("nan"))
    with pytest.raises(ValidationError):
        TaskSpec(task_id="frozen").task_id = "changed"


async def decision_pair():
    driver, task, lease, obs = await observation()
    candidates = SkillRegistry.fixture().propose(task, obs, frozenset())
    request = minimize(task, obs, (Candidate(candidate_id="search", description="search"), Candidate(candidate_id="abstain", description="abstain")), "mock-v1")
    answer = await MockDecisionProvider().decide(request, time.monotonic() + 1)
    return request, answer, candidates


@pytest.mark.parametrize("values", [(0.5, 0.5, 1.0), (0.89, 0.11, 1.0), (0.99, 0.01, 0.94)])
async def test_T03_low_probability_tie_and_unsupported(values):
    request, answer, candidates = await decision_pair()
    top, other, supported = values
    answer = answer.model_copy(update={"probabilities": (Probability(candidate_id="search", probability=top), Probability(candidate_id="abstain", probability=other)), "state_supported": supported})
    assert accept_decision(answer, request, candidates) is None


@pytest.mark.parametrize("mutation", [
    {"choice": "unregistered"}, {"resolved_model": "different-model"}, {"request_digest": "wrong"},
    {"probabilities": (Probability(candidate_id="search", probability=1.0),)},
])
async def test_T02_decision_contract_mismatch(mutation):
    request, answer, candidates = await decision_pair()
    with pytest.raises(DecisionGateError):
        accept_decision(answer.model_copy(update=mutation), request, candidates)


@pytest.mark.parametrize("field,value", [("tab_id", "other"), ("navigation_epoch", 10), ("transaction_digest", "changed"), ("semantic_digest", "changed")])
async def test_T04_changed_observation_never_dispatches(field, value):
    class ChangingDriver(MockBrowserDriver):
        observations = 0
        async def observe(self, task, lease):
            result = await super().observe(task, lease)
            self.observations += 1
            return result.model_copy(update={field: value}) if self.observations > 1 else result
    driver = ChangingDriver()
    result = await Controller(driver, MockDecisionProvider(), mode="enforced").run(TaskSpec(task_id="stale"))
    assert result.state == "ESCALATED"
    assert driver.actions == []


async def test_T05_lane_serializes_and_fences_expired_lease():
    lane, task = BrowserLane("fixture-session", "fixture-tab"), TaskSpec(task_id="lane")
    seen = []
    async def work(number):
        async with lane.acquire(task, time.monotonic() + 5) as lease:
            seen.append((number, "start"))
            await asyncio.sleep(0.001)
            lane.validate(lease)
            seen.append((number, "end"))
            return lease
    first, second = await asyncio.gather(work(1), work(2))
    assert seen == [(1, "start"), (1, "end"), (2, "start"), (2, "end")]
    assert second.fencing_token > first.fencing_token
    with pytest.raises(PolicyError):
        lane.validate(first)
    driver = MockBrowserDriver()
    await driver.observe(task, second)
    with pytest.raises(PolicyError):
        await driver.observe(task, first)


async def test_T06_cache_scope_and_current_candidate_binding():
    driver, task, lease, obs = await observation()
    registry, cache = SkillRegistry.fixture(), DecisionCache(b"k" * 32)
    candidates = registry.propose(task, obs, frozenset())
    key = cache.make_key(task, obs, candidates, "mock", "mock-v1")
    cache.put(key, candidates[0])
    fresh_candidates = registry.propose(task, obs.model_copy(update={"observation_id": "new-observation"}), frozenset())
    assert cache.lookup(key, fresh_candidates).observation_id == "new-observation"
    for changed_task in (task.model_copy(update={"principal_id": "other"}), task.model_copy(update={"tenant_id": "other"}), task.model_copy(update={"query": "other"}), task.model_copy(update={"policy_version": "other"})):
        changed_key = cache.make_key(changed_task, obs, candidates, "mock", "mock-v1")
        assert changed_key != key
        assert cache.lookup(changed_key, candidates) is None
    assert cache.make_key(task, obs.model_copy(update={"transaction_digest": "changed"}), candidates, "mock", "mock-v1") != key
    cache.values[key] = (time.monotonic() - 1, "search")
    assert cache.lookup(key, fresh_candidates) is None


async def test_T07_T12_page_and_task_secrets_do_not_enter_provider_or_audit():
    secret = "Authorization: Bearer super-secret-cookie"
    driver, task, lease, obs = await observation(task=TaskSpec(task_id="privacy", goal=secret, query=secret))
    candidates = SkillRegistry.fixture().propose(task, obs, frozenset())
    request = minimize(task, obs, (Candidate(candidate_id="search", description="Approved search"),), "mock-v1")
    assert secret not in request.model_dump_json()
    journal = Journal()
    await Controller(driver, MockDecisionProvider(), journal=journal, mode="enforced").run(task)
    assert secret not in repr(journal.db.execute("SELECT payload FROM events").fetchall())
    assert secret not in repr(journal.db.execute("SELECT snapshot FROM observations").fetchall())


@pytest.mark.parametrize("url", ["https://fixture.test.evil/", "https://fixture.test@evil.test/", "http://fixture.test/", "https://fixture.test:444/", "https://127.0.0.1/", "https://[::1]/", "https://169.254.169.254/", "https://fixture.test/?token=secret", "https://fixture.test/../../admin"])
def test_T08_unapproved_destinations(url):
    with pytest.raises(PolicyError):
        Policy().check_url(url)


async def test_T09_model_deadline_is_bounded():
    driver = MockBrowserDriver()
    result = await Controller(driver, MockDecisionProvider(delay=1), mode="enforced", routing="model").run(TaskSpec(task_id="timeout", budget=Budget(deadline_seconds=0.01)))
    assert result.state == "ESCALATED"
    assert driver.actions == []


async def test_T10_dispatched_crash_restart_is_not_replayed(tmp_path):
    class CrashDriver(MockBrowserDriver):
        async def execute(self, action, lease):
            await super().execute(action, lease)
            raise ConnectionError("crash after dispatch")
    path, task = tmp_path / "journal.sqlite3", TaskSpec(task_id="crash")
    journal, driver = Journal(path), CrashDriver()
    first = await Controller(driver, MockDecisionProvider(), journal=journal, mode="enforced").run(task)
    assert first.state == "UNKNOWN_EFFECT"
    journal.close()
    journal, second_driver = Journal(path), MockBrowserDriver()
    second = await Controller(second_driver, MockDecisionProvider(), journal=journal, mode="enforced").run(task)
    assert second.state == "UNKNOWN_EFFECT"
    assert second_driver.actions == []


async def test_T10_prepared_restart_and_stable_operation_key(tmp_path):
    driver, task, lease, obs = await observation()
    action = SkillRegistry.fixture().propose(task, obs, frozenset())[0]
    path = tmp_path / "journal.sqlite3"
    journal = Journal(path)
    key = journal.prepare(task, action, lease.fencing_token)
    assert key == journal.operation_key(task, action.model_copy(update={"observation_id": "new", "action_id": "new-id"}))
    with pytest.raises(PersistenceError):
        journal.prepare(task, action, lease.fencing_token)
    journal.close()
    journal = Journal(path)
    assert journal.db.execute("SELECT state FROM operations").fetchone()[0] == "UNKNOWN_EFFECT"


async def test_T11_approval_expiry_tamper_scope_and_replay():
    driver, task, lease, obs = await observation()
    action = SkillRegistry.fixture().propose(task, obs, frozenset())[0]
    authority = ApprovalAuthority(b"k" * 32, Journal())
    token = authority.sign(ApprovalToken(task_id=task.task_id, actor=task.principal_id, action_kind=action.kind,
                                         normalized_payload_digest=digest(action.parameters), destination=obs.origin,
                                         transaction_digest=obs.transaction_digest, policy_version=task.policy_version,
                                         expires_at=time.time() + 10, nonce="fixture-nonce-0001"))
    with pytest.raises(PolicyError):
        authority.consume(token.model_copy(update={"actor": "attacker"}), task, obs, action)
    with pytest.raises(PolicyError):
        authority.consume(authority.sign(token.model_copy(update={"expires_at": time.time() - 1})), task, obs, action)
    with pytest.raises(PolicyError):
        authority.consume(token, task, obs.model_copy(update={"transaction_digest": "other"}), action)
    authority.consume(token, task, obs, action)
    with pytest.raises(PersistenceError):
        authority.consume(token, task, obs, action)


def test_T13_unsigned_or_candidate_skill_rejected(tmp_path):
    artifact = SkillRegistry.fixture()._artifact
    approved = tmp_path / "skills_approved"
    approved.mkdir()
    path = approved / "skill.json"
    path.write_text(artifact.model_dump_json())
    assert SkillRegistry.load(path, FIXTURE_REGISTRY_KEY)
    path.write_text(artifact.model_copy(update={"approval_record": "tampered"}).model_dump_json())
    with pytest.raises(PolicyError):
        SkillRegistry.load(path, FIXTURE_REGISTRY_KEY)
    candidate = tmp_path / "skill_candidates"
    candidate.mkdir()
    path = candidate / "skill.json"
    path.write_text(artifact.model_dump_json())
    with pytest.raises(PolicyError):
        SkillRegistry.load(path, FIXTURE_REGISTRY_KEY)


async def test_T14_model_unavailable_stops_enforced_and_shadow_uses_known_route():
    class BrokenProvider:
        async def decide(self, request, deadline):
            raise RuntimeError("unavailable")
    driver = MockBrowserDriver()
    result = await Controller(driver, BrokenProvider(), mode="enforced", routing="model").run(TaskSpec(task_id="broken"))
    assert result.state == "ESCALATED" and driver.actions == []
    result = await Controller(MockBrowserDriver(), BrokenProvider(), mode="shadow").run(TaskSpec(task_id="shadow-broken"))
    assert result.state == "SUCCEEDED"


async def test_T15_audit_failure_prevents_action():
    class BrokenJournal(Journal):
        def event(self, *args, **kwargs):
            raise PersistenceError("disk full")
    driver = MockBrowserDriver()
    result = await Controller(driver, MockDecisionProvider(), journal=BrokenJournal(), mode="enforced").run(TaskSpec(task_id="audit-full"))
    assert result.state == "FAILED"
    assert driver.actions == []


async def test_false_driver_success_is_not_completion():
    class LyingDriver(MockBrowserDriver):
        async def execute(self, action, lease):
            from decision_browser.contracts import ExecutionResult
            return ExecutionResult(action_id=action.action_id, status="OK")
    result = await Controller(LyingDriver(), MockDecisionProvider(), mode="enforced").run(TaskSpec(task_id="lying"))
    assert result.state == "UNKNOWN_EFFECT"


async def test_step_cost_budgets_stop_before_extra_dispatch():
    driver = MockBrowserDriver()
    result = await Controller(driver, MockDecisionProvider(), mode="enforced").run(TaskSpec(task_id="steps", budget=Budget(max_steps=1)))
    assert result.state == "ESCALATED" and len(driver.actions) == 1
    driver = MockBrowserDriver()
    result = await Controller(driver, MockDecisionProvider(), mode="enforced", routing="model").run(TaskSpec(task_id="cost", budget=Budget(max_cost_units=0)))
    assert result.state == "ESCALATED" and driver.actions == []


async def test_observation_replay_and_metadata_audit():
    journal = Journal()
    result = await Controller(MockBrowserDriver(), MockDecisionProvider(), journal=journal, mode="enforced", routing="model").run(TaskSpec(task_id="replay"))
    assert result.state == "SUCCEEDED"
    snapshots = journal.load_observations("replay")
    assert len(snapshots) == 18
    assert snapshots[0].page_kind == "search"
    assert snapshots[-1].facts.page_index == 3
    row = journal.db.execute("SELECT payload FROM events WHERE event_type='decision' LIMIT 1").fetchone()
    payload = json.loads(row[0])
    assert payload["actor"] == "fixture-user"
    assert payload["candidate_set_digest"]
    assert payload["probabilities"] == [["search", 1.0], ["abstain", 0.0]]
    assert payload["policy_version"] == "poc-v1"


async def test_cancellation_during_action_is_unknown():
    started = asyncio.Event()
    class BlockingDriver(MockBrowserDriver):
        async def execute(self, action, lease):
            await super().execute(action, lease)
            started.set()
            await asyncio.Event().wait()
    journal, driver = Journal(), BlockingDriver()
    running = asyncio.create_task(Controller(driver, MockDecisionProvider(), journal=journal, mode="enforced").run(TaskSpec(task_id="cancel-dispatch")))
    await started.wait()
    running.cancel()
    result = await running
    assert result.state == "UNKNOWN_EFFECT"
    assert journal.unresolved("cancel-dispatch")


async def test_cancellation_during_decision_stops_before_action():
    started = asyncio.Event()
    class BlockingProvider:
        async def decide(self, request, deadline):
            started.set()
            await asyncio.Event().wait()
    driver = MockBrowserDriver()
    running = asyncio.create_task(Controller(driver, BlockingProvider(), mode="enforced", routing="model").run(TaskSpec(task_id="cancel-decision")))
    await started.wait()
    running.cancel()
    result = await running
    assert result.state == "CANCELLED"
    assert driver.actions == []


async def test_T15_after_dispatch_audit_failure_remains_uncertain():
    class BrokenJournal(Journal):
        def event(self, task_id, event_type, **payload):
            if event_type == "action_verified":
                raise PersistenceError("disk full")
            super().event(task_id, event_type, **payload)
    driver = MockBrowserDriver()
    result = await Controller(driver, MockDecisionProvider(), journal=BrokenJournal(), mode="enforced").run(TaskSpec(task_id="post-action-audit"))
    assert result.state == "UNKNOWN_EFFECT"
    assert len(driver.actions) == 1


async def test_T15_decision_audit_failure_stops_shadow_before_dispatch():
    class BrokenDecisionAudit(Journal):
        def event(self, task_id, event_type, **payload):
            if event_type == "decision":
                raise PersistenceError("disk full")
            super().event(task_id, event_type, **payload)
    driver = MockBrowserDriver()
    result = await Controller(driver, MockDecisionProvider(), journal=BrokenDecisionAudit(), mode="shadow").run(TaskSpec(task_id="decision-audit"))
    assert result.state == "FAILED"
    assert driver.actions == []


def test_T05_controllers_share_one_driver_lane():
    driver = MockBrowserDriver()
    first = Controller(driver, MockDecisionProvider())
    second = Controller(driver, MockDecisionProvider())
    assert first.lane is second.lane
    with pytest.raises(ValueError):
        Controller(driver, MockDecisionProvider(), lane=BrowserLane("fixture-session", "fixture-tab"))


async def test_completion_requires_at_least_one_verified_extraction():
    class EmptyUncollectedDriver(MockBrowserDriver):
        async def observe(self, task, lease):
            from decision_browser.contracts import Facts
            obs = await super().observe(task, lease)
            return obs.model_copy(update={"page_kind": "results", "facts": Facts(searched=True, page_index=0)})
    driver = EmptyUncollectedDriver()
    result = await Controller(driver, MockDecisionProvider(), mode="enforced").run(TaskSpec(task_id="false-empty"))
    assert result.state != "SUCCEEDED"


def test_circuit_breaker_opens_and_allows_only_one_probe():
    from decision_browser.controller import CircuitBreaker
    breaker = CircuitBreaker()
    for _ in range(5):
        assert breaker.allow()
        breaker.failure()
    assert not breaker.allow()
    breaker.open_until = time.monotonic() - 1
    assert breaker.allow()
    assert not breaker.allow()
    breaker.success()
    assert breaker.allow()


async def test_model_cache_reuses_choice_but_verifies_current_observation():
    cache = DecisionCache(b"k" * 32)
    provider = MockDecisionProvider()
    first = await Controller(MockBrowserDriver(), provider, mode="enforced", routing="model", cache=cache).run(TaskSpec(task_id="cache-first"))
    second = await Controller(MockBrowserDriver(), provider, mode="enforced", routing="model", cache=cache).run(TaskSpec(task_id="cache-second"))
    assert first.state == second.state == "SUCCEEDED"
    assert first.model_calls == 6
    assert second.model_calls == 0
    assert first.records == second.records == DEFAULT_RECORDS


async def test_audit_jsonl_export_contains_structured_events(tmp_path):
    journal = Journal()
    result = await Controller(MockBrowserDriver(), MockDecisionProvider(), journal=journal, mode="enforced").run(TaskSpec(task_id="jsonl"))
    assert result.state == "SUCCEEDED"
    output = tmp_path / "audit.jsonl"
    journal.export_events(output)
    events = [json.loads(line) for line in output.read_text().splitlines()]
    assert events[0]["event_type"] == "task_started"
    assert events[-1]["event_type"] == "task_succeeded"
    assert all(event["task_id"] == "jsonl" and event["schema_version"] == "1.0" for event in events)
    assert sum(event["event_type"] == "action_verified" for event in events) == 6
    with pytest.raises(PersistenceError):
        journal.export_events(tmp_path / "absent-directory" / "audit.jsonl")


async def test_exact_step_budget_permits_verified_completion():
    result = await Controller(MockBrowserDriver(), MockDecisionProvider(), mode="enforced").run(TaskSpec(task_id="exact-budget", budget=Budget(max_steps=6)))
    assert result.state == "SUCCEEDED"
    assert result.steps == 6 and result.records == DEFAULT_RECORDS


async def test_stale_final_observation_cannot_complete():
    class StaleFinalDriver(MockBrowserDriver):
        async def observe(self, task, lease):
            obs = await super().observe(task, lease)
            if len(self.actions) == 6:
                return obs.model_copy(update={"captured_monotonic": time.monotonic() - 100})
            return obs
    result = await Controller(StaleFinalDriver(), MockDecisionProvider(), mode="enforced").run(TaskSpec(task_id="stale-final"))
    assert result.state == "UNKNOWN_EFFECT"


async def test_T12_persisted_snapshot_redacts_query_and_page_text():
    from decision_browser.contracts import Record
    secret = "Authorization: Bearer token123"
    driver, task, lease, obs = await observation(task=TaskSpec(task_id="snapshot-redaction", query=secret))
    facts = obs.facts.model_copy(update={"query": secret, "records": (Record(id=secret, name=secret, updated_at=secret),)})
    elements = tuple(element.model_copy(update={"element_ref": secret, "role": secret, "name": secret}) for element in obs.elements)
    journal = Journal()
    journal.save_observation(obs.model_copy(update={"facts": facts, "elements": elements}))
    snapshot = journal.db.execute("SELECT snapshot FROM observations").fetchone()[0]
    assert secret not in snapshot
    reloaded = journal.load_observations(task.task_id)[0]
    assert reloaded.redaction_profile == "persisted-redacted-v1"
    assert reloaded.facts.records[0].name == "[redacted]"
