from __future__ import annotations

import hmac
import time
from urllib.parse import urlsplit

from pydantic import Field

from decision_browser.audit import Journal
from decision_browser.contracts import ActionProposal, Contract, Observation, TaskSpec, canonical, digest


class PolicyError(RuntimeError):
    pass


class Policy:
    version = "poc-v1"
    origins = ("https://fixture.test",)

    def validate_task(self, task: TaskSpec):
        if task.allowed_origins != self.origins or task.policy_version != self.version:
            raise PolicyError("UNAPPROVED_POLICY_OR_ORIGIN")
        if task.data_classification != "SYNTHETIC":
            raise PolicyError("MVP_SYNTHETIC_ONLY")
        if task.workflow_version != "search-results-v1":
            raise PolicyError("UNAPPROVED_WORKFLOW")

    def check_url(self, url: str):
        parsed = urlsplit(url)
        try:
            port = parsed.port
        except ValueError as error:
            raise PolicyError("INVALID_PORT") from error
        if (parsed.scheme != "https" or parsed.hostname != "fixture.test" or
                parsed.username is not None or parsed.password is not None or
                port not in (None, 443) or parsed.fragment or parsed.query or
                parsed.path not in ("", "/", "/search", "/results")):
            raise PolicyError("UNAPPROVED_DESTINATION")

    def check_observation(self, task: TaskSpec, obs: Observation):
        now = time.monotonic()
        if obs.captured_monotonic > now or (now - obs.captured_monotonic) * 1000 > obs.valid_for_ms:
            raise PolicyError("STALE_OBSERVATION")
        self.validate_task(task)
        self.check_url(obs.origin)
        if obs.origin != self.origins[0] or obs.task_id != task.task_id:
            raise PolicyError("OBSERVATION_SCOPE_MISMATCH")
        if obs.truncated or not obs.facts.supported:
            raise PolicyError("UNSUPPORTED_OR_TRUNCATED_STATE")
        if obs.facts.requires_human or obs.page_kind == "login":
            raise PolicyError("HUMAN_REQUIRED")

    def authorize(self, task: TaskSpec, obs: Observation, action: ActionProposal) -> str:
        self.check_observation(task, obs)
        if action.skill_version != task.workflow_version:
            raise PolicyError("SKILL_VERSION_MISMATCH")
        expected_risk = "R0" if action.kind in ("OBSERVE", "EXTRACT", "WAIT") else "R1"
        if action.risk_class != expected_risk:
            raise PolicyError("RISK_CLASS_MISMATCH")
        p = action.parameters
        if action.kind == "FILL_SEARCH":
            if p.query != task.query or obs.facts.searched or obs.page_kind != "search" or action.element_ref != "search":
                raise PolicyError("SEARCH_PRECONDITION_FAILED")
            if p.url is not None or p.wait_ms is not None or p.page_index is not None:
                raise PolicyError("INVALID_SEARCH_PARAMETERS")
        elif action.kind in ("EXTRACT", "CLICK_NEXT"):
            if (obs.page_kind != "results" or not obs.facts.searched or obs.facts.query != task.query or
                    p.page_index != obs.facts.page_index):
                raise PolicyError("RESULTS_PRECONDITION_FAILED")
            if p.query is not None or p.url is not None or p.wait_ms is not None:
                raise PolicyError("INVALID_RESULTS_PARAMETERS")
            if action.kind == "CLICK_NEXT" and (not obs.facts.has_next or action.element_ref != "next-page"):
                raise PolicyError("NEXT_PRECONDITION_FAILED")
        elif action.kind == "NAVIGATE":
            if p.url is None:
                raise PolicyError("MISSING_URL")
            self.check_url(p.url)
            if p.query is not None or p.wait_ms is not None or p.page_index is not None:
                raise PolicyError("INVALID_NAVIGATION_PARAMETERS")
        elif action.kind == "WAIT":
            if p.wait_ms is None or p.query is not None or p.url is not None or p.page_index is not None:
                raise PolicyError("INVALID_WAIT_PARAMETERS")
        elif p.query is not None or p.url is not None or p.wait_ms is not None or p.page_index is not None:
            raise PolicyError("INVALID_OBSERVE_PARAMETERS")
        if action.element_ref is not None:
            if not any(e.element_ref == action.element_ref and e.enabled and e.frame_ref == "main" for e in obs.elements):
                raise PolicyError("TARGET_NOT_ENABLED")
        return "allow"


class ApprovalToken(Contract):
    task_id: str
    actor: str
    action_kind: str
    normalized_payload_digest: str
    destination: str
    transaction_digest: str
    policy_version: str
    expires_at: float
    nonce: str = Field(min_length=16)
    signature: str = ""


class ApprovalAuthority:
    """Fixture-only token verification; it never enables R2/R3 execution."""

    def __init__(self, key: bytes, journal: Journal):
        if len(key) < 32:
            raise ValueError("approval key must contain at least 32 bytes")
        self.key, self.journal = key, journal

    def sign(self, token: ApprovalToken) -> ApprovalToken:
        signature = hmac.digest(self.key, canonical(token.model_dump(exclude={"signature"})), "sha256").hex()
        return token.model_copy(update={"signature": signature})

    def consume(self, token: ApprovalToken, task: TaskSpec, obs: Observation, action: ActionProposal):
        expected = self.sign(token).signature
        if not hmac.compare_digest(expected, token.signature) or token.expires_at <= time.time():
            raise PolicyError("INVALID_OR_EXPIRED_APPROVAL")
        if (token.task_id != task.task_id or token.actor != task.principal_id or
                token.action_kind != action.kind or token.normalized_payload_digest != digest(action.parameters) or
                token.destination != obs.origin or token.transaction_digest != obs.transaction_digest or
                token.policy_version != task.policy_version):
            raise PolicyError("APPROVAL_BINDING_MISMATCH")
        self.journal.consume_nonce(token.nonce)
