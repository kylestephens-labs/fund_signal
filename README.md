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

### Key API Endpoints (backend-only)

- `POST /auth/magic-link` → issue magic link token (verify via `/auth/magic-link/verify`); accepts optional `plan_id` (Stripe price ids when configured, otherwise `starter|pro|team`) and is rate-limited per email
- `POST /auth/otp` → issue OTP (verify via `/auth/otp/verify`); accepts optional `plan_id` and enforces the same rate limits
- `GET /auth/google/url` → returns Google OAuth consent URL + state (uses `GOOGLE_CLIENT_ID`/`GOOGLE_REDIRECT_URI`; optional `plan_id`)
- `POST /auth/google/callback` → exchanges code for token, fetches user info, and returns a verified session
- `GET /leads` → list leads with optional `score_gte` and `limit`
- `POST /delivery/weekly` → queue weekly email/Slack artifact generation (stubbed locally)
- `POST /billing/stripe/webhook` → Stripe webhook receiver (idempotent)
- `POST /billing/cancel` → cancel subscription and log reason


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

### Quality Gates (Prove)

All engineers and Codex agents run the same lint + test bar before handoff:

```bash
make setup-dev           # Creates .venv via uv and installs base+dev deps
make prove-quick         # Ruff format check + lint + pytest -q $(PYTEST_FLAGS => -m "not slow and not contract")
make prove-full          # Same checks with the full pytest suite via $(PYTEST_FULL_FLAGS)
```

Override `PYTEST_FLAGS` (e.g., `PYTEST_FLAGS='-m "not slow and not contract"'`) or `PYTEST_FULL_FLAGS` when you need to target specific pytest markers. `prove-quick` stays <1 minute for the inner loop by skipping `slow` pipeline/benchmark suites and `contract` tests that hit external APIs; `prove-full` mirrors CI and is the hook point for additional gates (mypy, fixture verification, contracts) documented in `docs/prove/prove_v1.md`.

#### Prove CLI config (drop-in)

When the Prove CLI lands, copy the example config so automation can run the same commands you run locally:

```bash
cp prove/prove.config.example.toml prove/prove.config.toml
prove --config prove/prove.config.toml --gate quick   # Runs make prove-quick under the hood
prove --config prove/prove.config.toml --gate full    # Runs make prove-full
```

The TOML lists the project metadata plus the exact quick/full commands (`make prove-quick` / `make prove-full`). Keep secrets out of the config—Prove only shells out to those make targets, so lint/test logs and failures still appear in your terminal just like today. When those commands change, update both the Makefile targets and `prove/prove.config.example.toml` together (see `docs/prove/prove_v1.md` for the gate contract).

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

### Day-3 Delivery Pipelines (Persistence-Aware)

1. **Seed deterministic scores**

   ```bash
   uv run python scripts/seed_scores.py \
     --fixture tests/fixtures/scoring/regression_companies.json \
     --scoring-run demo-day3 \
     --seed-all \
     --force
   ```

   The CLI now seeds every persona (or a specific `--company-id`) directly into Supabase/Postgres so Day-3 pipelines, Supabase dashboards, and the UI drawer all read the same persisted `CompanyScore` payloads after API restarts.

