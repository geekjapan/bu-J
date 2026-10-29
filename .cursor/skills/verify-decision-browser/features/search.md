# Search and pagination

Search returns synthetic records whose names contain the query and collects all result pages without duplicates.

## Sub-features

- `search-all`: five records over three pages.
- `search-filter`: case-insensitive name filtering with query `a`.
- `search-empty`: successful completed search with zero matches.

## How to get to it (user POV)

- Run `decision-browser --provider mock --mode enforced --routing rules`.
- Add `--query a` to filter, or `--query no-such-fixture` for no matches.
- Pagination is automatic; there is no separate next-page CLI entry point.

## Driving it with control.py

Preconditions: fresh Launch and passing Doctor; use the repository's `uv run --no-sync python` and its saved `RUN`.

- **All three entries.** Run `uv run --no-sync python .cursor/skills/verify-decision-browser/scripts/control.py drive --run "RUN_PATH" --feature search`. It launches the three exact query variants with isolated journals and explicit output, task and audit flags.
- **All records.** `all-records/result.json` shows `SUCCEEDED`, the five known IDs/names/dates, six steps and zero model calls. SQLite contains six VERIFIED operations; observation events cover page indices 0, 1, 2 and 3.
- **Filter.** `filtered/result.json` contains Alpha, Beta, Gamma, Delta in order, four steps and four VERIFIED operations.
- **No matches.** `empty/result.json` contains an empty records array, `SUCCEEDED`, two steps and two VERIFIED operations (search and extraction).
- **Proof.** Inspect `search-proof.json`, the three command transcripts, result files, JSONL exports and SQLite backups before Cleanup.

## Gotchas

- No real site is searched. The fixture has a fixed page size of two.
- A successful exit alone does not prove pagination; require the known records, operation counts and page indices.
- Query is a literal argument, including an empty string for the all-records case. No shell interpolation reaches the CLI.
