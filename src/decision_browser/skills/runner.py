from decision_browser.contracts import ActionProposal, Observation, Record, TaskSpec


def resolve(candidates: tuple[ActionProposal, ...]) -> ActionProposal | None:
    return candidates[0] if len(candidates) == 1 else None


def complete(task: TaskSpec, obs: Observation, collected_pages: frozenset[int], records: tuple[Record, ...]) -> bool:
    return (obs.page_kind == "results" and obs.facts.supported and not obs.facts.requires_human and
            obs.facts.searched and obs.facts.query == task.query and obs.facts.page_index >= 1 and not obs.facts.has_next and
            collected_pages == frozenset(range(1, obs.facts.page_index + 1)) and
            len(records) == obs.facts.total_records and len({r.id for r in records}) == len(records))