2. **Render the email digest from Supabase**

   ```bash
   export DELIVERY_SCORING_RUN=demo-day3
   uv run python -m pipelines.day3.email_delivery --output output/email_demo.md
   ```

   The email renderer fetches cached scores through `SupabaseScoreRepository.list_run`, logs `delivery.supabase.query`, and writes Markdown including recommended approaches, pitch angles, and every `proof/proofs` URL.

   **Dry run (artifact only):** keeps the default behavior for local QA/CI—Markdown is written to `output/email_demo.md`, no email leaves your machine.

   **Send mode:** add `--deliver` once SMTP credentials are configured (or set `DELIVERY_EMAIL_FORCE_RUN=true`; pass `--no-deliver` to skip a forced send) to send the digest automatically:

   ```bash
   export EMAIL_SMTP_URL=smtp://user:pass@mailtrap.io:2525
   export EMAIL_FROM="FundSignal <alerts@fundsignal.dev>"
   export EMAIL_TO=ops@fundsignal.dev,revops@example.com
   uv run python -m pipelines.day3.email_delivery --output output/email_demo.md --deliver
   ```

   The CLI validates env vars, sends via SMTP (Mailtrap/Papercut are great for staging), emits `delivery.email.sent` metrics, and writes Markdown + HTML + CSV artifacts under `DELIVERY_OUTPUT_DIR`. The HTML body mirrors Slack content, includes a top “Download CSV” link, and attaches the CSV to the outbound email for easy export.
   Set `DELIVERY_EMAIL_FORCE_RUN=true` for cron jobs that always send and pass `--no-deliver` locally to render artifacts without sending when the force flag is set.

   **Visual QA (Mailtrap/Papercut):**
   - Point `EMAIL_SMTP_URL` at your sandbox SMTP (Mailtrap/Papercut/debug server).
   - Run `uv run python -m pipelines.day3.email_delivery --output output/email_preview.md --deliver` to render Markdown/HTML/CSV under `DELIVERY_OUTPUT_DIR` and send to the sandbox inbox.
   - Open the inbox to review the HTML body and CSV attachment; artifacts stay in `output/` for audit. If `DELIVERY_EMAIL_FORCE_RUN=true`, add `--no-deliver` to generate artifacts without sending.

4. **Schedule the Monday 9 AM PT send (cron-friendly)**

   ```bash
   # Pre-seed the scoring run used by the scheduler (default demo-day3)
   DELIVERY_SCORING_RUN=demo-day3 make seed-scores

   # Or run both in one shot
   DELIVERY_SCORING_RUN=demo-day3 make email-cron-seed

   export DELIVERY_SCORING_RUN=demo-day3
   export DELIVERY_EMAIL_FORCE_RUN=true
   export EMAIL_SMTP_URL=...
   export EMAIL_FROM="FundSignal <alerts@fundsignal.dev>"
   export EMAIL_TO=ops@fundsignal.dev,revops@example.com
   # Cron example (Render/GitHub/host): 0 9 * * 1 TZ=America/Los_Angeles
   uv run python -m pipelines.day3.email_schedule --output output/email_cron.md --company-limit 25 --min-score 80 --deliver --timezone America/Los_Angeles --enforce-window
   ```

   The scheduler wrapper logs `delivery.email.schedule.start|success`, enforces Monday 09:00 when `--enforce-window` is set, and delegates to `email_delivery` to write Markdown/HTML/CSV artifacts and send via SMTP. Use Mailtrap/Papercut in staging; secrets stay in env and are never printed.

   **GitHub Actions opt-in:** `.github/workflows/day3-email-cron.yml` runs the same command on Monday at 09:00 PT (cron in UTC with a double-slot for DST) with `DELIVERY_EMAIL_FORCE_RUN=true`, seeds via `scripts/seed_scores.py`, and uploads `output/email_cron.*` artifacts on failure. Set secrets/vars: `DATABASE_URL`, `EMAIL_SMTP_URL`, `EMAIL_FROM`, `EMAIL_TO/CC/BCC`, `DELIVERY_SCORING_RUN` (defaults to `demo-day3`), `DELIVERY_OUTPUT_DIR` (defaults to `output`), and optional `EMAIL_SUBJECT`/`EMAIL_DISABLE_TLS`.

   > ⚠️ **Credential safety:** never commit SMTP creds to Git. Store them in Render/GitHub secrets (or a local `.env` that stays ignored), and test delivery with sandbox servers (Mailtrap, Papercut, `python -m smtpd -c DebuggingServer`). Set `EMAIL_DISABLE_TLS=true` only for local debug servers; production SMTP should keep TLS enabled.

