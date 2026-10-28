from __future__ import annotations

import hmac
import json
from pathlib import Path

from typing import Literal

from decision_browser.contracts import ActionProposal, Contract, Observation, Parameters, TaskSpec, canonical, digest
from decision_browser.policy import PolicyError

# This key authenticates bundled synthetic fixtures only, not deployment approvals.
FIXTURE_REGISTRY_KEY = b"fixture-registry-test-key-32-bytes-only"


class SkillArtifact(Contract):
    schema_version: Literal["1.0"]
    skill_id: Literal["search-results"]
    version: Literal["search-results-v1"]
    status: Literal["approved"]
    implementation: Literal["builtin-search-results-v1"]
    approval_record: str
    allowed_origins: tuple[Literal["https://fixture.test"], ...]
    signature: str


class SkillRegistry:
    def __init__(self, artifact: SkillArtifact, key: bytes):
        expected = hmac.digest(key, canonical(artifact.model_dump(mode="json", exclude={"signature"})), "sha256").hex()
        if not hmac.compare_digest(expected, artifact.signature) or not artifact.approval_record:
            raise PolicyError("UNAPPROVED_REGISTRY_ARTIFACT")
        if artifact.allowed_origins != ("https://fixture.test",):
            raise PolicyError("REGISTRY_ORIGIN_MISMATCH")
        self._artifact = artifact

    @classmethod
    def load(cls, path: str | Path, key: bytes) -> SkillRegistry:
        path = Path(path)
        if path.parent.name != "skills_approved" or path.is_symlink():
            raise PolicyError("CANDIDATE_ARTIFACT_NOT_EXECUTABLE")
        return cls(SkillArtifact.model_validate_json(path.read_bytes()), key)

    @classmethod
    def fixture(cls) -> SkillRegistry:
        payload = {"schema_version": "1.0", "skill_id": "search-results", "version": "search-results-v1",
                   "status": "approved", "implementation": "builtin-search-results-v1",
                   "approval_record": "synthetic-fixture-only-v1", "allowed_origins": ("https://fixture.test",)}
        signature = hmac.digest(FIXTURE_REGISTRY_KEY, canonical(payload), "sha256").hex()
        return cls(SkillArtifact(**payload, signature=signature), FIXTURE_REGISTRY_KEY)

    def propose(self, task: TaskSpec, obs: Observation, collected_pages: frozenset[int]) -> tuple[ActionProposal, ...]:
        if task.workflow_version != self._artifact.version or obs.page_kind not in ("search", "results"):
            return ()
        if not obs.facts.searched:
            action = ActionProposal(action_id="search", kind="FILL_SEARCH", parameters=Parameters(query=task.query),
                                    element_ref="search", observation_id=obs.observation_id,
                                    preconditions=("fixture-search",), postconditions=("query-applied",))
        elif obs.facts.page_index not in collected_pages:
            action = ActionProposal(action_id="extract", kind="EXTRACT", parameters=Parameters(page_index=obs.facts.page_index),
                                    risk_class="R0", retry_class="READ_ONLY", observation_id=obs.observation_id,
                                    preconditions=("results-ready",), postconditions=("records-match-observation",))
        elif obs.facts.has_next:
            action = ActionProposal(action_id="next-page", kind="CLICK_NEXT", parameters=Parameters(page_index=obs.facts.page_index),
                                    element_ref="next-page", observation_id=obs.observation_id,
                                    preconditions=("current-page-saved",), postconditions=("page-index-incremented",))
        else:
            return ()
        candidates_digest = digest((action.model_dump(mode="json", exclude={"observation_id", "candidate_set_digest"}),))
        return (action.model_copy(update={"candidate_set_digest": candidates_digest}),)
