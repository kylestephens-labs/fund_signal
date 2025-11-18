You’re not overly concerned—the current GitHub CI only runs the Day‑1 fixture gates (verify-fixtures, check-freshness), so lint/type/test regressions can slip through and nothing is automatically deploying anywhere. I’d treat this as an immediate hardening target:

Add a fast feedback job on PRs/main that runs uv pip install -r requirements*.txt, ruff check, ruff format --check, pytest (unit subset), and mypy/pyright if we adopt typing. This can reuse the existing matrix or a dedicated job so we keep fixture checks but regain the standard quality bar.

Split “slow” suites (proof hydrator load, outage sims, Playwright smoke) into scheduled workflows or opt‑in labels; they’re valuable but shouldn’t gate every push when they take minutes and often fail because required env vars/providers aren’t configured.

Bring back staged deployments: wire Render via a render.yaml or GitHub deploy key so a successful main build auto-deploys to staging; then promote to prod either on tag or manual approval. Our release.yml already builds/pushes a container, so we mostly need the Render deploy steps plus environment secrets.

Codify smoke tests in CI/CD: e.g., after deploying to staging, run uvicorn ... health check and a lightweight Playwright “Why this score?” drawer spec (pointed at staging data) to guarantee persistence wiring works before prod promotion.

Document the prove gates: update Makefile/PR template so engineers run make lint test locally and know CI will enforce the same; this keeps local+CI parity and avoids “works on my machine” drift.

If we tackle those, GitHub becomes the enforcement point rather than relying on everyone to remember all commands, and Render deployments stop lagging behind main. Let me know if you’d like a dedicated FSQ to plan the CI/CD overhaul.

Now that FSQ-037A exists, wire a scheduled `email-demo-deliver` job (or Render cron) that exports `EMAIL_SMTP_URL`, `EMAIL_FROM`, `EMAIL_TO`, and `DELIVERY_EMAIL_FORCE_RUN=true` so staging automatically exercises the SMTP path nightly. Capture the emitted `delivery.email.sent` metric/log in whatever dashboard we settle on to confirm the channel stays green before shipping to customers, and remind operators they can pass `--no-deliver` locally to bypass the forced send when debugging.

### Automating Day‑3 Email Delivery

1. **Staging dry run**
   - Seed a deterministic run (`make seed-scores DELIVERY_SCORING_RUN=demo-stage`).
   - In Render/GitHub/cron, export the SMTP secrets plus scoring run overrides:
     ```bash
     DELIVERY_SCORING_RUN=demo-stage \
     DELIVERY_EMAIL_FORCE_RUN=true \
     EMAIL_SMTP_URL=$SMTP_STAGING_URL \
     EMAIL_FROM="FundSignal Staging <alerts@fundsignal.dev>" \
     EMAIL_TO=ops+staging@fundsignal.dev \
     make email-demo-deliver
     ```
   - Monitor `delivery.email.sent` logs/metrics and the artifact under `output/email_demo.md`. Run without `--deliver` first (or set `DELIVERY_EMAIL_FORCE_RUN=false`) to verify content before enabling send mode.

2. **Production cadence**
   - Schedule the same command weekly (e.g., Monday 08:45 local) with production secrets for `EMAIL_*` and a real scoring run (`DELIVERY_SCORING_RUN=prod-weekly`).
   - Store all SMTP credentials in the platform’s secrets manager—never in Git. For Mailtrap/local SMTP tests set `EMAIL_DISABLE_TLS=true`; production SMTP should keep TLS enabled.

3. **Rollback / pause**
   - Disable the cron or set `DELIVERY_EMAIL_FORCE_RUN=false` to fall back to artifact-only mode.
   - Operators can still invoke `make email-demo` manually or rerun `make email-demo-deliver --no-deliver` to inspect content without sending.

Reference: README “Day‑3 Delivery Pipelines” section lists all required env vars and the CLI flags; use those instructions when building CI/CD jobs.