### Day-3 Email DoD (delivery)

- Content parity: HTML + CSV digest matches Slack ordering/fields; artifacts saved under `DELIVERY_OUTPUT_DIR` for audit.
- Observability: logs/metrics include `delivery.supabase.query`, `delivery.email.rendered`, `delivery.email.csv_written`, `delivery.email.sent`, `delivery.email.duration_ms`; scheduler/workflow surfaces non-zero exits.
- Secret safety: SMTP creds/recipients never logged; secrets only in env/CI; when `DELIVERY_EMAIL_FORCE_RUN=true`, use `--no-deliver` to avoid unintended sends during local QA.
- Scheduler enabled/monitored: Monday 09:00 PT job wired (e.g., `.github/workflows/day3-email-cron.yml`) with required envs and artifact upload on failure.
- Visual QA documented: sandbox SMTP steps above; artifacts retained in `DELIVERY_OUTPUT_DIR` for inspection.

3. **Generate the Slack payload**

   ```bash
   uv run python -m pipelines.day3.slack_delivery --output output/slack_demo.json
   ```

   Slack payloads contain full Block Kit sections for each company plus a metadata block with serialized scores (proof arrays included). Post to your webhook via `curl -X POST -H "Content-Type: application/json" --data @output/slack_demo.json $SLACK_WEBHOOK_URL`.

   > Day-3 deliveries rely on persisted scores in Postgres/Supabase. Run `uv run python scripts/seed_scores.py --fixture tests/fixtures/scoring/regression_companies.json --scoring-run demo-day3 --seed-all --force` first to hydrate demo-day3 after restarts; both email/slack CLIs log `delivery.supabase.query` when they read from the DB.

Key environment variables (see `.env.example`):

| Variable | Purpose |
| --- | --- |
| `DATABASE_URL` | Required for Supabase/Postgres reads. |
| `DELIVERY_SCORING_RUN` | Default scoring run used by the delivery CLIs. |
| `DELIVERY_FORCE_REFRESH` | Records when a delivery job intentionally bypasses cached scores (mirrors `force=true`). |
| `DELIVERY_EMAIL_FORCE_RUN` | Defaults the CLI `--deliver` flag for cron jobs that always send (`--no-deliver` overrides per run). |
| `DELIVERY_OUTPUT_DIR` | Base directory for rendered Markdown/JSON artifacts. |
| `EMAIL_SMTP_URL` / `EMAIL_FROM` | SMTP endpoint + sender identity. |
| `EMAIL_TO` / `EMAIL_CC` / `EMAIL_BCC` | Recipient lists (comma-separated). |
| `EMAIL_SUBJECT` | Optional subject override (defaults to `FundSignal Delivery — <run>`). |
| `EMAIL_FEEDBACK_TO` | Optional mailto target for the Day-3 “Provide feedback” CTA (set to Mailtrap for sandbox). |
| `EMAIL_DISABLE_TLS` | Set to `true` only when TLS must be skipped (local debug servers). |
| `SLACK_WEBHOOK_URL` | Optional webhook recorded in Slack metadata/logs. |

Helpful targets:

```bash
make seed-scores        # seeds demo-day3 deterministically
make email-demo         # renders output/email_delivery.md
make email-demo-deliver # renders + sends via SMTP (--deliver)
make slack-demo         # renders output/slack_delivery.json
```

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

The API responds with a persisted `CompanyScore` object (0–100 score, rubric breakdown, recommended approach, pitch angle). Results are cached by `company_id + scoring_run_id`; repeat calls reuse cached runs in ≤300 ms until `force=true` is supplied. Use `GET /api/scores/<company_id>?scoring_run_id=<run>` to retrieve stored outputs for a single company, or `GET /api/scores?scoring_run_id=<run>&limit=25` to load the top cached companies for a scoring run (fueling the FundSignal UI cards and smoke tests). Errors from OpenAI, Exa, You.com, or Tavily surfaces are logged with context and mapped to API codes (`429_RATE_LIMIT`, `502_OPENAI_UPSTREAM`, `422_INVALID_COMPANY_DATA`) without exposing secrets. The scoring system prompt lives at `configs/scoring/system_prompt.md` for quick updates.

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

