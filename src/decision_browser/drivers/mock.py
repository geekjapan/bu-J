from __future__ import annotations

import asyncio
import time
import uuid
from datetime import datetime, timezone

from decision_browser.contracts import ActionProposal, Element, ExecutionResult, Facts, Lease, Observation, Record, TaskSpec, digest
from decision_browser.policy import PolicyError


DEFAULT_RECORDS = (
    Record(id="001", name="Alpha", updated_at="2026-10-01"),
    Record(id="002", name="Beta", updated_at="2026-10-02"),
    Record(id="003", name="Gamma", updated_at="2026-10-03"),
    Record(id="004", name="Delta", updated_at="2026-10-04"),
    Record(id="005", name="Epsilon", updated_at="2026-10-05"),
)


class MockBrowserDriver:
    def __init__(self, records: tuple[Record, ...] = DEFAULT_RECORDS, page_size: int = 2,
                 session_id: str = "fixture-session", tab_id: str = "fixture-tab"):
        if page_size < 1:
            raise ValueError("page_size must be positive")
        self.records, self.page_size = records, page_size
        self.session_id, self.tab_id = session_id, tab_id
        self.epoch = 0
        self.query = ""
        self.searched = False
        self.page_index = 0
        self.actions: list[str] = []
        self.highest_fence = 0

    def _check(self, lease: Lease):
        if (lease.session_id != self.session_id or lease.tab_id != self.tab_id or
                lease.expires_monotonic <= time.monotonic() or lease.fencing_token < self.highest_fence):
            raise PolicyError("INVALID_OR_STALE_LEASE")
        self.highest_fence = lease.fencing_token

    def _filtered(self) -> tuple[Record, ...]:
        return tuple(r for r in self.records if self.query.casefold() in r.name.casefold())

    async def observe(self, task: TaskSpec, lease: Lease) -> Observation:
        self._check(lease)
        records = self._filtered()
        start = max(0, self.page_index - 1) * self.page_size
        page = records[start:start + self.page_size] if self.searched else ()
        has_next = self.searched and start + self.page_size < len(records)
        facts = Facts(page_index=self.page_index, has_next=has_next, searched=self.searched,
                      query=self.query, records=page, total_records=len(records) if self.searched else 0)
        elements = (Element(element_ref="next-page", role="button", name="Next"),) if has_next else ()
        if not self.searched:
            elements = (Element(element_ref="search", role="textbox", name="Search"),)
        return Observation(task_id=task.task_id, session_id=self.session_id, tab_id=self.tab_id,
                           navigation_epoch=self.epoch, observation_id=uuid.uuid4().hex,
                           captured_at=datetime.now(timezone.utc), captured_monotonic=time.monotonic(),
                           origin="https://fixture.test", page_kind="results" if self.searched else "search",
                           phase="collect" if self.searched else "search", facts=facts, elements=elements,
                           semantic_digest=digest({"facts": facts.model_dump(mode="json"), "elements": [e.model_dump(mode="json") for e in elements]}),
                           transaction_digest=digest({"query": self.query, "page": self.page_index}))

    async def execute(self, action: ActionProposal, lease: Lease) -> ExecutionResult:
        self._check(lease)
        self.actions.append(action.kind)
        if action.kind == "FILL_SEARCH":
            self.query = action.parameters.query or ""
            self.searched, self.page_index = True, 1
            self.epoch += 1
        elif action.kind == "CLICK_NEXT":
            if not self.searched or self.page_index * self.page_size >= len(self._filtered()):
                return ExecutionResult(action_id=action.action_id, status="FAILED", reason="NO_NEXT_PAGE")
            self.page_index += 1
            self.epoch += 1
        elif action.kind == "EXTRACT":
            start = (self.page_index - 1) * self.page_size
            return ExecutionResult(action_id=action.action_id, status="OK", records=self._filtered()[start:start + self.page_size])
        elif action.kind == "WAIT":
            await asyncio.sleep((action.parameters.wait_ms or 1) / 1000)
        elif action.kind == "NAVIGATE":
            self.epoch += 1
        return ExecutionResult(action_id=action.action_id, status="OK")
