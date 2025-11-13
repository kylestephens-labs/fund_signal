# FundSignal Overview

FundSignal is an automated funding-signal fabric that supplies outbound sales teams with live, confidence-ranked SaaS prospects. Three independent AI search engines (Exa, Tavily, Twitter/You.com) continuously pull fresh signals, while an OpenAI scoring layer cross-validates sources, generates proof snippets, and ranks each company by buying readiness. The workflow ingests target lists (or open web), runs parallel capture/verification jobs, stores results in Supabase with explainable metadata, and pushes curated drops to Slack, email, or CSV so reps never open a dashboard.

## Main Features
- **Multi-source verification**: Each lead includes linked evidence from all three search providers, sharply reducing false positives and manual research overhead.
- **AI scoring & explainability**: GPT-4 assigns prioritization scores plus human-readable reasoning, giving reps the context they need for tailored outreach.
- **Fixture-based pipelines**: Offline-friendly ingestion lets engineers develop safely while nightly CI capture jobs run networked, keeping data fresh without exposing secrets.
- **Turnkey delivery**: Slack/email automations and Stripe billing package FundSignal as a drop-in signal subscription for SaaS sales orgs.
- **Cloud-native ops**: FastAPI backend, Supabase storage, GitHub Actions scheduling, and Render deployment enable rapid iteration with minimal infrastructure lift.

## Core Workflow
1. **Ingest targets or open-web discovery** via Exa, Tavily, and Twitter/You.com search adapters.
2. **Cross-validate & deduplicate** signals with shared company identity resolution and metadata normalization.
3. **Score and explain** using the OpenAI layer to produce confidence ratings, timing signals, and proof snippets.
4. **Persist to Supabase** with structured JSON blobs so downstream automations can query or enrich with zero-ETL overhead.
5. **Deliver curated drops** to Slack channels, email digests, and optional CSV exports—optimized for “no-dashboard” consumption.
