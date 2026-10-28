import time

from decision_browser.contracts import Candidate, DecisionRequest, Observation, TaskSpec, digest
from decision_browser.policy import PolicyError


def require_fresh(before: Observation, fresh: Observation, element_ref: str | None):
    if any((time.monotonic() - obs.captured_monotonic) * 1000 > obs.valid_for_ms or obs.captured_monotonic > time.monotonic() for obs in (before, fresh)):
        raise PolicyError("STALE_OBSERVATION")
    fields = ("task_id", "session_id", "tab_id", "navigation_epoch", "origin", "semantic_digest", "transaction_digest", "phase", "page_kind", "facts")
    if any(getattr(before, key) != getattr(fresh, key) for key in fields):
        raise PolicyError("OBSERVATION_CHANGED")
    if element_ref is not None:
        old = tuple(e for e in before.elements if e.element_ref == element_ref)
        new = tuple(e for e in fresh.elements if e.element_ref == element_ref)
        if not old or old != new or not new[0].enabled:
            raise PolicyError("TARGET_CHANGED")


def minimize(task: TaskSpec, obs: Observation, candidates: tuple[Candidate, ...], model: str) -> DecisionRequest:
    # Page text and search values cannot carry secrets into the provider boundary.
    request = DecisionRequest(task_id=task.task_id, goal="Choose a fixture workflow candidate", phase=obs.phase,
                              page_kind=obs.page_kind, page_index=obs.facts.page_index,
                              has_next=obs.facts.has_next, candidates=candidates, requested_model=model)
    if len(request.model_dump_json().encode()) > 16000 or len(candidates) > 16:
        raise PolicyError("DECISION_INPUT_BUDGET")
    return request.model_copy(update={"request_digest": digest(request)})
