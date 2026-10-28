from __future__ import annotations

import asyncio
import time
from collections import deque
from typing import Literal

from pydantic import ValidationError

from decision_browser.audit import Journal, PersistenceError
from decision_browser.cache import DecisionCache
from decision_browser.contracts import ActionProposal, Candidate, DecisionRequest, DecisionResult, RunResult, TaskSpec, digest
from decision_browser.drivers.base import BrowserDriver
from decision_browser.executor import BrowserLane, Executor, UnknownEffect, bind_lane
from decision_browser.observer import minimize
from decision_browser.policy import Policy, PolicyError
from decision_browser.providers.base import DecisionProvider
from decision_browser.skills.registry import SkillRegistry
from decision_browser.skills.runner import complete, resolve


class DecisionGateError(RuntimeError):
    pass


def accept_decision(answer: DecisionResult, request: DecisionRequest, candidates: tuple[ActionProposal, ...]) -> ActionProposal | None:
    expected = {c.candidate_id for c in request.candidates}
    probabilities = {p.candidate_id: p.probability for p in answer.probabilities}
    if (len(probabilities) != len(answer.probabilities) or set(probabilities) != expected or
            abs(sum(probabilities.values()) - 1.0) > 1e-3 or answer.choice not in expected or
            answer.requested_model != request.requested_model or answer.resolved_model != request.requested_model or
            answer.question_version != request.question_version or answer.request_digest != request.request_digest):
        raise DecisionGateError("DECISION_CONTRACT_VIOLATION")
    ordered = sorted(probabilities.values(), reverse=True)
    top, second = ordered[0], ordered[1] if len(ordered) > 1 else 0.0
    if probabilities[answer.choice] != top:
        raise DecisionGateError("CHOICE_NOT_MAXIMUM")
    if (answer.choice == "abstain" or top < 0.90 or top - second < 0.20 or answer.state_supported < 0.95):
        return None
    return next((c for c in candidates if c.action_id == answer.choice), None)


class CircuitBreaker:
    def __init__(self):
        self.failures: deque[float] = deque()
        self.open_until = 0.0
        self.probing = False

    def allow(self) -> bool:
        now = time.monotonic()
        if now < self.open_until or self.probing:
            return False
        if self.open_until:
            self.probing = True
        return True

    def success(self):
        self.failures.clear()
        self.open_until, self.probing = 0.0, False

    def failure(self):
        now = time.monotonic()
        self.failures.append(now)
        while self.failures and self.failures[0] < now - 60:
            self.failures.popleft()
        if len(self.failures) >= 5 or self.probing:
            self.open_until = now + 30
        self.probing = False