### Score Persistence & Supabase Setup (FSQ-036A)

- Add `DATABASE_URL` (e.g., `postgresql+asyncpg://postgres:password@localhost:5432/fastapi_db`) plus the Supabase host creds (`SUPABASE_DB_HOST`, `SUPABASE_DB_PORT`, `SUPABASE_DB_USER`, `SUPABASE_DB_PASSWORD`) to `.env`. When targeting Supabase via asyncpg, use the pooled DSN format `postgresql+asyncpg://postgres:<password>@<project>.supabase.co:6543/postgres`—the Alembic env forces TLS when it detects a Supabase host and allows you to override the CA bundle via `ALEMBIC_SUPABASE_CA_FILE` (or set `ALEMBIC_SUPABASE_TLS_INSECURE=1` only when a corporate proxy terminates TLS). Store the Supabase password in Render/GitHub env vars instead of source control.
- Alembic resolves `app.config` and `.env` based on `PYTHONPATH`. Remove any duplicate FundSignal checkouts (e.g., an outdated Desktop copy) or export `PYTHONPATH=/Users/<you>/repo/fund_signal_vscode` before running Alembic so it always loads the active repo’s configuration.
- Start Postgres via `docker compose up -d db` or point `DATABASE_URL` at Supabase, then apply migrations: `alembic upgrade head`. Migrations emit `scoring.migration.applied` logs on success. When adding new schema later, run `alembic revision --autogenerate -m "create <table>"` so JSON columns stay typed as `sqlalchemy.dialects.postgresql.JSONB`.
- Seed a deterministic score to unblock the Day-2 drawer smoke tests: `python scripts/seed_scores.py --fixture tests/fixtures/scoring/regression_companies.json --scoring-run-id ui-smoke`. Pass `--force` to overwrite the `(company_id, scoring_run_id)` pair if the row already exists.
- Repository writes now log `scoring.persistence.persisted` and reuse the `(company_id, scoring_run_id)` unique constraint so GETs stay ≤300 ms with ~1k rows. Use Supabase SQL to verify `breakdown` JSONB payloads, timestamps, and unique indexes via `EXPLAIN ANALYZE SELECT * FROM scores WHERE company_id = '...' AND scoring_run_id = '...'`.
- Enable the async DSN via `DATABASE_URL=postgresql+asyncpg://...` then let the API convert it to sync psycopg when building the SQLModel repository. Tune pooling with `DB_POOL_MIN_SIZE` / `DB_POOL_MAX_SIZE` (maps to SQLAlchemy `pool_size` / `max_overflow`) so Render workers reuse warm Supabase connections.
- After POSTing a score, restart the API and run `curl "http://localhost:8000/api/scores/<company_id>?scoring_run_id=<run>"`. The response returns instantly using the persisted JSON payload—no ChatGPT recompute—and logs `scoring.persistence.hit` so Supabase dashboards confirm cache hits survive process restarts.

### ProofLinkHydrator Load Harness

Run the synthetic load harness in fixture mode to prove the ≤300 ms P95 target and capture cache stats before deploying:

```bash
python -m tools.proof_links_load_test \
  --input tests/fixtures/scoring/regression_companies.json \
  --concurrency 16 \
  --iterations 200 \
  --report output/proof_hydrator_report.json
```

