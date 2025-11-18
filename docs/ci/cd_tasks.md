You’re not overly concerned—the current GitHub CI only runs the Day‑1 fixture gates (verify-fixtures, check-freshness), so lint/type/test regressions can slip through and nothing is automatically deploying anywhere. I’d treat this as an immediate hardening target:

Add a fast feedback job on PRs/main that runs uv pip install -r requirements*.txt, ruff check, ruff format --check, pytest (unit subset), and mypy/pyright if we adopt typing. This can reuse the existing matrix or a dedicated job so we keep fixture checks but regain the standard quality bar.

Split “slow” suites (proof hydrator load, outage sims, Playwright smoke) into scheduled workflows or opt‑in labels; they’re valuable but shouldn’t gate every push when they take minutes and often fail because required env vars/providers aren’t configured.

Bring back staged deployments: wire Render via a render.yaml or GitHub deploy key so a successful main build auto-deploys to staging; then promote to prod either on tag or manual approval. Our release.yml already builds/pushes a container, so we mostly need the Render deploy steps plus environment secrets.

Codify smoke tests in CI/CD: e.g., after deploying to staging, run uvicorn ... health check and a lightweight Playwright “Why this score?” drawer spec (pointed at staging data) to guarantee persistence wiring works before prod promotion.

Document the prove gates: update Makefile/PR template so engineers run make lint test locally and know CI will enforce the same; this keeps local+CI parity and avoids “works on my machine” drift.

If we tackle those, GitHub becomes the enforcement point rather than relying on everyone to remember all commands, and Render deployments stop lagging behind main. Let me know if you’d like a dedicated FSQ to plan the CI/CD overhaul.
