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

This README combines project-specific details, MVP delivery clarity, and actionable usage steps—all mapped to your FundSignal goals and technical approach.

Sources
