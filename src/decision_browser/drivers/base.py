from typing import Protocol

from decision_browser.contracts import ActionProposal, ExecutionResult, Lease, Observation, TaskSpec


class BrowserDriver(Protocol):
    async def observe(self, task: TaskSpec, lease: Lease) -> Observation: ...
    async def execute(self, action: ActionProposal, lease: Lease) -> ExecutionResult: ...
