# Network Modes & Fixture Workflow

## Modes

- **Online (Capture Runner)**: Network-enabled GitHub Actions runner executes `tools.capture_pipeline`, talks to Exa/You.com/Tavily, uploads bundles to Supabase, and updates `artifacts/latest.json`.
- **Fixture (Sandbox/CI)**: Default mode (`FUND_SIGNAL_MODE=fixture`). Pipelines never call external APIs; they read `./fixtures/latest` populated via `make sync-fixtures`.

## Nightly Capture Workflow

1. Scheduled GitHub Actions job (`.github/workflows/nightly-capture.yml`) runs at 03:00 UTC.
2. Steps: checkout → install deps → capture → verify manifest → publish to Supabase → update pointer → summary/alerts.
3. Secrets required (GitHub Secrets): `EXA_API_KEY`, `YOUCOM_API_KEY`, `TAVILY_API_KEY`, `SUPABASE_URL`, `SUPABASE_SERVICE_KEY`, `SUPABASE_BUCKET`, `BUNDLE_HMAC_KEY` (optional).
4. Runner requirements: network access only to Exa, You.com, Tavily, and Supabase; no secrets persisted on disk.

## Local/Sandbox Workflow

1. Run `make sync-fixtures` (uses `tools.sync_fixtures.py`) to fetch the pointer/bundle into `./fixtures/latest`.
2. Pipelines automatically resolve bundle metadata (`ensure_bundle`) and verify freshness/integrity before executing.
3. If sync fails with `E_BUNDLE_EXPIRED` or `E_BUNDLE_TAMPERED`, rerun sync; if remote bundle is stale, investigate capture workflow.

## Freshness SLA

- Bundles expire **7 days** after `captured_at`; warning triggered after 48h (observed via logs/alerts), hard fail after SLA breach.
- Confidence scoring uses `captured_at` for the “Freshness Watermark” displayed to users.

## Manual Promotion / Rollback

- Re-run `python -m tools.promote_latest --prefix artifacts/YYYY/MM/DD/bundle-<id>` locally to point `latest.json` at any existing bundle, then publish pointer via `python -m tools.publish_bundle --bundle ... --remote-prefix ...` if needed.

## CI Gates

- **verify-fixtures (CI workflow):** pulls fixtures via `make sync-fixtures --source local` and runs the Day‑1 pipelines entirely offline.
- **check-freshness (CI workflow):** executes `make check-freshness`, which wraps `tools.verify_bundle` and freshness-specific tests.
- **weekly-online-contract (scheduled):** runs `make online-contract-test` with provider keys once a week (04:00 UTC Monday) to detect upstream API drift early.

All CI jobs must keep `FUND_SIGNAL_MODE=fixture` (except the weekly contract job) to ensure no accidental outbound traffic.
