# Actual Chrome

This is a separate live browser verifier, outside the synthetic CLI helper.
Read [the runner instructions](../../../../verification/chrome/README.md)
before running from the repository root:

```sh
.venv/bin/python verification/chrome/run.py
```

Requires the existing Python environment and local Docker daemon. Building downloads
public packages; runtime Chrome has no external network, mounts or published ports.
The runner checks its owned container and immutable image ID before constructing
the product Driver. It uses only the exact loopback fixture Policy adapter and
rules routing; production startup remains closed and no model is called.

The eight journeys are all/filtered/empty search, observe-only, login, unsupported
wait, changed labels and fixture injection text. Check each expected result against
the actual result, SQLite and exported audit. PNGs are diagnostic captures after
the persisted outcome. The unique `.audit/chrome/<run-id>` is printed at launch.
Cleanup retains evidence and removes only the owned container by immutable ID;
the local image/cache remain. Missing or failed proof means failure.

On 2026-10-29, Chrome 156.0.8078.73 run `20ae24f82ca14ab6998eb049d477517a`
passed seven journeys and failed empty search with `UNKNOWN_EFFECT`. It captured
eight PNGs. This is not full browser acceptance. Keep failures alongside successes;
do not insert waits into product actions or rerun until green to hide the race.
The subsequent cleanup-only revision has separate real fault-injection evidence,
as recorded in the runner instructions.

## Real Azure and Chrome

For an explicitly authorized live inference run, use
`uv run --no-sync --env-file .env python verification/chrome/run.py --provider azure`.
It uses the existing Azure CLI login and configured endpoint, with two isolated
tasks, enforced and shadow. See the runner instructions for the 12-request limit,
token-budget limits, evidence and cleanup. Never capture token or request headers.

Run `9f3151eea5684c9db1ee15735b10778f` verified seven actual HTTP 200 responses.
Shadow completed five records across three pages; enforced abstained at probability
0.79818678 before any operation. The aggregate result remains failure. Shadow does
not establish model-controlled completion, and production startup remains closed.
