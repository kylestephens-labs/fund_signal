# Network Modes & Fixture Workflow

## 1. Modes at a Glance

| Mode | Purpose | Traffic | Commands |
| --- | --- | --- | --- |
| **Online (Capture Runner)** | Gather new leads nightly | Exa, You.com, Tavily, Supabase | `python -m tools.capture_pipeline` (invoked by GH Actions) |
| **Fixture (Sandbox / CI)** | Consume last published bundle | No outbound network | `make sync-fixtures`, then run pipelines |

`FUND_SIGNAL_MODE` defaults to `fixture`; set to `online` only inside the scheduled capture workflow.

## 2. Nightly Capture (03:00 UTC)

1. `.github/workflows/nightly-capture.yml` runs on a network-enabled runner.
2. Sequence: checkout → `uv sync --all-extras` (installs prod + tooling deps) → `uv run capture_pipeline` → `uv run verify_bundle` → `uv run publish_bundle` → alert/summarize.
3. `publish_bundle` re-checks the manifest and normalizes remote prefixes before writing to Supabase, so a malformed pointer can’t clobber unrelated paths.
4. Required GitHub secrets  
   `EXA_API_KEY`, `YOUCOM_API_KEY`, `TAVILY_API_KEY`, `SUPABASE_URL`, `SUPABASE_SERVICE_KEY`, `SUPABASE_BUCKET`, optional `BUNDLE_HMAC_KEY`.
5. Runner hygiene: scoped network egress, no secrets persisted, logs scrubbed for API responses.

### Runner Security Hardening

- **Egress allowlist** – Only `api.ydc-index.io`, `api.tavily.com`, `api.exa.ai`, and the Supabase bucket host are reachable. The workflow executes `python -m tools.check_egress` before capture; any unexpected success to a denied host raises `E_EGRESS_DENIED` and the job aborts.
- **Secrets at runtime** – Keys are injected from GitHub Secrets, masked via the “Log secret sources” step, and never written to disk. Missing values raise `E_SECRET_MISSING`.
- **Rotation & expiry** – `python -m tools.rotate_keys --provider all --state-file security/rotation_state.json --check-only` runs nightly. Values older than 90 days raise `E_SECRET_EXPIRED`. To rotate manually: `python -m tools.rotate_keys --provider youcom --state-file security/rotation_state.json --force`.
- **Alerting** – Any egress or rotation failure bubbles up through the workflow’s final “Alert on failure” step, creating an issue in `#capture`. Extendable to Slack via webhook if needed.

## 3. Local / Sandbox Consumption

1. Run `make sync-fixtures` (wrapper around `tools.sync_fixtures.py`) to download `latest.json` + bundle to `./fixtures/latest`.
2. Pipelines call `ensure_bundle` which verifies manifest freshness/integrity before wiring input/output paths.
3. Troubleshooting  
   - `E_BUNDLE_EXPIRED`: nightly capture is stale → investigate workflow.  
   - `E_BUNDLE_TAMPERED` / checksum mismatch: rerun sync, then inspect Supabase history.

## 4. Freshness, Retention & Watermarks

- Bundles expire **7 days** after `captured_at`; alerts trigger after 48 h, CI fails after the SLA breach.
- Confidence scoring uses `captured_at` for “Freshness Watermark” messaging surfaced to users.
- Retain at least 30 days of bundles in `artifacts/YYYY/MM/DD/bundle-*` for rollback/audits.

## 5. Manual Promotion & Rollback

1. Promote locally: `python -m tools.promote_latest --prefix artifacts/YYYY/MM/DD/bundle-<id>`.
2. Push pointer remotely (if needed):  
   `python -m tools.publish_bundle --bundle ... --remote-prefix ... --pointer-path artifacts/latest.json`.
3. Sync affected sandboxes: rerun `make sync-fixtures`.

## 6. CI / QA Guards

- `make verify-fixtures` → runs Day‑1 pipelines entirely offline (invoked by `.github/workflows/ci.yml`).
- `make check-freshness` → executes `tools.verify_bundle` + freshness unit tests (`tests/test_freshness_gate.py`).
- `make online-contract-test` (weekly workflow) → hits Exa/You.com/Tavily with low QPS to detect upstream schema drift.

> Except for the weekly contract test, _all_ CI jobs must keep `FUND_SIGNAL_MODE=fixture` to avoid accidental outbound calls.

## 7. Key Management Notes

- Store provider credentials exclusively in GitHub Secrets; never check them into `.env`.
- Rotate Supabase service keys monthly; revoke immediately if a runner is compromised.
- The optional `BUNDLE_HMAC_KEY` signs manifests/pointers—keep it separate from the service key.