The CLI warms the hydrator cache, replays every scoring slug through `ChatGPTScoringEngine`, and prints a structured `proof_hydrator.load_test` log (plus optional JSON reports) that include cache hit/miss deltas, throughput, and latency percentiles per slug. Reports default to `PROOF_LOAD_REPORT_DIR` (if set)—point `--report` somewhere under `output/` or delete generated files to avoid clutter. CI fails with exit code `2` whenever warmed-cache P95 exceeds the configured threshold (default `300 ms`); override via `--p95-threshold-ms` when investigating regressions.

### ProofLink Cache Benchmark (FSQ-035C)

The pytest benchmark + CLI in `tools/proof_links_benchmark.py` hardens the cache SLO by replaying ≥50 fixture companies, capturing cold vs. warm stats, and asserting warmed-cache P95 stays ≤300 ms.

- **Pytest hook:** `pytest tests/benchmarks/test_proof_links_benchmark.py --benchmark-only`. Uses `pytest-benchmark` to run one cold and one warm phase (default `BENCHMARK_COLD_RUNS=25`, `BENCHMARK_RUNS=200`) and fails when `proof_links.latency_p95` exceeds `PROOF_BENCH_P95_THRESHOLD_MS` (default `300`). Cache hit ratio must remain ≥0.80, and tests emit structured log events `proof_links.benchmark` + `proof_links.latency_p95` for Render/Supabase ingestion.
- **CLI wrapper:** `python -m tools.proof_links_benchmark --input tests/fixtures/scoring/proof_links_benchmark_companies.json --report output/proof_links_metrics.json`. Reports land in `BENCHMARK_REPORT_DIR` (defaults to `output/`) with the schema `{"benchmark_version","fixture_hash","cold":{...},"warm":{...},"statsd_payload":{"proof_links.latency_p50":...}}`. JSON includes latency percentiles, cache hits/misses, throughput_qps, and a StatsD-ready payload reused by FSQ-035D alert wiring.
- **Env toggles:** `BENCHMARK_RUNS`, `BENCHMARK_COLD_RUNS`, `PROOF_BENCH_SAMPLE_SIZE`, `PROOF_BENCH_SKIP_COLD`, `PROOF_BENCH_CONCURRENCY`, and `PROOF_BENCH_P95_THRESHOLD_MS` control workload size. Developers on laptops can drop iterations or skip the cold phase, while CI keeps the defaults to validate the ≤300 ms SLO and ≥80% cache hit ratio.
- **Interpreting results:** The CLI/test prints `proof_links.benchmark {"warm_p95_ms":..., "warm_hit_ratio":..., "fixture_hash":...}` plus `statsd_payload` fields (`proof_links.latency_p50/p95/p99`, `proof_links.cache_hit_ratio`, `proof_links.throughput_qps`). Copy the emitted JSON to Supabase for trend analysis and feed the log payload into Render alerts referenced by FSQ-035B/035D.

### ProofLink Metrics & Alerts (FSQ-035D)

Enable structured metrics and alerting for hydrator + scoring flows via the new `METRICS_*` env vars (defaults shown in `.env.example`):

- `METRICS_BACKEND=stdout|statsd`, `METRICS_NAMESPACE=proof_links`, `METRICS_SAMPLE_RATE`, `METRICS_DISABLE`, and optional `METRICS_STATSD_HOST/PORT`. StatsD timers/counters publish `proof_links.hydrator.latency_ms`, `proof_links.scoring.latency_ms`, cache hits/misses, retries, and provider error codes. When running locally, leave `METRICS_BACKEND=stdout` to view structured `proof_links.metric` and `proof_links.alert` logs.
- Thresholds live in `RENDER_ALERT_THRESHOLD_P95` (default `300`) and `RENDER_ALERT_THRESHOLD_ERROR` (`0.05`). Whenever the load harness or benchmark reports P95 above the limit, both CLIs log `proof_links.latency_p95 {"value_ms":...}` and emit a `proof_links.alert` payload (`metrics_schema_version=proof-links.v1`, severity `critical`). Error rates above 5% trigger `proof_links.alert` with severity `warning`.
- The hydrator/scoring services emit per-request timings and cache counters, while `tools.proof_links_load_test` + `tools.proof_links_benchmark` publish aggregated gauges (latency percentiles, cache hit ratio, throughput, error rate) plus alerts that Render/Supabase dashboards ingest. Run `METRICS_BACKEND=stdout python -m tools.proof_links_load_test ...` or `pytest tests/benchmarks/test_proof_links_benchmark.py --benchmark-only` to validate the wiring locally.
- All alerts include `metrics_schema_version` so Supabase renderers can validate payload shape; bump this version whenever the JSON changes and update downstream dashboards accordingly.

