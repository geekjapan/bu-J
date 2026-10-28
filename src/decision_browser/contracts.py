from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class Contract(BaseModel):
    model_config = ConfigDict(strict=True, frozen=True, extra="forbid", allow_inf_nan=False)


def canonical(value: object) -> bytes:
    if isinstance(value, BaseModel):
        value = value.model_dump(mode="json")
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def digest(value: object) -> str:
    return hashlib.sha256(canonical(value)).hexdigest()


class Budget(Contract):
    max_steps: int = Field(default=200, ge=1, le=10000)
    deadline_seconds: float = Field(default=300.0, gt=0, le=3600)
    max_cost_units: int = Field(default=100, ge=0)
    max_no_progress_steps: int = Field(default=3, ge=1)


class TaskSpec(Contract):
    schema_version: Literal["1.0"] = "1.0"
    task_id: str = Field(min_length=1)
    tenant_id: str = "fixture"
    principal_id: str = "fixture-user"
    goal: str = "Collect fixture search results"
    phase: Literal["search", "collect"] = "search"
    allowed_origins: tuple[str, ...] = ("https://fixture.test",)
    data_classification: Literal["PUBLIC", "SYNTHETIC", "CONFIDENTIAL"] = "SYNTHETIC"
    policy_version: str = "poc-v1"
    workflow_version: str = "search-results-v1"
    query: str = Field(default="", max_length=200)
    budget: Budget = Budget()


class Record(Contract):
    id: str
    name: str
    updated_at: str


class Element(Contract):
    element_ref: str
    role: str
    name: str
    enabled: bool = True
    frame_ref: Literal["main"] = "main"


class Facts(Contract):
    page_index: int = Field(default=0, ge=0)
    has_next: bool = False
    searched: bool = False
    query: str = ""
    records: tuple[Record, ...] = ()
    total_records: int = Field(default=0, ge=0)
    supported: bool = True
    requires_human: bool = False


class Observation(Contract):
    schema_version: Literal["1.0"] = "1.0"
    task_id: str
    session_id: str
    tab_id: str
    navigation_epoch: int = Field(ge=0)
    observation_id: str
    captured_at: datetime
    captured_monotonic: float
    valid_for_ms: int = Field(default=2000, gt=0, le=10000)
    origin: str
    page_kind: Literal["search", "results", "unsupported", "login"]
    phase: Literal["search", "collect"]
    semantic_digest: str
    transaction_digest: str
    elements: tuple[Element, ...] = ()
    facts: Facts
    redaction_profile: Literal["public-fixture-v1", "persisted-redacted-v1"] = "public-fixture-v1"
    truncated: bool = False

    @model_validator(mode="after")
    def utc_timestamp(self):
        if self.captured_at.utcoffset() is None or self.captured_at.utcoffset().total_seconds() != 0:
            raise ValueError("captured_at must use UTC")
        return self


ActionKind = Literal["OBSERVE", "EXTRACT", "NAVIGATE", "WAIT", "CLICK_NEXT", "FILL_SEARCH"]


class Parameters(Contract):
    query: str | None = Field(default=None, max_length=200)
    url: str | None = None
    wait_ms: int | None = Field(default=None, ge=1, le=1000)
    page_index: int | None = Field(default=None, ge=0)


class ActionProposal(Contract):
    action_id: str
    kind: ActionKind
    skill_version: str = "search-results-v1"
    parameters: Parameters = Parameters()
    element_ref: str | None = None
    preconditions: tuple[str, ...] = ()
    postconditions: tuple[str, ...] = ()
    risk_class: Literal["R0", "R1", "R2", "R3"] = "R1"
    retry_class: Literal["READ_ONLY", "RECONCILE"] = "RECONCILE"
    observation_id: str
    candidate_set_digest: str = ""


class Lease(Contract):
    session_id: str
    tab_id: str
    fencing_token: int = Field(ge=1)
    expires_monotonic: float


class ExecutionResult(Contract):
    action_id: str
    status: Literal["OK", "FAILED", "UNKNOWN"]
    records: tuple[Record, ...] = ()
    reason: str = ""


class Candidate(Contract):
    candidate_id: str
    description: str


class DecisionRequest(Contract):
    schema_version: Literal["1.0"] = "1.0"
    task_id: str
    goal: str
    phase: str
    page_kind: str
    page_index: int
    has_next: bool
    candidates: tuple[Candidate, ...]
    requested_model: str = "mock-v1"
    question_version: str = "next-action-v1"
    request_digest: str = ""


class Probability(Contract):
    candidate_id: str
    probability: float = Field(ge=0, le=1)


class DecisionResult(Contract):
    provider: str
    requested_model: str
    resolved_model: str
    question_version: str = "next-action-v1"
    choice: str
    probabilities: tuple[Probability, ...]
    provider_confidence: float = Field(default=1.0, ge=0, le=1)
    state_supported: float = Field(default=1.0, ge=0, le=1)
    latency_ms: float = Field(default=0.0, ge=0)
    usage: int = Field(default=0, ge=0)
    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    request_digest: str


class RunResult(Contract):
    task_id: str
    state: Literal["SUCCEEDED", "FAILED", "CANCELLED", "WAITING_APPROVAL", "ESCALATED", "UNKNOWN_EFFECT", "OBSERVED"]
    records: tuple[Record, ...] = ()
    steps: int = 0
    model_calls: int = 0
    reason: str = ""
