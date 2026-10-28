# Observe only

Observe-only reports the current synthetic page and a proposal without dispatching an operation.

## Sub-features

- `observe-result`: OBSERVED result with zero steps and no records.
- `observe-side-effects`: persisted observation and proposal, zero operation rows.

## How to get to it (user POV)

- Run `decision-browser --provider mock --mode observe-only --routing rules`.

## Driving it with control.py

Preconditions: fresh Launch and passing Doctor with the saved `RUN`.

- **Observe.** Run `.venv/bin/python .cursor/skills/verify-decision-browser/scripts/control.py drive --run "$RUN" --feature observe`.
- **Visible result.** `observe-only/result.json` shows `OBSERVED`, `OBSERVE_ONLY_NO_ACTION`, zero steps, zero model calls and no records.
- **Stored effect.** The independent SQLite read finds zero rows in `operations`, with only `task_started`, `observation`, `proposal_only` events. The stored page remains unsearched at page zero.
- **Proof.** Inspect `observe-proof.json`, command transcript, audit export and SQLite backup before Cleanup.

## Gotchas

- Observe-only still creates a journal and persists a redacted observation; it is not filesystem read-only.
- This rules-route recipe deliberately avoids model calls. Observe-only with model routing can consult its provider before returning; it is not covered here.
- This proves zero dispatch in the synthetic CLI path, not browser-network isolation.