### Provider Outage Simulation Suite

Run the deterministic outage simulations to validate retries, timeout handling, and structured logs for Exa/You.com/Tavily:

```bash
pytest tests/outages/test_proof_providers_outages.py
```

The suite injects fake clients that emit timeouts, 5xx responses, and slow (>1 s) calls, then asserts `provider.retry` and `proof_hydrator.outage_sim` logs include latency, attempt count, and sanitized slugs. Tune scenarios via `PROOF_OUTAGE_MODE`, `PROOF_OUTAGE_DELAY_MS`, `PROOF_OUTAGE_STATUS_CODE`, and `PROOF_OUTAGE_ATTEMPTS` (documented in `.env.example`) to simulate alternative outages locally without touching the test code.

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
- Run the domain freshness replay nightly via `python -m pipelines.qa.proof_domain_replay --scores scores/day2_fixture.json --bundle-id day2_fixture --supabase-table proof_domain_audits`. The CLI fetches stored `CompanyScore` payloads, resolves each proof’s redirect chain with `PROOF_REPLAY_CONCURRENCY` workers, records drift (domain change, protocol downgrade, non-2xx), and upserts the results to Supabase for dashboards/alerts (`proof_replay.checked_total`, `proof_replay.domain_mismatch_total`).
- Env vars: `PROOF_REPLAY_CONCURRENCY`, `PROOF_REPLAY_MAX_REDIRECTS`, `PROOF_REPLAY_FAILURE_THRESHOLD`, `PROOF_REPLAY_ALERT_WEBHOOK`, `PROOF_REPLAY_DISABLE_ALERTS`, `SUPABASE_PROOF_REPLAY_TABLE`, plus optional `PROOF_REPLAY_SCHEDULE_CRON` for job schedulers. Set `PROOF_REPLAY_DISABLE_ALERTS=true` during dry runs; leave false so insecure redirects trigger notifications immediately.

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

### UI Smoke Drawer Tests

Use the deterministic persona baked into `tests/fixtures/scoring/regression_companies.json` to verify the Supabase-backed “Why this score?” drawer end-to-end:

1. Export `DATABASE_URL`, `UI_SMOKE_COMPANY_ID`, `UI_SMOKE_COMPANY_NAME`, and `UI_SMOKE_SCORING_RUN_ID` (see `.env.example` for defaults) and ensure Postgres + the FastAPI server are running against the same database.
2. Seed the persona with `make ui-smoke-seed`, which logs `ui_smoke.seed.success` once the `(company_id, scoring_run_id)` row exists.
3. Run `npm install --prefix frontend` (first time only), then `npx playwright install --with-deps` if prompted, and execute the smoke test via `npx playwright test --config frontend/tests/playwright.config.ts --grep "Why this score"` while a UI is available at `UI_BASE_URL` and pointed to the API specified by `API_BASE_URL`.
4. For CI, set `CI_UI_SMOKE_ENABLED=true` plus the `UI_SMOKE_*`, `UI_BASE_URL`, `API_BASE_URL`, and `DATABASE_URL` secrets/variables so `.github/workflows/ui-smoke.yml` gates pull requests with Playwright artifacts (screenshots, traces, video) attached on failure. Artifacts land under `frontend/artifacts/test-results` and `frontend/artifacts/report` for debugging.

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
