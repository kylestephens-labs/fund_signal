# Prove v3 Plan — Final Gate Coverage

## Goal Recap
- Close the remaining Prove gates (API contracts, lockfile/engines, TDD/diff coverage refinements, mode-aware automation) so `make prove-full` mirrors the complete spec in `docs/prove/prove_overview.md`.
- Enable JSON/structured output so CI/MCP can consume results and drive dashboards.

## Key Outcomes
- **Contract safety**: API schema and webhook changes validated automatically before merge.
- **Environment consistency**: Lockfile + runtime versions enforced, preventing “works on my machine” drift.
- **Mode-aware reporting**: Functional vs non-functional flavors automatically toggle tests, coverage, and kill-switch requirements.
- **Automation-ready output**: Every gate emits structured logs ready for Prove CLI or future orchestrators.

## Proposed Implementation Improvements

### 1. API Contract Validation
- Create `scripts/check_contracts.py` (or reuse existing tooling) that:
  - Validates OpenAPI/JSON schema files changed in the diff.
  - Detects breaking response changes (removing fields, changing types).
  - Validates webhook payloads if present.
- Invoke this script in `prove-full` only when API files change; allow an opt-out env var for local debugging.

### 2. Lockfile & Engine Enforcement
- Add `scripts/check_lockfile.py` to ensure `requirements.txt`, `uv.lock`, and `requirements-dev.txt` stay synchronized with virtualenv metadata.
- Verify Python version via `pyproject.toml` or `.python-version` so contributors match the supported interpreter.
- Hook this into both `prove-quick` (fast fail if lockfile isn’t updated) and `prove-full` (strict enforcement).

### 3. Mode-Oriented TDD + Diff Coverage
- Extend the coverage logic from v2:
  - Functional mode: require companion tests for changed source files (e.g., via `pytest --collect-only` diff detection or `git` heuristics).
  - Non-functional mode: allow lower coverage but still ensure no tests were removed without justification.
- Emit warnings in `prove-quick` and hard failures in `prove-full` for missing tests/diff coverage.

### 4. Structured Reporting + JSON Output
- Wrap all gate scripts with a shared reporter (e.g., `scripts/prove_reporter.py`) that:
  - Aggregates pass/fail status per gate.
  - Supports `PROVE_JSON=1` to output machine-readable results.
  - Provides clear remediation text for humans.
- Document how CI should capture this JSON artifact and display it.

## Tasks

### Task G: Implement API contract gate
Build `scripts/check_contracts.py`, wire it into `prove-full`, and ensure it catches OpenAPI/webhook regressions automatically. Include guidance on how to update schemas safely. 📚 ESSENTIAL CONTEXT:
  • docs/prove/prove_v3.md
  • docs/prove/prove_overview.md
  • app/api/ (or equivalent API directories)
  • Makefile

### Task H: Add lockfile/engine enforcement
Create and integrate tooling that verifies lockfiles + Python versions, failing fast during `prove-quick` if mismatched. Document remediation steps. 📚 ESSENTIAL CONTEXT:
  • docs/prove/prove_v3.md
  • pyproject.toml / requirements*.txt / uv.lock
  • Makefile

### Task I: Mode-aware TDD + JSON reporting
Extend coverage enforcement with TDD checks and add the shared reporter so `PROVE_JSON=1 make prove-full` produces structured output for CI/MCP. 📚 ESSENTIAL CONTEXT:
  • docs/prove/prove_v2.md
  • docs/prove/prove_v3.md
  • scripts/ (coverage helpers)
  • docs/prove/future_implementation.md