class Controller:
    def __init__(self, driver: BrowserDriver, provider: DecisionProvider, *, policy: Policy | None = None,
                 journal: Journal | None = None, registry: SkillRegistry | None = None,
                 lane: BrowserLane | None = None, cache: DecisionCache | None = None,
                 mode: Literal["observe-only", "shadow", "enforced"] = "shadow",
                 routing: Literal["rules", "model", "hybrid"] = "rules", model: str = "mock-v1"):
        if mode not in ("observe-only", "shadow", "enforced") or routing not in ("rules", "model", "hybrid"):
            raise ValueError("unknown controller mode or routing")
        self.driver, self.provider = driver, provider
        self.policy = policy or Policy()
        self.journal = journal or Journal()
        self.registry = registry or SkillRegistry.fixture()
        self.lane = bind_lane(driver, lane)
        self.cache, self.mode, self.routing, self.model = cache, mode, routing, model
        self.executor = Executor(driver, self.policy, self.journal, self.lane)
        self.breaker = CircuitBreaker()

    async def run(self, task: TaskSpec) -> RunResult:
        records = ()
        pages: frozenset[int] = frozenset()
        calls, steps, cost = 0, 0, 0
        progress: dict[str, int] = {}

        def result(state, reason=""):
            return RunResult(task_id=task.task_id, state=state, records=records, steps=steps, model_calls=calls, reason=reason)

        try:
            self.policy.validate_task(task)
            if self.journal.unresolved(task.task_id):
                return result("UNKNOWN_EFFECT", "RECONCILIATION_REQUIRED_NO_REPLAY")
            self.journal.event(task.task_id, "task_started", actor=task.principal_id, policy_version=task.policy_version,
                               workflow_version=task.workflow_version, mode=self.mode)
            deadline = time.monotonic() + task.budget.deadline_seconds
            async with asyncio.timeout_at(deadline):
                while steps < task.budget.max_steps:
                    async with self.lane.acquire(task, deadline) as lease:
                        self.lane.validate(lease)
                        obs = await self.driver.observe(task, lease)
                        self.lane.validate(lease)
                        self.policy.check_observation(task, obs)
                        if (obs.session_id, obs.tab_id) != (lease.session_id, lease.tab_id):
                            raise PolicyError("LEASE_OBSERVATION_MISMATCH")
                        self.journal.save_observation(obs)
                        self.journal.event(task.task_id, "observation", observation_id=obs.observation_id,
                                           semantic_digest=obs.semantic_digest, transaction_digest=obs.transaction_digest,
                                           page_index=obs.facts.page_index, navigation_epoch=obs.navigation_epoch)
                        if complete(task, obs, pages, records):
                            self.journal.event(task.task_id, "task_succeeded", record_count=len(records), steps=steps)
                            return result("SUCCEEDED")
                        progress_key = digest({"semantic": obs.semantic_digest, "pages": sorted(pages), "records": len(records)})
                        progress[progress_key] = progress.get(progress_key, 0) + 1
                        if progress[progress_key] > task.budget.max_no_progress_steps:
                            return result("ESCALATED", "NO_PROGRESS_BUDGET")
                        candidates = self.registry.propose(task, obs, pages)
                        for candidate in candidates:
                            self.policy.authorize(task, obs, candidate)
                        if not candidates:
                            return result("ESCALATED", "NO_APPROVED_CANDIDATES")
                        known = resolve(candidates)
                        proposed = known if self.routing != "model" else None
                        cache_key = None
                        if self.cache is not None:
                            cache_key = self.cache.make_key(task, obs, candidates, type(self.provider).__name__, self.model)
                            if proposed is None and self.mode != "shadow":
                                proposed = self.cache.lookup(cache_key, candidates)
                        if proposed is None or self.mode == "shadow":
                            if cost >= task.budget.max_cost_units:
                                return result("ESCALATED", "MODEL_COST_BUDGET")
                            if not self.breaker.allow():
                                if known is None or self.mode != "shadow":
                                    return result("ESCALATED", "MODEL_CIRCUIT_OPEN")
                            else:
                                request_candidates = tuple(Candidate(candidate_id=c.action_id, description=f"Approved {c.kind} fixture operation") for c in candidates)
                                request_candidates += (Candidate(candidate_id="abstain", description="No supported candidate applies"),)
                                request = minimize(task, obs, request_candidates, self.model)
                                calls += 1
                                cost += 1
                                decision_deadline = min(deadline, time.monotonic() + 2.5)
                                try:
                                    async with asyncio.timeout_at(decision_deadline):
                                        answer = await self.provider.decide(request, decision_deadline)
                                    cost += answer.usage
                                    selected = accept_decision(answer, request, candidates)
                                    self.breaker.success()
                                    self.journal.event(task.task_id, "decision", request_digest=request.request_digest,
                                                       resolved_model=answer.resolved_model, selected=answer.choice,
                                                       comparison_match=selected == known, mode=self.mode,
                                                       tenant_id=task.tenant_id, actor=task.principal_id,
                                                       policy_version=task.policy_version, workflow_version=task.workflow_version,
                                                       candidate_set_digest=candidates[0].candidate_set_digest,
                                                       route="decision_model", requested_model=answer.requested_model,
                                                       question_version=answer.question_version,
                                                       probabilities=tuple((p.candidate_id, p.probability) for p in answer.probabilities),
                                                       provider_confidence=answer.provider_confidence,
                                                       state_supported=answer.state_supported, usage=answer.usage,
                                                       input_tokens=answer.input_tokens, output_tokens=answer.output_tokens,
                                                       latency_ms=answer.latency_ms)
                                    if cost > task.budget.max_cost_units:
                                        return result("ESCALATED", "MODEL_COST_BUDGET")
                                    if self.mode != "shadow":
                                        proposed = selected
                                        if proposed is not None and self.cache is not None and cache_key is not None:
                                            self.cache.put(cache_key, proposed)
                                except PersistenceError:
                                    raise
                                except (TimeoutError, DecisionGateError, ValidationError, RuntimeError, ValueError):
                                    self.breaker.failure()
                                    self.journal.event(task.task_id, "decision_unavailable", reason_code="MODEL_UNAVAILABLE_OR_INVALID")
                                    if self.mode != "shadow":
                                        return result("ESCALATED", "MODEL_UNAVAILABLE_OR_INVALID")
                            if self.mode == "shadow":
                                proposed = known
                        if self.mode == "observe-only":
                            self.journal.event(task.task_id, "proposal_only", action_id=proposed.action_id if proposed else "abstain")
                            return result("OBSERVED", "OBSERVE_ONLY_NO_ACTION")
                        if proposed is None:
                            return result("ESCALATED", "DECISION_ABSTAINED")
                        steps += 1
                        executed, after = await self.executor.execute(task, obs, proposed, lease)
                        if proposed.kind == "EXTRACT":
                            if any(r.id in {existing.id for existing in records} for r in executed.records):
                                return result("FAILED", "DUPLICATE_RECORD_ID")
                            records += executed.records
                            pages = pages | {after.facts.page_index}
                            if complete(task, after, pages, records):
                                self.journal.event(task.task_id, "task_succeeded", record_count=len(records), steps=steps)
                                return result("SUCCEEDED")
                return result("ESCALATED", "STEP_BUDGET")
        except UnknownEffect:
            return result("UNKNOWN_EFFECT", "DISPATCH_OR_VERIFICATION_UNCERTAIN")
        except asyncio.CancelledError:
            return result("CANCELLED", "CANCELLED_BEFORE_DISPATCH")
        except TimeoutError:
            return result("ESCALATED", "TASK_DEADLINE")
        except PersistenceError:
            return result("UNKNOWN_EFFECT" if self._unresolved(task.task_id) else "FAILED", "AUDIT_OR_JOURNAL_PERSISTENCE_FAILED")
        except PolicyError as error:
            return result("WAITING_APPROVAL" if str(error) == "HUMAN_REQUIRED" else "ESCALATED", str(error))
        except (ValueError, ValidationError):
            return result("FAILED", "CONTRACT_VIOLATION")
        except Exception:
            return result("ESCALATED", "ADAPTER_OR_RUNTIME_FAILURE")

    def _unresolved(self, task_id: str) -> bool:
        try:
            return self.journal.unresolved(task_id)
        except Exception:
            return True
