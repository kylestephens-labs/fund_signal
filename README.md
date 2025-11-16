Here's an optimized README for FundSignal, fully reconstructed using your FundSignal context and blending in relevant technical sections and tooling notes from your original FastAPI template README. This version emphasizes project-specific details, end-to-end setup, deployment, and usage instructions while surfacing only the most useful content from the template.

***

# FundSignal

[
[
[

**AI-Verified Funding Signal Delivery for SaaS Sales Teams**

***

## Overview

FundSignal delivers curated, explainable lists of B2B SaaS companies recently funded (within their prime buying window), verified by multiple public sources and ranked by an AI scoring system. Results are sent directly to sales teams via Slack and email—eliminating 10+ hours/week of manual research, and ensuring your prospecting effort is always directed at the most actionable targets.

**Ideal User:** B2B SaaS Account Executives at Series A–C companies.

***

## Features

- **AI-Scored Funding Signals:** Multi-source verification (Exa, Tavily, Twitter) and OpenAI explainability.
- **Automated Delivery:** Curated signals sent via Slack, email, and CSV—no dashboard logins.
- **Zero Friction:** No AWS/N8N; relies on production-ready Python, lightweight hosting, and modern scheduling.
- **One-Click Deploy:** Render.com integration for instant deployment.
- **Subscription Ready:** Stripe-powered billing, tiered pricing, and early adopter promo.
- **Agile Database:** Supabase PostgreSQL backend, with explainable JSON scoring and proof links.

***

## Tech Stack

| Layer        | Technology           |
|--------------|---------------------|
| Backend      | FastAPI (Python 3.12) |
| Database     | Supabase (PostgreSQL) |
| Hosting      | Render.com           |
| Scheduling   | GitHub Actions (cron) |
| Data Sources | Exa API, Tavily API, Twitter API |
| AI           | OpenAI GPT-4         |
| Delivery     | Slack SDK, Resend (email) |
| Payments     | Stripe               |

***

## Quick Start

### 1. Clone & Setup

```bash
git clone https://github.com/kylestephens-labs/fund_signal.git
cd fund_signal
cp .env.example .env  # Fill in Supabase URL, secret keys, and API tokens
```

### 2. Install Dependencies

```bash
uv venv
source .venv/bin/activate
uv pip install -r requirements.txt
```

### 3. Local Development

```bash
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
curl http://localhost:8000/health     # Ensure you see a 200 response
```

### Runtime Modes (Day 1 Pipelines)

| Env Var | Default | Purpose |
|---------|---------|---------|
| `FUND_SIGNAL_MODE` | `fixture` | `fixture` keeps runs offline; set to `online` only on the capture host. |
| `FUND_SIGNAL_SOURCE` | `local` | Where fixtures are read from (`local` directory or `supabase`). |
| `FUND_SIGNAL_FIXTURE_ROOT` | `fixtures/latest` | Directory that holds `latest.json` and bundle contents. |
| `FUND_SIGNAL_FIXTURE_DIR` | `fixtures/sample` | Internal use; auto-set to `<bundle>/fixtures` in fixture mode. |
| `FUND_SIGNAL_SUPABASE_BASE_URL` | _empty_ | Supabase storage endpoint (fixture fetch). |
| `FUND_SIGNAL_SUPABASE_SERVICE_KEY` | _empty_ | Service key used by fixture sync tooling. |
| `PROOF_CACHE_TTL_SECONDS` | `300` | Cache duration for hydrated proof links used by the scoring API. |

In sandbox/CI, keep the defaults so no outbound network occurs. The capture job (GitHub Actions/runner) switches to `FUND_SIGNAL_MODE=online` and uploads new fixtures to Supabase before developers sync them down.

### Capture Pipeline (Networked)

Run `python -m tools.capture_pipeline --input leads/exa_seed.json --out artifacts --concurrency 8 --qps-youcom 2 --qps-tavily 2` on a network-enabled runner. The CLI:

1. Fetches You.com and Tavily data concurrently with QPS limits/retries.
2. Stores raw JSONL plus derived fixtures in a date-stamped bundle (`artifacts/YYYY/MM/DD/bundle-...`).
3. Runs the Day‑1 pipelines in fixture mode to produce `leads/youcom_verified.json` and `leads/tavily_confirmed.json`.
4. Writes `manifest.json` with provider stats (`requests`, `rate_limits`, `dedup_ratio`) and `captured_at`.

Use `--resume --bundle <path>` to continue a partially completed bundle without re-requesting finished companies.

After uploading a completed bundle, finalize the pointer with:

```bash
python -m tools.promote_latest --prefix artifacts/YYYY/MM/DD/bundle-<timestamp>
```

This writes/updates `latest.json` (atomic) so consumers only see fully published bundles. See `docs/data_pipelines.md/storage_layout.md` for the bucket layout and promotion flow.

Before consuming a bundle, run:

```bash
python -m tools.verify_bundle --manifest artifacts/YYYY/MM/DD/bundle-<timestamp>/manifest.json
```

This enforces freshness, checksums, and optional signature validation.

### Nightly Capture Workflow

- Automated GitHub Actions workflow (`nightly-capture.yml`) runs every day at 03:00 UTC on a network-enabled runner.
- Steps: checkout → install via `uv sync --all-extras` → `uv run tools.capture_pipeline` (with retries/QPS limits) → `uv run tools.verify_bundle` → `uv run tools.publish_bundle` (uploads to Supabase and atomically updates `artifacts/latest.json`).
- `tools.publish_bundle` re-validates every manifest plus pointer metadata before uploading files and strips leading slashes from remote prefixes to prevent accidental bucket root overwrites.
- Secrets required (configured in repository settings): `EXA_API_KEY`, `YOUCOM_API_KEY`, `TAVILY_API_KEY`, `SUPABASE_URL`, `SUPABASE_SERVICE_KEY`, `SUPABASE_BUCKET`, `BUNDLE_HMAC_KEY` (optional).
- Failures raise GitHub Issues (labelled `capture`). Bundles are only promoted after verification, so consumers never see partial runs.
- See `docs/network_modes.md` for the full runbook (runner requirements, SLA, rollback).

### Fixture Sync (Sandbox)

To hydrate `./fixtures/latest` with a local (or downloaded) bundle:

```bash
python -m tools.sync_fixtures --source fixtures/sample/bundle-sample --dest-root fixtures/latest
```

This copies the bundle, validates it, and updates `fixtures/latest/latest.json`. Pipelines running in fixture mode automatically resolve this pointer, enforce freshness/integrity, and log the bundle ID/age before processing.

**Fixture storage policy**

- `fixtures/sample/`: small anonymized fixtures checked into Git for CI/unit tests (≤5 fake companies). Default when `FUND_SIGNAL_SOURCE=local`.
- `fixtures/latest/`: real nightly bundles pulled from Supabase via `make sync-fixtures` (ignored by Git). Used when `FUND_SIGNAL_SOURCE=supabase`.

### CI Guards

- `.github/workflows/ci.yml` runs on every push/PR and includes:
  - **verify-fixtures:** syncs sample fixtures (`python -m tools.sync_fixtures --source local ...`) and runs the Day‑1 pipelines entirely offline to guarantee deterministic outputs.
  - **check-freshness:** validates `fixtures/latest/manifest.json` via `tools.verify_bundle` and fails when bundles are expired or tampered.
- `.github/workflows/weekly-online-contract.yml` runs every Monday 04:00 UTC, executes a tiny live smoke test (marked `@pytest.mark.contract`) with provider keys, and alerts if Exa/You.com/Tavily responses diverge from the expected schema.

Helpful commands:

```bash
make verify-fixtures       # Offline pipelines using fixtures
make check-freshness       # Freshness/integrity gate
make online-contract-test  # Minimal live smoke (requires provider keys)
```

### Feedback Resolver CLI (FSQ‑008)

Run the deterministic feedback resolver immediately after `tools.normalize_and_resolve` finishes so `unified_verify` consumes the corrected payload:

```bash
FUND_SIGNAL_MODE=fixture FUND_SIGNAL_SOURCE=local \
python -m tools.verify_feedback_resolver \
  --input artifacts/<bundle>/leads/exa_seed.normalized.json \
  --youcom artifacts/<bundle>/leads/youcom_verified.json \
  --tavily artifacts/<bundle>/leads/tavily_verified.json \
  --out artifacts/<bundle>/leads/exa_seed.feedback_resolved.json \
  --update-manifest artifacts/<bundle>/manifest.json
```

**What it does**

1. Filters rows with `resolution.final_label == "EXCLUDE"` or resolver scores `<2`.
2. Scans You.com/Tavily evidence for 1–3 token spans that appear in ≥2 unique domains (publisher/stopword tokens are ignored).
3. Promotes the winning span deterministically and records `feedback_applied`, `feedback_reason`, `feedback_domains`, `feedback_version` (`v1`), and `feedback_sha256` per row, plus a canonical payload SHA for the entire file.

**Operator checklist**

- Keep the normalized, You.com, and Tavily inputs in the same bundle folder so manifest rewrites succeed.
- Pass `--update-manifest` when producing promotable bundles; the command rewrites `leads/exa_seed.feedback_resolved.json` with its SHA256 via `tools.manifest_utils` (no secrets logged). Skip it for ad-hoc experiments.
- Capture the emitted telemetry (`feedback_resolver` module) so auditors can confirm the recorded SHA.
- Need fixtures? `tests/fixtures/bundles/feedback_case/` contains the canonical bundle referenced by `pytest -k "feedback_resolver" -q`.

> **Integration plan (FSQ‑019)**: Day‑1 automation will land this CLI between `tools.normalize_and_resolve` and `pipelines.day1.unified_verify`. Until then, operators should run it manually on any bundle that will flow into unified verification so downstream stages prefer `exa_seed.feedback_resolved.json` over the raw normalized payload.

### 4. Deployment (Render.com)

- Connect your GitHub repo to Render
- Environment:
    - Set all required `.env` variables (especially `DATABASE_URL`)
- Build Command: `pip install -r requirements.txt`
- Start Command: `uvicorn app.main:app --host 0.0.0.0 --port $PORT`
- Health Check Path: `/health`
- Instance Type: Starter ($7/mo)

***

## Database Schema

**Run in Supabase SQL editor:**

```sql
-- Companies, user deliveries, feedback, users tables (see /schema/ in repo for full DDL)
-- Core: companies (id, name, funding, confidence, AI score, signals, verified_by, source_urls, timestamps), user_deliveries, feedback, users
```

See [schema.sql](schema.sql) for the exact definitions.

***

## Environment Variable Reference

```env
APP_NAME="FundSignal"
APP_VERSION="0.1.0"
ENVIRONMENT="development"
DEBUG="true"
LOG_LEVEL="INFO"
TELEMETRY_FORMAT="json"
TELEMETRY_PATH="logs/pipeline.log"
DATABASE_URL="postgresql://postgres:<password>@<host>.supabase.co:5432/postgres"
SECRET_KEY="your_secret"
CORS_ORIGINS='["http://localhost:3000","http://localhost:8000"]'
SENTRY_DSN=""
HOST="0.0.0.0"
PORT="8000"
EXA_API_KEY=""
TAVILY_API_KEY=""
TWITTER_API_KEY=""
OPENAI_API_KEY=""
SCORING_MODEL="gpt-4o-mini"
SCORING_SYSTEM_PROMPT_PATH="configs/scoring/system_prompt.md"
SCORING_TEMPERATURE="0.2"
SLACK_BOT_TOKEN=""
RESEND_API_KEY=""
```

***

## Day 1: Exa Discovery Pipeline

- Add `EXA_API_KEY` to `.env` (keep `LOG_LEVEL=INFO`, `ENVIRONMENT=development`, and telemetry defaults unless you want plain text logs).
- (Optional) Start Postgres for inserts: `docker compose up -d db`.
- Seed discovery data: `python -m pipelines.day1.exa_discovery --days_min=60 --days_max=90 --limit=80`.
- Inspect output: open `leads/exa_seed.json` or run `python -m tools.peek leads/exa_seed.json | head -n 50`.
- Verify parsing rules with `pytest -k test_exa_discovery`.

***

## Day 1: You.com Verification Pipeline

- Add `YOUCOM_API_KEY` to `.env` (keep API keys out of logs).
- Ensure Exa seed exists at `leads/exa_seed.json`.
- Run verification: `python -m pipelines.day1.youcom_verify --input=leads/exa_seed.json --min_articles=2`.
- Inspect results: `python -m tools.peek leads/youcom_verified.json | head -n 50`.
- Run the targeted tests: `pytest -k test_youcom_verify`.

***

## Day 1: Tavily Confirmation Pipeline

- Add `TAVILY_API_KEY` to `.env`.
- Ensure You.com results exist at `leads/youcom_verified.json`.
- Run cross-confirmation: `python -m pipelines.day1.tavily_confirm --input=leads/youcom_verified.json --min_confirmations=2`.
- Inspect proof links: `python -m tools.peek leads/tavily_confirmed.json | head -n 50`.
- Run the targeted tests: `pytest -k test_tavily_confirm`.
- Proof-link strategy: dedupe by domain, keep the top confirming URLs, store them in `proof_links` for UX transparency.

***

## Day 2: ChatGPT Scoring Engine

- Set `OPENAI_API_KEY`, `SCORING_MODEL`, and `SCORING_SYSTEM_PROMPT_PATH` inside `.env`. Run in fixture mode (`FUND_SIGNAL_MODE=fixture`) to use the deterministic rubric, or switch to `online` to call OpenAI. Tune `PROOF_CACHE_TTL_SECONDS` if you need longer-lived proof hydration results during load tests.
- Boot the API (`uvicorn app.main:app --reload`) and submit a scoring job:

```bash
curl -X POST http://localhost:8000/api/scores \
  -H "Content-Type: application/json" \
  -d '{
    "company_id": "a5dcb3f4-29f3-4bb0-8d7f-81f5d93eeb01",
    "name": "Acme SaaS",
    "funding_amount": "$10M",
    "funding_stage": "Series A",
    "days_since_funding": 75,
    "employee_count": 40,
    "job_postings": 6,
    "tech_stack": ["Salesforce", "HubSpot"],
    "buying_signals": ["https://techcrunch.com/acme"],
    "verified_sources": ["Exa", "You.com"],
    "scoring_run_id": "daily-2024-10-29"
  }'
```

The API responds with a persisted `CompanyScore` object (0–100 score, rubric breakdown, recommended approach, pitch angle). Results are cached by `company_id + scoring_run_id`; repeat calls reuse cached runs in ≤300 ms until `force=true` is supplied. Use `GET /api/scores/<company_id>?scoring_run_id=<run>` to retrieve stored outputs for downstream UI or delivery channels. Errors from OpenAI, Exa, You.com, or Tavily surfaces are logged with context and mapped to API codes (`429_RATE_LIMIT`, `502_OPENAI_UPSTREAM`, `422_INVALID_COMPANY_DATA`) without exposing secrets. The scoring system prompt lives at `configs/scoring/system_prompt.md` for quick updates.

Every breakdown item now carries a primary `proof` plus a `proofs` array so downstream UIs can render single or multi-link evidence per signal:

```json
{
  "reason": "Raised $10M Series A (75 days ago)",
  "points": 30,
  "proof": {
    "source_url": "https://techcrunch.com/acme",
    "verified_by": ["Exa", "You.com", "Tavily"],
    "timestamp": "2025-10-29T09:15:00Z"
  },
  "proofs": [
    {
      "source_url": "https://techcrunch.com/acme",
      "verified_by": ["Exa", "You.com", "Tavily"],
      "timestamp": "2025-10-29T09:15:00Z"
    },
    {
      "source_url": "https://presswire.com/acme",
      "verified_by": ["Exa", "Tavily"],
      "timestamp": "2025-10-29T09:30:00Z"
    }
  ]
}
```

Pass structured signal metadata in the scoring request via the optional `signals` array:

```json
"signals": [
  {
    "slug": "funding",
    "source_url": "https://techcrunch.com/acme",
    "timestamp": "2025-10-29T09:15:00Z",
    "verified_by": ["Exa", "You.com"]
  }
]
```

When a slugged signal is provided, the proof-link hydrator reuses every matching piece of evidence; otherwise it falls back to the sanitized `buying_signals` list (deduped, order-preserving) or built-in default proof URLs. Each breakdown item exposes `proof` (legacy single entry) plus the full `proofs` array so UIs can render multiple links. Missing or unreachable evidence results in API errors using the `404_PROOF_NOT_FOUND` or `424_EVIDENCE_SOURCE_DOWN` codes so clients can remediate quickly.

Regression guard: run `pytest tests/services/test_chatgpt_engine.py -k regression` to ensure the scoring rubric keeps the canonical high/medium/low personas in their expected bands. Update `tests/fixtures/scoring/regression_companies.json` (or set `SCORING_REGRESSION_FIXTURE`) whenever rubric weights or persona definitions change so the suite reflects the new intuition.

Bundle regression guard: run `pytest tests/services/test_chatgpt_engine.py -k bundle_regression` to replay curated bundle excerpts with human-labeled tiers. Update `tests/fixtures/bundles/intuition_regression/bundle_companies.json` (or override `SCORING_BUNDLE_FIXTURE_DIR`) whenever new bundles are captured so drift detection mirrors production data.

***

## Deterministic Confidence Scoring

- Set `FUND_SIGNAL_MODE=fixture` and `FUND_SIGNAL_SOURCE=local` to stay fully offline, then point the pipeline at a canonical bundle root: `python -m pipelines.day1.confidence_scoring --input ./fixtures/latest --output leads/day1_output.json`.
- Required artifacts (auto-read from the bundle): `leads/youcom_verified.json`, `leads/tavily_confirmed.json`, and optional `exa_seed.json` (looked up in `leads/` first, then `raw/`).
- Confidence tiers are deterministic and source-counted: ≥3 sources → `VERIFIED`, 2 sources → `LIKELY`, else `EXCLUDE`. Proof links are deduped by publisher+URL, sanitized to drop any `*key` query params, and sorted for reproducibility.
- The export is a stable JSON object: `{schema_version, bundle_id, captured_at, leads:[{company, confidence, verified_by, proof_links, captured_at}]}` sorted alphabetically by company. The pipeline logs bundle metadata, tier counts, runtime, and the SHA256 hash of the output. Use `--ignore-expiry` only when you intentionally want to bypass expired fixtures.
- Tests: `pytest -k "test_confidence_scoring or test_determinism"`. Determinism can also be verified manually via `sha256sum leads/day1_output.json`.

## Evidence Integrity Verification (Day 2)

- Run `python -m pipelines.qa.proof_link_monitor --input leads/day1_output.json --supabase-table proof_link_audits` to HEAD-check every proof link emitted by the scoring pipeline. The CLI dedupes URLs, runs asynchronously (configurable via `PROOF_QA_CONCURRENCY`), retries transient failures, and persists each attempt to the `proof_link_audits` Supabase table (UPSERT keyed by `proof_hash,last_checked_at`).
- Env vars: `SUPABASE_URL`, `SUPABASE_SERVICE_KEY`, `SUPABASE_PROOF_QA_TABLE`, `PROOF_QA_CONCURRENCY`, `PROOF_QA_RETRY_LIMIT`, `PROOF_QA_FAILURE_THRESHOLD`, `PROOF_QA_ALERT_WEBHOOK`, and the kill switch `PROOF_QA_DISABLE_ALERTS=true` for safe rollouts. Defaults live in `.env.example`.
- Proof freshness: every `SignalProof` carries a timestamp and must be younger than `PROOF_MAX_AGE_DAYS` (default 90). Stale proofs raise `422_PROOF_STALE` and never reach explainability drawers, so keep bundle fixtures updated if they drift past the threshold.
- Observability: every run logs `proof_qa.checks_total`/`proof_qa.failures_total` and alerts whenever ≥3% of links fail or a proof fails twice inside 24h. Alert payloads summarize the affected company + slug without leaking webhook URLs or Supabase secrets.
- Error semantics: timeouts map to `504_HEAD_TIMEOUT`, TLS failures to `523_TLS_HANDSHAKE_FAILED`, and systemic issues raise `ProofLinkMonitorError` with codes like `598_TOO_MANY_FAILURES` so Render/CI can gate deployments.
- Downstream readers (explainability drawers, Supabase dashboards) can now pull `last_checked_at`, `last_success_at`, and `http_status` per proof, ensuring dead links are hidden before Slack/email bundles go out.

## Retention & Compression Policy

- **Compression:** Every capture bundle writes raw payloads under `<bundle>/raw/`. Run `python -m tools.compress_raw_data --input <bundle_dir>` (automated in the nightly workflow) to convert any `.json`/`.jsonl` into `.jsonl.gz`, removing the originals once the stream is safely written.
- **Local retention:** Enforce the 30-day raw / 90-day canonical SLA with `python -m tools.enforce_retention --path artifacts --dry-run` to review and `--delete --report retention-report.json --raw-days 30 --canonical-days 90` to apply. The CLI records deleted paths, bytes reclaimed, and any permission issues, and validates that retention windows are always positive.
- **Bucket lifecycle:** Mirror the same policy in Supabase/S3 via `python -m tools.apply_bucket_lifecycle --bucket fundsignal-artifacts --raw-days 30 --canonical-days 90 --output bucket-lifecycle.json` (add `--dry-run` to preview), then apply the generated JSON through your storage console or API.
- **Config:** Override retention windows via `RETENTION_RAW_DAYS` and `RETENTION_CANONICAL_DAYS` (see `.env.example`). Defaults are 30/90 days; the nightly workflow now exports the same env vars so local runs, CI, and retention automation stay aligned.

***

## Day 1–7 MVP Roadmap

| Day | Goal                              | Deliverable                                  |
|-----|-----------------------------------|----------------------------------------------|
| 1   | Baseline setup                    | FastAPI live locally/on Render, DB connected |
| 2   | Data pipeline                     | Exa/Tavily/Twitter integrations, DB storage  |
| 3   | AI scoring                        | OpenAI scores & explainability               |
| 4   | Delivery channels                 | Slack, Email, CSV export automation          |
| 5   | Feedback loop                     | Feedback endpoints, per-lead forms           |
| 6   | Payments & landing                | Stripe integration, landing page, sample     |
| 7   | Beta launch                       | User onboarding, feedback round              |

***

## Pricing Model

| Tier     | Price   | Details                                                   |
|----------|---------|-----------------------------------------------------------|
| Starter  | $149/mo | 25 verified/week, Slack+Email                             |
| Pro      | $247/mo | 75/week, enrichment, Airtable sync                        |
| Team     | $499/mo | Unlimited, CRM/API, dedicated support                     |

**Early Adopter:** $49/mo for first 50 users (lifetime).

***

## Development, Testing & CI

- **Trunk-based:** Only use the `main` branch.
- **CI:** GitHub Actions checks on push; release workflow for Docker builds.
- **Tests/Lint:** Run `pytest` and `ruff` as in template for code quality.
- **Metrics:** `/metrics` for Prometheus integration (optional).
- **Observability:** Sentry error tracking via `SENTRY_DSN` (optional).
- **uv cache:** Set `UV_CACHE_DIR=$(pwd)/.uv-cache` (Makefile exports this automatically) to keep uv’s cache inside the repo and avoid permission issues on locked-down runners.

***

## Security & Best Practices

- CORS protection, Secret key configuration, Trusted host middleware
- Minimal Docker context, non-root run by default
- Update all secrets for production

***

## Contributing

Open to PRs, bug reports, and feature requests. Please submit tests and pass CI before merging.

***

## License

MIT License. See [LICENSE](LICENSE) for details.

***

Your README is already comprehensive and well-structured for onboarding, deployment, and MVP delivery[1]. For optimal developer and operator experience, consider adding these additional sections:

***

### 1. **Troubleshooting & Common Issues**

Add a section to guide new developers on typical errors (such as Python version mismatches, `.env` formatting issues, or database connection errors).
```markdown
## Troubleshooting

- **Python Version Error:** Ensure `.python-version` is set to 3.12.0 for compatibility with all dependencies.
- **Env Parsing Errors:** Check `.env` for correct formatting—especially bracket syntax in `CORS_ORIGINS`.
- **Database Connection Issues:** Verify DATABASE_URL uses the `postgresql+asyncpg://` prefix for proper async support.
- **Render Deploy Fails:** Review Render logs under Events and Logs tabs for precise error output. Most issues trace to Python version, environment variable formatting, or missing secrets.
```

***

### 2. **Links to Documentation**

Provide helpful links for third-party services (especially for API setup), e.g. Supabase, Render, Exa, Tavily, Twitter, Resend, Stripe.

```markdown
## Service Documentation Links

- [Supabase Docs](https://supabase.com/docs)
- [Render Deployment Guide](https://render.com/docs/deploy-fastapi)
- [OpenAI API Reference](https://platform.openai.com/docs)
- [Resend API](https://resend.com/docs)
```

***

### 3. **Directory Structure**

Show a quick view of your repo’s major folders and their purpose.
```markdown
## Directory Structure

```
fund_signal/
├── app/               # FastAPI source code
├── tests/             # Pytest test cases
├── .env.example       # Environment variable template
├── requirements.txt   # Python dependencies
├── docker-compose.yml # Local/testing compose setup
├── .python-version    # Pin Python for Render
├── README.md          # Project documentation
```
```

***

### 4. **Release Checklist**

Document each Day 1 deliverable for future audits/releases.
```markdown
## Day 1 Release Checklist

- [x] Repo cloned, .env configured, DB connected
- [x] Local app runs and passes `/health`
- [x] Production (Render) deploy live, `/health` endpoint verified
- [x] Troubleshooting notes added to README
```

***
