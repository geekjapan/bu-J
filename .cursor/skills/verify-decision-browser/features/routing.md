# Routing and shadow comparison

Users choose known rules, model selection or hybrid routing and can compare shadow proposals while executing known approved steps.

## Sub-features

- `routing-rules`: enforced rules skips Mock model calls.
- `routing-model`: enforced model selects each approved action through Mock decisions.
- `routing-hybrid`: this fixture resolves known steps without model calls.
- `routing-shadow`: rules and model shadow both compare Mock proposals and execute known steps.

## How to get to it (user POV)

- Run `decision-browser --provider mock --mode enforced --routing rules`.
- Select `--routing model` or `--routing hybrid` with enforced mode.
- Select `--mode shadow --routing rules` or `--mode shadow --routing model`.

## Driving it with control.py

Preconditions: fresh Launch and passing Doctor with the saved `RUN`; no Azure prerequisite.

- **All five entries.** Run `.venv/bin/python .cursor/skills/verify-decision-browser/scripts/control.py drive --run "$RUN" --feature routing`.
- **Same output.** Each result contains the five known records, six steps and six VERIFIED operations.
- **Routing difference.** Enforced rules and hybrid have zero model calls and zero decision events. Enforced model and both shadow variants have six Mock calls and six decision events.
- **Comparison.** Each decision identifies `mock-v1`, the requested mode and `comparison_match: true`. Per-command argv proves which route was selected.
- **Proof.** Inspect `routing-proof.json` and all five command, result, audit and SQLite artifacts before Cleanup.

## Gotchas

- Default Mock decisions choose the first approved candidate; matching comparison does not exercise disagreement or unsupported model output.
- Shadow consults its provider even when routing is rules. Its model comparisons are not evidence of model-controlled execution.
- Hybrid is only verified for these known fixture steps; no unknown-step fallback is exercised.
