---
name: verify-decision-browser-safety
description: Check durable dispatch and crash/retry safety when changing decision_browser Journal, Executor, Controller recovery gates, or browser lane serialization. Runs a bounded TLA+ model and real Python crash-boundary mutation checks.
---

# Verify durable browser dispatch

Use this skill for changes to `Journal.prepare`, `Journal.transition`, journal recovery, `Journal.operation_key`, `Journal.unresolved`, the ordering in `Executor.execute`, the unresolved gate in `Controller.run`, or the lane lock in `BrowserLane.acquire`.

Property: a logical operation reaches the driver at most once, and only after its `DISPATCHED` transition has committed. Harm: a crash or concurrent retry could repeat a browser effect or perform an effect without a durable record that prevents replay.

This is a bounded model check plus real-code refinement checks. It is not a proof of exactly-once website effects, policy authorization, lease expiry/fencing, arbitrary retries, or the whole Executor.

## Launch and run

From the repository root, use the existing Python 3.12+ environment with this project and its dependencies installed, and Java 11+ on PATH. The confirmed environment was Python 3.12.11 and OpenJDK 27. If the virtual environment is missing, use the repository setup command `uv sync --locked --extra dev` first.

```bash
uv run --no-sync python verification/formal/run.py
```

The runner fetches the official [TLA+ tools v1.8.0 release](https://github.com/tlaplus/tlaplus/releases/tag/v1.8.0) JAR into ignored `.audit/formal/tools/` only when absent. It always verifies SHA256 `fc6878c603f0fdc18b9fe3e5bf51860cb90ab5b1154e3c371660c036d6bdd63e`; do not commit the JAR. It runs TLC breadth-first with one worker, a fixed seed/fingerprint polynomial, and a 512 MB heap. No browser service, model provider, credentials, or paid call is used.

The printed evidence directory `.audit/formal/run-<UTC>-<unique>/` contains exact commands, tool versions, source hashes, logs, TLC counterexample traces, per-attempt SQLite journals and mock send receipts, and `summary.json`. Runs are separate; retain the failing run when investigating. Scratch source copies are automatically removed; their mutation descriptions and behavioral logs remain. The product source is never mutated.

## Interpret the result

A successful command exits 0 and records `status: PASS`. All of these must hold:

- Baseline TLC completes with zero queued states and both safety invariants intact. Initial confirmed result: 881 generated states, 497 distinct states, depth 26.
- `BrokenDispatch` fails specifically on `DurableDispatchBeforeSend`; `BrokenPrepare` fails specifically on `NoDuplicateSend`. These expected counterexamples are positive controls, not product findings.
- Real `Controller` / `Executor` / `Journal` complete 15 cases: no crash plus both sides of seven boundaries, each with three process attempts (45 processes). At durable pre-send crashes, zero sends is the expected safe outcome.
- A scratch copy removing `self.journal.transition(key, "DISPATCHED")` fails because a mock driver receipt observes `PREPARED` at send. A scratch copy replacing journal INSERT with INSERT OR REPLACE fails because the same operation key is sent twice. Failure must be behavioral; source-hash drift alone is insufficient.
- The complete baseline crash suite passes again after scratch cleanup, and covered product source hashes match the starting values.

`TypeOK` is a model consistency check, not an additional claimed product guarantee. A missing tool, parser error, unexpected mutant error, incomplete TLC search, or unreachable crash checkpoint is a failed verification, never a pass. Open the named log in the printed evidence directory.

For a baseline counterexample, read it as a sequence of `Controller` gate, lane acquisition, prepare SQL, committed dispatch, driver send, and recovery steps. Reproduce it in real code before classifying it. A missing production guard is a finding; a model step that cannot occur under the stated assumptions is a refinement error. Keep the property intact, report the trace, and obtain an authorized product-fix task rather than weakening the check.

## Bounds and correspondence

Read [the refinement map](../../../verification/formal/REFINEMENT.md) before changing the model. It maps every model action to the real function or SQL statement and lists omissions.

TLC explores two logical workers, one stable operation key, one shared browser lane and SQLite database, and at most one crash/restart across the workers. Workers may both pass the Controller gate before the first prepare. Prepare BEGIN, INSERT, COMMIT and the durable DISPATCHED update are separate steps; the whole Executor is never atomic. SQLite transactions, autocommit statements and recovery are assumed atomic/durable; the driver is assumed to send once per invocation. No fairness or liveness is asserted. Terminal states are allowed, so deadlock checking is disabled. No state-space cutoff is used for the baseline. The send counter saturates at the first violating second send.

The Python suite uses the real code with a fixture driver and real on-disk SQLite WAL databases, force-exits processes with `os._exit(75)` at the chosen boundary, and starts fresh processes. It checks each listed boundary; it is not an exhaustive Python execution proof. Website retries, power loss or broken storage guarantees, multiple independent lanes/processes using one browser, lease fencing/expiry, different task IDs/operation keys, changing parameters, approval nonces, HTTP delivery, and arbitrary browser/provider behavior remain outside the claim.

## Coverage and synchronization

The authoritative coverage record is [coverage.json](../../../verification/formal/coverage.json), including the exact function list and SHA256 of every covered source file. Its initial `sync_commit` is JSON `null` because this repository had an unborn `main` and no commit at confirmation. No commit identifier is fabricated. These source hashes are the initial synchronization evidence.

When a covered source hash changes, the runner stops before verification. Read the changed functions and the refinement map; update model actions, crash checkpoints and mutation controls if intended behavior changed. Only after confirming correspondence, record current hashes and the real HEAD (or `null` if HEAD is unborn or any covered file differs from HEAD):

```bash
uv run --no-sync python verification/formal/run.py --record-sync
uv run --no-sync python verification/formal/run.py
```

The runner only records a non-null commit when every covered file matches the bytes stored in HEAD. Dirty or untracked covered files retain `null` and a working-tree snapshot note. Check this behavior with `uv run --no-sync python -m unittest discover -s verification/formal -p "test_*.py"`.

After the first commit exists, follow that same review and recording procedure to replace `null` with the actual commit. `--record-sync` records evidence; it does not prove correspondence. Do not advance it just to silence drift. On later maintenance, use `/maintain-formal-verification-skill` to reconcile changes that bypassed this check.
