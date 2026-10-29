# Azure model gate

The CLI can consult a configured Microsoft-Decision-1 deployment while retaining the synthetic BrowserDriver. This feature is NOT RUN by this skill's offline helper.

## Sub-features

- `azure-enforced`: live model selects approved synthetic actions.
- `azure-shadow`: live proposals are compared while known steps execute.
- `azure-rules`: enforced rules deliberately skips live inference.

## How to get to it (user POV)

- Use `--provider azure --mode enforced --routing model` or `--provider azure --mode shadow --routing model`.
- `--provider azure --mode enforced --routing rules` does not consult Azure and cannot prove connectivity.
- Supported auth is `--azure-auth azure-cli`, `entra` or `api-key`; see repository README for setup.

## Driving it with control.py

Preconditions: separate explicit approval for paid inference, a configured existing deployment and endpoint, approved auth and a budget. No helper subcommand enables Azure. Do not inspect secret values or reuse a personal browser session. The following separate manual recipe requires `AZURE_ENDPOINT` and `AZURE_DEPLOYMENT_NAME` already configured and existing authorized Azure CLI login; do not run it as part of offline creation.

- **Prepare isolated paths after approval.** Run `AZURE_RUN=$(mktemp -d /tmp/decision-browser-azure-verify-XXXXXX)` and `AZURE_TASK=$(uv run --no-sync python -c 'import uuid; print(uuid.uuid4().hex)')`. Retain the directory as evidence; no server is started.
- **Enforced model entry.** Run `.venv/bin/decision-browser --provider azure --azure-auth azure-cli --mode enforced --routing model --max-cost-units 10000 --task-id "$AZURE_TASK" --journal "$AZURE_RUN/journal.sqlite3" --output "$AZURE_RUN/result.json" --audit-jsonl "$AZURE_RUN/audit.jsonl" >"$AZURE_RUN/stdout.json" 2>"$AZURE_RUN/stderr.txt"; AZURE_EXIT=$?; printf '%s\n' "$AZURE_EXIT" >"$AZURE_RUN/exit-code.txt"`. Retain the exact argv and approved deployment identity without tokens. Require exit 0, SUCCEEDED, five known records, positive model calls, validated decision events and VERIFIED operation rows. Inspect the stored JSONL and SQLite, not only stdout.
- **Shadow entry.** Start another unique task and separate journal with the same command changed to `--mode shadow`. Require successful five-record output and live decision events with comparison metadata; failures must stay failures.
- **Rules entry.** Another separate task with `--mode enforced --routing rules` should show zero model calls and no decision events. Label this as skip-inference behavior only.
- **Cleanup.** The console commands exit by themselves; keep the retained evidence directory. No browser or MCP service is started.

## Gotchas

- Gate status stays NOT RUN until authorized live evidence exists; Mock proofs never satisfy it.
- `max-cost-units` limits calls plus returned token usage locally, not money. A deployment's monetary budget must be approved separately.
- Azure auth can run `az account get-access-token`; the offline Doctor does not establish its readiness. Never capture its token output.
- The CLI recipe above still uses a synthetic BrowserDriver. For the separately authorized real browser recipe, use the [Chrome journey](chrome.md#real-azure-and-chrome). Its 2026-10-29 live run completed shadow but enforced abstained; production MCP startup remains closed.
