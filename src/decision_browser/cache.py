import hmac
import time
from collections import OrderedDict

from decision_browser.contracts import ActionProposal, Observation, TaskSpec, canonical


class DecisionCache:
    def __init__(self, key: bytes, ttl_seconds: float = 30.0, capacity: int = 256):
        if len(key) < 32 or not 0 < ttl_seconds <= 30 or capacity < 1:
            raise ValueError("invalid cache configuration")
        self.key, self.ttl, self.capacity = key, ttl_seconds, capacity
        self.values: OrderedDict[str, tuple[float, str]] = OrderedDict()

    def make_key(self, task: TaskSpec, obs: Observation, candidates: tuple[ActionProposal, ...], provider: str, model: str) -> str:
        value = {"tenant": task.tenant_id, "actor": task.principal_id, "goal": task.goal, "query": task.query,
                 "workflow": task.workflow_version, "policy": task.policy_version, "classification": task.data_classification,
                 "origins": task.allowed_origins, "phase": obs.phase, "skill": tuple(a.skill_version for a in candidates),
                 "provider": provider, "model": model, "question": "next-action-v1", "normalizer": "public-fixture-v1",
                 "semantic": obs.semantic_digest, "transaction": obs.transaction_digest, "facts": obs.facts.model_dump(mode="json"),
                 "candidates": tuple(a.model_dump(mode="json", exclude={"observation_id", "candidate_set_digest"}) for a in candidates)}
        return hmac.digest(self.key, canonical(value), "sha256").hex()

    def lookup(self, key: str, candidates: tuple[ActionProposal, ...]) -> ActionProposal | None:
        value = self.values.get(key)
        if value is None:
            return None
        expires, candidate_id = value
        if expires <= time.monotonic():
            del self.values[key]
            return None
        return next((a for a in candidates if a.action_id == candidate_id and a.risk_class in ("R0", "R1")), None)

    def put(self, key: str, action: ActionProposal):
        if action.risk_class not in ("R0", "R1"):
            return
        self.values[key] = (time.monotonic() + self.ttl, action.action_id)
        self.values.move_to_end(key)
        while len(self.values) > self.capacity:
            self.values.popitem(last=False)
