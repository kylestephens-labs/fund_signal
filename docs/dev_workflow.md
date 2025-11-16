# Developer Workflow Guardrails

To keep local environments healthy and reproducible, follow the repo’s baked-in guardrails:

## Python Interpreter

- VS Code automatically targets `${workspaceFolder}/.venv/bin/python` via `.vscode/settings.json`.
- If the interpreter warning ever reappears, open the Command Palette → “Python: Select Interpreter” and choose the same path.

## Make Targets

- `make install` – Installs runtime + dev dependencies using `uv pip` (with `UV_CACHE_DIR` exported by the Makefile). Run this after cloning or whenever requirements change.
- `make test` – Executes the full pytest suite through `uv run`; run `make install` beforehand so the `.venv` always has the latest deps.
- `make serve` (alias `make dev`) – Starts the FastAPI server via `uv run uvicorn …`, so the same interpreter/venv powers the API.

Because these commands wrap `uv`, you never have to remember to set `UV_CACHE_DIR` or avoid raw `pip`. Stick to the Make targets to prevent environment drift and keep CI parity.

## UI Drawer Smoke Test

Day 2 promises every score explains itself, so keep the Playwright drawer test green:

1. Start the API in fixture mode: `FUND_SIGNAL_MODE=fixture SCORING_MODEL=fixture-rubric uvicorn app.main:app --reload --port 8000`.
2. Launch the UI locally (or point `UI_BASE_URL` at your deployed preview). The test expects the drawer to surface the `High Growth SaaS` fixture company by default; override via the `UI_SMOKE_*` env vars when needed.
3. Install UI test deps once: `cd frontend && npm install && npx playwright install --with-deps`.
4. Run the smoke: `cd frontend && UI_BASE_URL=http://localhost:3000 API_BASE_URL=http://localhost:8000 npm run test:ui`.

Artifacts (videos, traces, screenshots) land in `frontend/artifacts/`; ship them to CI storage so Render/Supabase dashboards can link to the latest drawer validation run.

If you need to debug without the forced “missing proof” mutation, set `UI_SMOKE_DISABLE_PROOF_PATCH=true` before launching Playwright so the test only verifies the happy-path drawer.

## Evidence Integrity QA Job

- Run `python -m pipelines.qa.proof_link_monitor --input leads/day1_output.json --supabase-table proof_link_audits` whenever a new bundle ships. The CLI dedupes URLs per run, issues concurrent HEAD requests (default `PROOF_QA_CONCURRENCY=25`), falls back to GET on 405s, and records every attempt (status, latency, retry_count, company_id, slug) in the `proof_link_audits` Supabase table.
- Configure env vars in `.env`/CI: `SUPABASE_URL`, `SUPABASE_SERVICE_KEY`, `SUPABASE_PROOF_QA_TABLE`, `PROOF_QA_RETRY_LIMIT`, `PROOF_QA_FAILURE_THRESHOLD`, and optional `PROOF_QA_ALERT_WEBHOOK`. Set `PROOF_QA_DISABLE_ALERTS=true` as a kill switch during rollouts or dry runs.
- Alerts trigger when ≥3% of proofs fail in a run or the same proof fails twice inside 24h; payloads only include sanitized URLs + company/proof slug so secrets never leak. Failures bubble up as `ProofLinkMonitorError` with codes (`504_HEAD_TIMEOUT`, `523_TLS_HANDSHAKE_FAILED`, `598_TOO_MANY_FAILURES`) for CI visibility.
- Signal proofs must include timestamps fresher than `PROOF_MAX_AGE_DAYS` (default 90). Update fixtures or raise the limit temporarily via env vars only when validating legacy bundles; stale proofs now raise `422_PROOF_STALE` during hydration.
- Domain replay: `python -m pipelines.qa.proof_domain_replay --scores scores/day2_fixture.json --bundle-id day2_fixture --supabase-table proof_domain_audits` replays stored `CompanyScore` payloads, follows redirects (default `PROOF_REPLAY_MAX_REDIRECTS=5`), flags domain/protocol drift, and publishes rows to Supabase plus alerts via `PROOF_REPLAY_ALERT_WEBHOOK`. Set `PROOF_REPLAY_DISABLE_ALERTS=true` for smoke tests; otherwise any insecure redirect triggers immediate notifications.
- Seed data: use `leads/day1_output.json` or any scoring export that contains `proof_links`/`SignalProof` metadata. Add at least one intentionally broken URL when validating alert webhooks.
