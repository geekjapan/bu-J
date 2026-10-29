---
name: verify-decision-browser
description: Drive the installed decision-browser CLI with its synthetic Driver and Mock provider, verifying search, pagination, observe-only, routing, SQLite journals and audit exports. Use for local CLI acceptance and retained evidence; live Azure requires separate authorization.
---

# Verify Decision Browser

Run from the repository root. The primary user surface is the installed
`decision-browser` console command. This skill drives it through subprocess pipes,
using the existing Mock provider boundary and synthetic fixture Driver. Read the
[feature index](features/README.md) and the selected journey before running.

## Launch

The application is a short-lived CLI; there is no server, port, login or shared
browser session. Requires the project's existing Python >=3.12 environment:
`.venv/bin/python` and `.venv/bin/decision-browser`. If absent, stop and report the
missing prerequisite; the documented installation is
`uv sync --locked --extra dev --python 3.12` (dependency installation is separate
from this read-only doctor).

```sh
RUN=$(.venv/bin/python .cursor/skills/verify-decision-browser/scripts/control.py launch)
printf '%s\n' "$RUN"
```

Launch checks the actual console entry point's `--help`, records package versions
and boundary source hashes, and creates an empty, unique temporary runtime.
The printed absolute directory is the run's evidence handle. Save it before
continuing. Ready means the installed CLI accepts the documented flags; a drive
must still prove the user behavior. Every drive uses unique task IDs and explicit
journal paths. No default repository journal or personal session is used.

## Doctor

```sh
.venv/bin/python .cursor/skills/verify-decision-browser/scripts/control.py doctor
```

This read-only check reads Python/package versions and runs CLI `--help` only.
It does not inspect credentials, sign in, run `az`, discover browsers or invoke
inference. A failed doctor stops the drive. It does not claim auth readiness.

## Drive

```sh
.venv/bin/python .cursor/skills/verify-decision-browser/scripts/control.py drive --run "$RUN" --feature search
```

Other mapped journeys use `--feature observe`, `--feature routing`, or
`--feature audit`, one per invocation. Each journey launches the actual installed
console script with explicit `--provider mock`, isolated SQLite, output and audit
paths. Noninteractive pipes are sufficient for this argparse CLI. The helper
asserts independent expected fixture values rather than importing fixture records
or invoking Controller internals. Run each journey once per run; use a new launch
to repeat it. Do not label unrun journeys as covered.

The [Azure journey](features/azure.md) is a separate, deliberately unrun gate.
Do not switch a helper invocation to Azure or enable MCP to make a local proof.
`HarnessMCPDriver.start()` currently refuses with
`LIVE_ISOLATION_AND_EGRESS_NOT_VERIFIED`; this skill does not start it.

For the separately authorized real Chrome path, use the [Chrome journey](features/chrome.md).
It has its own Docker prerequisites, ownership checks, evidence and cleanup;
the CLI helper above remains synthetic.

For the separate `decision-browser-web` entry point and the Flights, Wikipedia,
and hotel comparison, use [the web journey](features/web.md). Its live calls are
separate from the offline helper and legacy formal coverage.

For `decision-browser-general`, use [the general browser journey](features/general.md).
It verifies the new owned Chrome adapter. Its API planner acceptance is separate.

## Evidence

Proof lives under `.audit/verification-skill/` in the UUID directory printed by
Launch. `command.json` captures exact argv, cwd, stdout, stderr and exit status.
Each successful command's `result.json` must equal stdout; expected records,
steps and state are checked. `audit.jsonl` must match SQLite events in order.
`sqlite-state.json` and read-only backup `journal.sqlite3` capture stored side
effects, including zero operations for observe-only. Failure journeys retain
command transcripts and the authoritative SQLite journal when export fails.
Each completed journey writes its own `*-proof.json`; missing means unverified.

`boundary.json` and source snapshots ground the no-external-system claim:
`cli.run` with `--provider mock` selects MockBrowserDriver/MockDecisionProvider;
their exercised paths do not invoke `az`, launch browsers or use network clients.
This is fixed-route plus production source evidence, not OS network monitoring.
Mock model routing proves Controller routing semantics, never Azure behavior or
real browser interaction. Shadow's matching proposal is the default Mock choice;
this journey does not prove disagreement handling.

## Cleanup

Run after success or failure, retaining the printed run handle:

```sh
.venv/bin/python .cursor/skills/verify-decision-browser/scripts/control.py cleanup --run "$RUN"
```

Cleanup verifies the unique runtime's ownership marker, removes only that scratch
directory, and checks retained proof files. It is safe to rerun. Each CLI process
is synchronous with a timeout; there is no daemon to kill. Evidence remains in
`.audit/verification-skill/` and `cleanup.json` records its survival. Do not remove
evidence as part of teardown.

## Helpers

The executable helper is `scripts/control.py`; invoke with the repository's
`.venv/bin/python` as shown above. It uses standard-library subprocess and SQLite,
plus installed package metadata. No product code, dependency configuration,
credentials or external resources are modified. Assertion failure means failure,
not a skipped success; inspect its retained command and SQLite artifacts.

Use `/maintain-verification-skill` when CLI behavior or the feature map changes.
