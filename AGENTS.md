# Project verification

Use the project-local verification skills for changes to Decision Browser.

- [verify-decision-browser](.cursor/skills/verify-decision-browser/SKILL.md) drives the real CLI with isolated journals and retained evidence. Read its feature map before selecting a journey.
- [verify-decision-browser-safety](.cursor/skills/verify-decision-browser-safety/SKILL.md) checks durable dispatch and crash/retry safety. Read its coverage record when changing the journal, executor or controller.

Run the relevant skill's documented command and report its bounds. Live Azure inference and browser operations require their own authorized verification; offline results do not establish those outcomes.

The bu-j Skill and decision-browser-general use the user's existing browser and profile.
Docker and disposable profiles are optional verification/benchmark infrastructure, never
normal runtime prerequisites. Preserve borrowed tabs; disconnect without terminating
the browser. Keep a regression check for these boundaries.
