## Feedback Case Bundle

This bundle recreates the FSQ‑008 “feedback resolver” scenario in a deterministic package that can be consumed by offline tests or CLIs.

### Contents

- `leads/exa_seed.normalized.json` – resolver output with two rows:
  - `row_hotglue` is intentionally mis-resolved (`company_name: "Seed Round"` with `final_label: "EXCLUDE"`). Feature flags show why the resolver scored it poorly even though Hotglue-specific signals exist.
  - `row_appya` is a control lead that stays VERIFIED so tests can confirm high-confidence rows remain untouched.
- `leads/youcom_verified.json` / `leads/tavily_verified.json` – deterministic article evidence for Hotglue spanning independent domains (`techcrunch.com`, `businesswire.com`, `venturebeat.com`). Feedback logic can treat these as corroborating hits.
- `manifest.json` – schema_version 1 manifest with SHAs for every file so determinism is auditable.

### Usage

- Import this bundle in forthcoming `tests/test_feedback_resolver.py` (or local experiments) whenever you need to simulate a low-confidence row being promoted by multi-source evidence.
- Keep the captured timestamp, file ordering, and SHAs stable; if you regenerate the leads, rerun `python -m tools.manifest_utils update` to refresh the manifest.
- Validate `leads/exa_seed.normalized.json` with `python3 -m json.tool` after edits to catch formatting mistakes early.
