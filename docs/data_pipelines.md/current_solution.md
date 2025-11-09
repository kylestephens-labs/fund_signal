# Faster Solution (Fixture + Capture Architecture)

## High-Level Problem
Day‑1 pipelines originally relied on live You.com/Tavily APIs, but local dev/CI run in an offline sandbox. We needed a deterministic, explainable workflow that still produces canonical leads without outbound network access, while keeping raw vendor data governed and compliant.

## Two-Mode Architecture
| Mode | Purpose | Behavior |
|------|---------|-----------|
| **Capture (online)** | Network-enabled runner produces canonical bundles | `tools.capture_pipeline` hits Exa → You.com → Tavily, writes `raw/`, `leads/`, `manifest.json`, verifies integrity, uploads/archives bundle. |
| **Fixture (offline)** | Default for dev + CI | Pipelines load bundles via `pipelines.io.fixture_loader`, enforce manifest checks/expiry, and never hit external APIs. |

Environment variables (`FUND_SIGNAL_MODE`, `FUND_SIGNAL_SOURCE`) switch between modes without code changes.

## Key Outcomes
- Deterministic bundle artifacts with raw `.jsonl.gz`, canonical JSON, manifest metadata, and SHA verification.
- Automated compression + retention (30‑day raw, 90‑day canonical) with `retention-report.json` for audits.
- Nightly capture workflow logs masked secrets, validates egress, checks key rotation, and publishes bundle SHA.
- CI runs `test_verify_fixtures`, `test_compression`, `test_retention`, `test_confidence_scoring`, `test_determinism` to catch regressions.

## Solution Implemented
1. **Capture Pipeline** (`tools.capture_pipeline`): throttled Exa/You.com/Tavily fetch, writes `artifacts/YYYY/MM/DD/bundle-<timestamp>/` with `raw/`, `leads/`, `manifest.json`.
2. **Verification Before Mutation**: `python -m tools.verify_bundle --manifest <bundle>/manifest.json` immediately after capture while raw JSON exists.
3. **Compression + Retention**:
   ```bash
   python -m tools.compress_raw_data --input <bundle>
   python -m tools.enforce_retention --path artifacts --dry-run
   python -m tools.enforce_retention --path artifacts --delete --report retention-report.json
   ```
4. **Pipelines** (online for live reruns, fixture for CI):
   ```bash
   python -m pipelines.day1.youcom_verify --input /tmp/exa_seed_array.json --output <bundle>/leads/youcom_verified.json
   python -m pipelines.day1.tavily_confirm --input <bundle>/leads/youcom_verified.json
   python -m pipelines.day1.confidence_scoring --input <bundle> --output <bundle>/leads/day1_output.json
   ```
5. **Archival**: `tar -czf archives/bundle-<id>.tar.gz <bundle> retention-report.json` plus `sha256sum` for reproducibility.
6. **Docs/Tests** updated in README + network_modes; CI enforces fixture determinism and retention/compression rules.

## User Interaction & Verification Checklist
1. **Capture**
   ```bash
   python -m tools.capture_pipeline --input leads/exa_seed.json --out artifacts/dry-run --concurrency 1 --qps-youcom 0.2 --qps-tavily 1 --expiry-days 7
   ```
2. **Verify Manifest Immediately**
   ```bash
   python -m tools.verify_bundle --manifest artifacts/dry-run/YYYY/MM/DD/bundle-*/manifest.json
   ```
3. **Compress Raw Payloads**
   ```bash
   python -m tools.compress_raw_data --input artifacts/dry-run/YYYY/MM/DD/bundle-*
   ```
4. **Retention**
   ```bash
   python -m tools.enforce_retention --path artifacts --dry-run
   cat retention-report.json
   python -m tools.enforce_retention --path artifacts --delete --report retention-report.json
   ```
5. **Online Pipelines (optional fresh rerun)**
   ```bash
   export FUND_SIGNAL_MODE=online FUND_SIGNAL_SOURCE=local
   python -m pipelines.day1.youcom_verify --input /tmp/exa_seed_array.json --output <bundle>/leads/youcom_verified.json
   python -m pipelines.day1.tavily_confirm --input <bundle>/leads/youcom_verified.json
   python -m pipelines.day1.confidence_scoring --input <bundle> --output <bundle>/leads/day1_output.json
   sha256sum <bundle>/leads/day1_output.json
   ```
6. **Fixture/CI Regression Tests**
   ```bash
   FUND_SIGNAL_MODE=fixture FUND_SIGNAL_SOURCE=local pytest tests/test_verify_fixtures.py -q
   pytest -k "test_compression or test_retention"
   pytest -k "test_confidence_scoring or test_determinism"
   ```
7. **Archive for Audit**
   ```bash
   tar -czf archives/bundle-<id>.tar.gz <bundle> retention-report.json
   sha256sum archives/bundle-<id>.tar.gz
   ```

Following these steps ensures the faster, two-mode solution operates correctly, keeps data compliant, and provides reproducible bundles with verifiable hashes.
