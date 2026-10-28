# DispatchSafety refinement map

The checked state is one row in `operations`, one shared lane, SQLite write ownership and an uncommitted insert, two workers' control locations, each worker's successful dispatch claim, a crash budget, and a driver invocation count. Other operations rows, events, observations, browser state and policy internals are abstracted away.

| Model action/state | Real code boundary | Refinement meaning |
| --- | --- | --- |
| `Init` | `Journal.__init__`, existing empty fixture database | No operation row; both logical attempts may enter the Controller. |
| `Gate(w)` | `Controller.run`: `journal.unresolved(task.task_id)` | PREPARED, DISPATCHED and UNKNOWN_EFFECT block before lane acquisition. VERIFIED permits the gate, but prepare must still reject its duplicate key. |
| `Acquire(w)` / `lane` | `BrowserLane.acquire`: `async with self.lock` | The one shared lane has one holder. This models mutual exclusion only, not fencing-token correctness. |
| `Advance(Observe, Authorize)` | `Executor.execute`: first `driver.observe` completion | An await may suspend; other enabled workers or a crash may run before completion. Observation contents are assumed admissible for the chosen search operation. |
| `Advance(Authorize, Audit)` | `Executor.execute`: `require_fresh`, `policy.authorize` | Successful synchronous preconditions do not modify the modeled operation row. Their validation semantics are outside this property. |
| `Advance(Audit, Begin)` | `save_observation`, session/proposal checks, `event("action_prepared")` | Abstract progress through the pre-prepare code. Each omitted storage statement is a stuttering step for the modeled operation row; these calls are not claimed to be one physical transaction. |
| `Begin(w)` / `transaction` | `Journal.prepare`: `BEGIN IMMEDIATE` | SQLite write transaction acquired; other write transactions/recovery wait. |
| `Insert(w)` / `pending` | `Journal.prepare`: unique-key `INSERT INTO operations` | Pending row is not yet visible to other connections. Duplicate-key failure goes to rollback. Stable key comes from `Journal.operation_key`: task, tenant, actor, workflow, kind and parameters; observation/action IDs do not create a new logical operation. |
| `Commit(w)` | `Journal.prepare`: `COMMIT` | PREPARED becomes durable/visible. The primary key is preserved in every final state. |
| `Rollback(w)` | `Journal.prepare`: duplicate/error `ROLLBACK` | Pending write discarded and write lock released. The failing attempt cannot dispatch. |
| `Dispatch(w)` / `claim[w]` | `Journal.transition(key, "DISPATCHED")` | One atomic autocommit conditional UPDATE from PREPARED, requiring rowcount 1. Only successful return gives this attempt a dispatch claim. A failed UPDATE stops before driver execution. |
| `Advance(Validate, Send)` | `Executor.execute`: final `lane.validate(lease)` before driver call | Validation assumed to pass. The claim remains that a durable DISPATCHED transition preceded invocation, even if later recovery changes the row. Lease expiry correctness is unverified. |
| `Send(w)` / `sends`, `unsafe` | Entry to the external effect in `driver.execute` | One send per invocation is assumed. Record a violation if no successful durable dispatch claim preceded it; the second send violates at-most-once. Async response handling is separate. |
| `Advance(After, Finish)` | `Executor.execute`: post-send observe, validation, verification and audit | Omitted checks do not rewrite this operation's durable state. Their await completions remain separate from Send and Finish. |
| `Finish(w)` | `Journal.transition`: DISPATCHED to VERIFIED or UNKNOWN_EFFECT; exception handling | Each update is atomic. Failed verification/interruption conservatively leaves an unresolved row. A failing expected-state update cannot remove the row. |
| `Release(w)` | `BrowserLane.acquire` finally / lock release | The next worker can acquire the lane. |
| `Crash(w)` | process interruption / abandoned attempt | Volatile control and dispatch claim disappear; its SQLite transaction rolls back; committed row and send count survive. The lane is released. Allowing the other worker to survive overapproximates a process crash that would also kill that worker. |
| `Recover(w)` | `Journal.__init__`: PREPARED/DISPATCHED to UNKNOWN_EFFECT | One atomic autocommit SQL statement, then restart through the real Controller gate. Recovery cannot erase the unique operation key. |

SQL exceptions other than duplicate/invalid-state transitions, denied preconditions and cancellation before prepare can only stop an attempt or preserve its row for this safety property. They are omitted from the successful-path control flow. They must not be reinterpreted as authorizing a send. Successful verification and uncertain postconditions are both represented, and neither permits replacing the unique operation row.

`BrokenDispatch = TRUE` skips the durable transition and dispatch claim but proceeds to send. TLC reports `DurableDispatchBeforeSend` failing after PREPARED → validate → send. The matching real-code scratch mutant removes the DISPATCHED call; a separate SQLite connection at driver entry observes PREPARED.

`BrokenPrepare = TRUE` permits insert/commit to replace a prior operation row. TLC reports two sends when another worker already passed the gate and later overwrites the existing row. The matching real-code scratch mutant uses INSERT OR REPLACE. Repeated Controller attempts with the same task and parameters then send the same operation key twice after the first VERIFIED row is overwritten.

The real boundary harness wraps the actual Journal connection, delegates every SQL statement to SQLite unchanged, and terminates immediately before/after BEGIN, INSERT, COMMIT, DISPATCHED UPDATE, driver receipt, post-send observation and VERIFIED UPDATE. The mock send receipt is fsynced to a separate file so it survives a lost process independently of the journal. On restart, the actual Journal constructor performs recovery and the actual Controller gate must return RECONCILIATION_REQUIRED_NO_REPLAY for unresolved rows. A crash after PREPARED commit but before send intentionally leaves zero sends; safety does not imply progress or exactly-once delivery.

This correspondence is a reviewed abstraction, not a machine-checked refinement proof. TLC exhaustively explores the stated finite model using its standard fingerprint algorithm; it does not execute Python or prove unbounded behavior. The real Python crash suite separately checks the 14 named crash locations plus no-crash and two retries. It does not schedule every bytecode interleaving, interrupt the middle of SQLite's atomic operations, or prove storage/remote service behavior.
