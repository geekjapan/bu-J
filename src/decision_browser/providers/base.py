from typing import Protocol

from decision_browser.contracts import DecisionRequest, DecisionResult


class DecisionProvider(Protocol):
    async def decide(self, request: DecisionRequest, deadline: float) -> DecisionResult: ...
