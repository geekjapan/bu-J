# Three web tasks

The `decision-browser-web` entry point runs Google Flights, Wikipedia, or the pinned hotel fixture. It uses the separate `decision_browser.web` runtime. Synthetic CLI proofs and the legacy TLA+ model do not cover this path.

Follow [the web benchmark recipe](../../../../docs/web_benchmark.md) from the repository root. Live execution requires authorized inference, configured existing Azure deployments, Azure CLI login, Docker, and the locally built Chrome base image. The user's benchmark authorization covers the current comparison; a future offline verification run must not silently enable paid inference.

For the model-free hotel control check, run:

```sh
.venv/bin/python verification/web_benchmark/check_controls.py
```

Require five observed effects, a passing independent final verification, the retained PNG, and a cleanup receipt with `absent: true`. This check supplies known input directly and does not establish model-controlled completion.

For the real comparison, use the suite command in the benchmark recipe. Require all planned attempts to be recorded. Inspect independent completion checks, actual HTTP records, generated text, journals, and container cleanup. Preserve abstentions, timeouts, and failures. A short rejection is not a performance success.

See [the failure fixes and live acceptance record](../../../../docs/web_failure_fixes.md) for the repaired progress, freshness, replacement-field, and pre-input cancellation boundaries. Preserve the documented single-trial limit when reporting that evidence.

The runtime's focused local checks are:

```sh
.venv/bin/python -m pytest -q tests/test_web_*.py
```

They exercise real SQLite and process-crash boundaries but mock browser and model responses. They cannot establish live site compatibility, inference permissions, or successful journeys.
