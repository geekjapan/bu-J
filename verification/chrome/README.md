Run from the repository root:

```sh
.venv/bin/python verification/chrome/run.py
```

Requires the existing project Python environment and a running local Docker daemon.
The command builds an amd64 image with official Google Chrome, pristine
`browser-harness[mcp]==0.1.13` and `mcp==2.1.1`, then uses one disposable container.
Build downloads public packages. The browser runtime has `--network none`, a read-only
root, tmpfs profile/runtime, no mounts or published ports, dropped capabilities and
`no-new-privileges`. It never attaches to the host browser or mounts a home directory.
The runner checks those controls and public/metadata egress denial before constructing
the real HarnessMCPDriver. Production `start()` remains fail-closed.

Evidence is printed as `.audit/chrome/<run-id>`. It retains the actual tools/list,
source hashes and source copy, Chrome version/archive hash, image ID, container
inspection, denied egress probes, real MCP error rejection, eight journey results,
SQLite journals, audit exports and CDP PNG screenshots. Container cleanup runs in
`finally`; diagnostic log timeouts do not skip removal. A timed-out start requires
matching the unique name and ownership label before removal by immutable ID.
The local image and build cache remain for reruns. The Chrome download
URL floats, so each run records the actual artifact rather than claiming a fixed
Chrome release. The default mock mode invokes no Azure inference or paid provider.

The exact loopback FixturePolicy is a verification adapter. It inherits all action
authorization checks but does not establish production origin/registry approval.
The distributed MCP source hash is independently pinned to `8b11bc6058098329b6badc14982ffa9fb0fea5d2be24e8819fb9e73e3512fa32`.
It differs from the reviewed main-source hash `88be79452beb74bf1dd4294b4b50bfbf672c2cf76bd8bec738b9567d0716833e`
only in the exception channel; the production transport rejects both formats.
The verifier still requires the unchanged production schema validator to accept the
actual tools/list and checks a real failed CDP call before workflow operations.

A successful run covers all/filtered/empty searches, observe-only, login/wait stops,
changed labels and the fixture injection text. It does not prove model resistance
to injection because these journeys use rules and zero model calls.

An intermittent product navigation race remains. Run
`.audit/chrome/71744faa6cd84875aac0c7a33ecaff16/empty` recorded `UNKNOWN_EFFECT`
after search submission: its post-action observation still saw the old search
page and navigation epoch. A subsequent run `07dfdb0d06db46719f23f82b76ad325a`
passed all eight journeys without changing the product driver. The final authored
run `e3dd5be367be4b1fa1e0c994288c219d`, with explicit setup readiness receipts,
again failed the empty journey while the other seven passed. Keep both receipts;
a later success does not resolve that failure. The runner waits only for trusted
scenario setup and diagnostic screenshots, never between product actions and their
postcondition checks.

Independent run `70ca2e71c7534adbacfe2f4d178b038c` and final Chrome run
`20ae24f82ca14ab6998eb049d477517a` also recorded seven passes and the same empty
search failure; the latter confirms immutable image-ID launch and attestation.
Both retained eight PNGs and normal cleanup. After that Chrome run, only cleanup
was changed: the current cleanup path was exercised separately against a real
owned container with an injected diagnostic timeout. Receipt
`cleanup-fault-e56489cff86142768af3e6baf990db08` confirms removal by ID and absence.
The full eight journeys were not rerun after this cleanup-only change.

Use the real Azure API and Chrome together only for an authorized inference run:

```sh
uv run --no-sync --env-file .env python verification/chrome/run.py --provider azure
```

Requires the existing Azure CLI login and `AZURE_ENDPOINT` configuration. The host
obtains an in-memory token; no credentials enter the Chrome container or evidence.
Azure mode runs two separate tasks, enforced and shadow, with the production
AzureDecisionProvider and model routing. It permits at most 12 HTTP requests across
both tasks, with no retries. Each task has a 12,000 token-plus-call budget checked
after responses; this is not a strict monetary or server-side token limit.

`azure-calls.json` retains request state, HTTP status/request ID, response body and
validated decision, including probabilities and usage. It omits request headers.
Success requires six valid HTTP 200 decisions, six VERIFIED operations, the exact
five records, and matching SQLite/JSONL. Shadow success means known rules executed
while real model decisions were compared. Enforced completion additionally requires
the model's decisions to pass the unchanged gates. An abstention is retained as
incomplete, even when safe. The aggregate command exits nonzero if either task fails.

Run `9f3151eea5684c9db1ee15735b10778f` made seven real HTTP 200 requests to
Microsoft-Decision-1, with 690 reported tokens in total. Shadow retrieved all five
records over three pages in six VERIFIED operations. Enforced stopped before any
operation because the search probability was 0.79818678, below 0.90. Both tasks
retained screenshots, and cleanup confirmed container removal. No thresholds or
production source changed. This proves live integration and shadow completion;
model-controlled completion remains unmet.
