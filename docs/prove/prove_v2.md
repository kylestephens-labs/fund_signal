# Prove v2 Plan — Next Quality Gates

## Goal Recap
- Extend `prove-quick/full` so they enforce the non-test gates highlighted in the Prove overview: branch discipline, context tags, coverage expectations, and safety toggles.
- Keep the contract “No green prove, no merge” by moving these checks into automation rather than doc reminders.

## Key Outcomes
- **Unified git hygiene**: commits carry Task IDs + mode tags and land from `main` unless CI overrides.
- **Measured confidence**: `prove-full` enforces minimum diff coverage and ensures new functional work ships with active kill switches.
- **Safety nets for data pipelines**: fixture freshness / hydration scripts run as part of the full gate so regressions cannot merge unseen.

## Proposed Implementation Improvements

### 1. Git Context Gates
- Add a lightweight Python script (e.g., `scripts/prove_git.py`) that checks:
  - Trunk enforcement (`git rev-parse --abbrev-ref HEAD == main` unless `CI=true`).
  - Mode detection: commit message or env var must include `[MODE:F]`/`[MODE:NF]`.
  - Context compliance: commit message contains `[T-YYYY-MM-DD-NNN]`.
  - Commit format: `type(scope): desc [T-…] [MODE:…]`.
- Wire this script into both `prove-quick` and `prove-full` (quick should fail fast before heavier work).
- Emit actionable failure tips (e.g., “Checkout main” or “Add [MODE:F]”). This mirrors gates 1–4 from `docs/prove/prove_overview.md`.

### 2. Coverage + Kill-Switch Enforcement
- Update `prove-full` to run `uv run pytest --cov=app --cov-report=term-missing` and fail if:
  - Functional mode and diff coverage < 85%.
  - Refactor mode and diff coverage < 60%.
- Use `coverage xml` + `diff-cover` (or `pytest --cov-report=xml`) to compute thresholds.
- Add a `scripts/check_killswitch.py` helper that scans diffs for feature-facing directories (`app/features`, `app/api/routes`) and ensures toggles or `settings.FeatureFlags` references were touched alongside code. Gate only applies in Functional mode.

### 3. Data/Fixture Health Checks
- Bake existing fixture guards into `prove-full` so CI and local runs stay aligned:
  - `uv run python scripts/verify-fixtures.py`
  - `uv run python scripts/check-freshness.py`
- If those scripts don’t exist yet, add TODO placeholders plus documentation so future tasks can flesh them out.
- This keeps the Day-1 fixture safety nets described in `docs/ci/cd_tasks.md` and referenced across Prove docs.

### 4. Configuration + Reporting
- Extend `prove/prove.config.example.toml` with sections describing the new gates:
  ```toml
  [gates.git]
  command = "uv run python scripts/prove_git.py"

  [gates.coverage]
  quick = false
  full_command = "make prove-full COVERAGE=1"
  ```
- Document JSON output expectations (e.g., `PROVE_JSON=1 make prove-full`) so future MCP integrations can parse structured failures.

## Tasks

### Task D: Implement git context gate script
Create `scripts/prove_git.py` (or similar) that enforces trunk, mode tags, and commit formatting. Hook it into `make prove-quick` before lint/tests and emit actionable failure messages. 📚 ESSENTIAL CONTEXT:
  • docs/prove/prove_v2.md
  • docs/prove/prove_overview.md
  • Makefile
  • scripts/ (for existing helper patterns)

### Task E: Add diff coverage + kill-switch checks
Enhance `prove-full` to run coverage with thresholds (85%/60%) and add a mode-aware kill-switch script tied to functional work. Ensure failures surface clearly in CI logs. 📚 ESSENTIAL CONTEXT:
  • docs/prove/prove_v2.md
  • docs/prove/prove_overview.md
  • Makefile
  • tests/ + coverage tooling docs

### Task F: Integrate fixture freshness into prove-full
Run the existing fixture verification scripts (or add skeletons) inside `prove-full` so data regressions are caught automatically. Document how to skip them only when absolutely necessary (env var). 📚 ESSENTIAL CONTEXT:
  • docs/prove/prove_v2.md
  • docs/ci/cd_tasks.md
  • scripts/verify-fixtures.py (or equivalent)
  • Makefile
