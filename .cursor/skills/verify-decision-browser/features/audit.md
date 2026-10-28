# Audit exports, failures and replay

Users retain an authoritative SQLite journal, export ordered JSONL events, and are prevented from repeating an operation under the same task identity.

## Sub-features

- `audit-export`: result file equals stdout; JSONL equals stored events.
- `audit-redaction`: stored observation records and query are redacted.
- `audit-replay`: duplicate task submission fails without new operation rows.
- `audit-failure`: failed result or JSONL destination still leaves authoritative completed SQLite data.

## How to get to it (user POV)

- Add `--journal`, `--output`, `--audit-jsonl` and `--task-id` to a CLI run.
- Repeat the same `--task-id` against the same `--journal` to request replay.
- Give `--output` or `--audit-jsonl` a destination whose parent does not exist to exercise failure reporting.
- Read the resulting journal with SQLite; persisted redacted observations have no CLI reconstruction command.

## Driving it with control.py

Preconditions: fresh Launch and passing Doctor with the saved `RUN`; all failures target only run-owned scratch paths.

- **Export and stored observations.** Run `.venv/bin/python .cursor/skills/verify-decision-browser/scripts/control.py drive --run "$RUN" --feature audit`. `audit-success` searches `a` and records four matching records and four VERIFIED operations; exported event types/payloads equal SQLite order. Observation snapshots use `persisted-redacted-v1` and redacted nonempty query and record names.
- **Replay.** The helper resubmits the same task and journal. `replay/command.json` shows exit 1, `FAILED`, `AUDIT_OR_JOURNAL_PERSISTENCE_FAILED`; independent SQLite reads show exactly the original four operations unchanged.
- **Export failure.** `export-failure/command.json` shows exit 1 and `SQLite journal remains authoritative`; no result JSON is written. Its retained SQLite backup still ends in `task_succeeded` and six VERIFIED operations.
- **Output failure.** `output-failure/command.json` shows the same exit and warning, while its earlier JSONL export survives and completed operations remain in SQLite.
- **Proof.** Inspect `audit-proof.json`, every command transcript and all SQLite backups before Cleanup.

## Gotchas

- Replay here means duplicate-submission rejection and inspection of persisted snapshots, not a full task replay feature.
- Reusing a task with unresolved operations has different reconciliation behavior; this recipe covers only a previously completed task.
- Export failure has no successful `audit.jsonl`; use retained SQLite and the failure transcript. Do not claim the failed export passed.
- Audit redaction does not redact user-requested result JSON. This skill uses only public synthetic fixtures.
