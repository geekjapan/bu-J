# Decision Browser verification map

The maintained map covers the installed synthetic CLI. Every recipe starts from
a fresh Launch handle (`RUN`) and a passing Doctor from [the skill](../SKILL.md).
Helpers use empty disposable runtimes, unique tasks and independent expected
records Alpha, Beta, Gamma, Delta and Epsilon, with IDs 001–005 and October 1–5,
2026 dates. Query `a` matches the first four; `no-such-fixture` matches none.

- [Search and pagination](search.md): all records, filtered query, empty result.
- [Observe only](observe.md): no operations despite a persisted observation.
- [Routing and shadow](routing.md): rules, model, hybrid and shadow comparison.
- [Audit and replay](audit.md): JSONL, redacted snapshots, export failures, duplicate task rejection.
- [Azure model](azure.md): live inference gate; unrun during skill creation.
- [Actual Chrome](chrome.md): isolated Docker, real MCP and Controller; seven of eight journeys pass, empty-search navigation race remains.
- [Three web tasks](web.md): separate web CLI, disposable Chrome, real Azure comparison, and independent completion checks.
- [General browser tasks](general.md): arbitrary goals, native input, frames, tabs, scoped files, extraction, and separate live planner acceptance.

Each user entry point has its own command and expected observable state. CLI
proof includes argv, stdout, stderr, exit status, stored rows and exported events.
A missing `*-proof.json` or failed assertion is unverified. Synthetic proofs never
cover Azure, browser sessions, MCP transport or production deployment. Keep all
proof under the printed `.audit/verification-skill/` run after cleanup.
