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
| `FUND_SIGNAL_FIXTURE_DIR` | `fixtures/sample` | Local path that stores captured artifacts for fixture mode. |

In sandbox/CI, keep the defaults so no outbound network occurs. The capture job (GitHub Actions/runner) switches to `FUND_SIGNAL_MODE=online` and uploads new fixtures to Supabase before developers sync them down.

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
SLACK_BOT_TOKEN=""
RESEND_API_KEY=""
```

***

## Day 1: Exa Discovery Pipeline

- Add `EXA_API_KEY` to `.env` (keep `LOG_LEVEL=INFO`, `ENVIRONMENT=development` by default).
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

## Day 1: Confidence Scoring & Freshness

- Input: `leads/tavily_confirmed.json`; run `python -m pipelines.day1.confidence_scoring --input=... --output=leads/day1_output.json`.
- Scoring rules: 3 sources (Exa, You.com, Tavily) → `VERIFIED`, 2 sources → `LIKELY`, otherwise `EXCLUDE`.
- Export includes only VERIFIED/LIKELY rows plus `verified_by`, `last_checked_at` (UTC ISO8601), and `freshness_watermark` (`Verified by: … • Last checked: … • Confidence: HIGH/MEDIUM/LOW`).
- Tests: `pytest -k test_confidence_scoring`.

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
