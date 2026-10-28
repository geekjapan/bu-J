from __future__ import annotations

import asyncio
import time

from decision_browser.contracts import DecisionRequest, DecisionResult, Probability


class MockDecisionProvider:
    def __init__(self, choice: str | None = None, delay: float = 0.0):
        self.choice, self.delay = choice, delay
        self.calls = 0

    async def decide(self, request: DecisionRequest, deadline: float) -> DecisionResult:
        self.calls += 1
        async with asyncio.timeout_at(deadline):
            await asyncio.sleep(self.delay)
            choice = self.choice or request.candidates[0].candidate_id
            probabilities = tuple(Probability(candidate_id=c.candidate_id, probability=1.0 if c.candidate_id == choice else 0.0)
                                  for c in request.candidates)
            return DecisionResult(provider="mock", requested_model=request.requested_model,
                                  resolved_model=request.requested_model, choice=choice,
                                  probabilities=probabilities, request_digest=request.request_digest)
