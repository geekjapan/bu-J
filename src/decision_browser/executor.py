from __future__ import annotations

import asyncio
import time
from contextlib import asynccontextmanager
from weakref import WeakKeyDictionary

from decision_browser.audit import Journal, PersistenceError
from decision_browser.contracts import ActionProposal, ExecutionResult, Lease, Observation, TaskSpec
from decision_browser.drivers.base import BrowserDriver
from decision_browser.observer import require_fresh
from decision_browser.policy import Policy, PolicyError


class UnknownEffect(RuntimeError):
    pass


class BrowserLane:
    def __init__(self, session_id: str, tab_id: str):
        self.session_id, self.tab_id = session_id, tab_id
        self.lock = asyncio.Lock()
        self.fencing_token = 0
        self.active: Lease | None = None

    @asynccontextmanager
    async def acquire(self, task: TaskSpec, deadline: float):
        async with self.lock:
            self.fencing_token += 1
            lease = Lease(session_id=self.session_id, tab_id=self.tab_id, fencing_token=self.fencing_token,
                          expires_monotonic=min(deadline, time.monotonic() + task.budget.deadline_seconds))
            self.active = lease
            try:
                yield lease
            finally:
                self.active = None

    def validate(self, lease: Lease):
        if self.active != lease or lease.expires_monotonic <= time.monotonic():
            raise PolicyError("LEASE_EXPIRED_OR_FENCED")


_driver_lanes: WeakKeyDictionary = WeakKeyDictionary()


def bind_lane(driver: BrowserDriver, requested: BrowserLane | None = None) -> BrowserLane:
    existing = _driver_lanes.get(driver)
    if existing is not None:
        if requested is not None and existing is not requested:
            raise ValueError("driver already belongs to another browser lane")
        return existing
    lane = requested or BrowserLane(getattr(driver, "session_id", "fixture-session"), getattr(driver, "tab_id", "fixture-tab"))
    _driver_lanes[driver] = lane
    return lane


class Verifier:
    def check(self, task: TaskSpec, before: Observation, after: Observation, result: ExecutionResult, action: ActionProposal) -> bool:
        if result.status != "OK" or result.action_id != action.action_id:
            return False
        if (after.task_id != task.task_id or after.session_id != before.session_id or after.tab_id != before.tab_id or
                after.origin != before.origin or not after.facts.supported or after.facts.requires_human or after.truncated):
            return False
        if action.kind == "FILL_SEARCH":
            return (after.facts.searched and after.facts.query == task.query and after.page_kind == "results" and
                    after.facts.page_index == 1 and after.navigation_epoch > before.navigation_epoch)
        if action.kind == "CLICK_NEXT":
            return (after.facts.page_index == before.facts.page_index + 1 and after.facts.query == task.query and
                    after.navigation_epoch > before.navigation_epoch and after.page_kind == "results")
        if action.kind == "EXTRACT":
            return (before.semantic_digest == after.semantic_digest and before.transaction_digest == after.transaction_digest and
                    before.navigation_epoch == after.navigation_epoch and result.records == after.facts.records and
                    len({r.id for r in result.records}) == len(result.records))
        if action.kind in ("OBSERVE", "WAIT"):
            return True
        return after.origin == before.origin


class Executor:
    def __init__(self, driver: BrowserDriver, policy: Policy, journal: Journal, lane: BrowserLane):
        self.driver, self.policy, self.journal, self.lane = driver, policy, journal, lane
        self.verifier = Verifier()

    async def execute(self, task: TaskSpec, before: Observation, action: ActionProposal, lease: Lease) -> tuple[ExecutionResult, Observation]:
        self.lane.validate(lease)
        fresh = await self.driver.observe(task, lease)
        self.lane.validate(lease)
        require_fresh(before, fresh, action.element_ref)
        self.policy.authorize(task, fresh, action)
        self.journal.save_observation(fresh)
        if (fresh.session_id, fresh.tab_id) != (lease.session_id, lease.tab_id):
            raise PolicyError("LEASE_OBSERVATION_MISMATCH")
        if action.observation_id != before.observation_id:
            raise PolicyError("PROPOSAL_OBSERVATION_MISMATCH")
        self.journal.event(task.task_id, "action_prepared", action_id=action.action_id,
                           actor=task.principal_id, policy_version=task.policy_version, skill_version=action.skill_version,
                           observation_id=fresh.observation_id, fencing_token=lease.fencing_token)
        key = self.journal.prepare(task, action, lease.fencing_token)
        self.journal.transition(key, "DISPATCHED")
        try:
            self.lane.validate(lease)
            result = await self.driver.execute(action, lease)
            after = await self.driver.observe(task, lease)
            self.lane.validate(lease)
            self.policy.check_observation(task, after)
            self.journal.save_observation(after)
            verified = self.verifier.check(task, fresh, after, result, action)
            self.journal.event(task.task_id, "action_verified" if verified else "action_unknown", action_id=action.action_id,
                               operation_key=key, policy_version=task.policy_version, actor=task.principal_id)
            self.journal.transition(key, "VERIFIED" if verified else "UNKNOWN_EFFECT", "POSTCONDITION_VERIFIED" if verified else "POSTCONDITION_UNKNOWN")
            if not verified:
                raise UnknownEffect("POSTCONDITION_UNKNOWN")
            return result, after
        except BaseException as error:
            try:
                self.journal.transition(key, "UNKNOWN_EFFECT", "DISPATCH_INTERRUPTED")
            except PersistenceError:
                pass
            if isinstance(error, (KeyboardInterrupt, SystemExit)):
                raise
            raise UnknownEffect("DISPATCH_OR_VERIFICATION_UNCERTAIN") from error
