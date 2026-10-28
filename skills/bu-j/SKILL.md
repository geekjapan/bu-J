---
name: bu-j
description: Run bu-J's offline demo or its fixed Google Flights, Wikipedia, and hotel-fixture tasks with Microsoft-Decision-1 and an owned Docker Chrome. Use when the user requests bu-J; arbitrary websites and booking are unsupported.
---

# bu-J

This skill supplies instructions, not Python dependencies, Chrome, or Azure credentials.
Use the user's bu-J checkout. Check `BU_J_DIR` if set, otherwise verify the current directory contains `pyproject.toml` with project name `decision-browser` and `src/decision_browser/web`. If neither identifies the checkout, ask for its path. Run commands from that root, not the installed skill directory.

## Offline demo

```sh
uv run --no-sync decision-browser --provider mock --mode enforced --routing rules --query a
```

Expect `SUCCEEDED` and Alpha, Beta, Gamma, Delta. This uses synthetic data and proves neither Azure connectivity nor browser interaction.

## Real browser task

Requires the Driver setup from the checkout's README: the `web` Python extra, running Docker with the `bu-j-chrome-verifier:local` base image, and existing Azure CLI sign-in. `.env` must name existing Microsoft-Decision-1 and gpt-4.1-mini deployments. Do not print `.env`, tokens, or credential command output, and do not create cloud resources or change access permissions.

Supported task names are `flights`, `wikipedia`, and `hotel`. Use only a task the user requested. Explain missing prerequisites and stop rather than switching to a personal browser or another model. Real tasks call paid APIs and require authorization for that work.

Run the installed web CLI inside one owned Chrome. In the following command, replace the `wikipedia` task value with the requested supported task. Change the Flights date only when the user requests another date.

```sh
uv run --no-sync --env-file .env python - <<'PY'
import json
import subprocess
import sys
from pathlib import Path
from uuid import uuid4
from decision_browser.web.environment import ManagedChrome

task = "wikipedia"
output = Path(".audit/skill-runs") / uuid4().hex
output.mkdir(parents=True)
print(output, flush=True)
with ManagedChrome(output / "chrome") as chrome:
    command = [sys.executable, "-m", "decision_browser.web.cli",
        "--task", task, "--flight-date", "2027-09-20",
        "--manifest", str(chrome.manifest_path),
        "--journal", str(output / "journal.sqlite3"),
        "--task-id", output.name, "--output", str(output / "result.json")]
    result = subprocess.run(command, timeout=180)
    print(json.dumps({"exit_code": result.returncode, "evidence": str(output)}))
PY
```

Read `result.json`: require `status == "SUCCEEDED"` and `independent_verification.passed == true`. Also inspect `chrome/cleanup.json` for `absent == true`. The wrapper's exit code alone does not establish task success. Retain failed attempts, screenshots, and journals; never rerun an uncertain operation or clear its journal to force success.

Keep the task's origin/control restrictions, confidence gates, and final verifier intact. No flight selection, booking, login, Wikipedia editing, arbitrary selectors, or code generated from page content. Report failures and abstentions as such. A single successful task is functional evidence, not a performance comparison.
