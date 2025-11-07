High-Level Problem

Day‑1 pipelines depend on live You.com and Tavily APIs, but the sandbox (and CI) are offline-only—no DNS or outbound HTTPS.
Exa data works because we already cached it to JSON; You.com/Tavily don’t, so downstream verification, proof links, and confidence scoring can’t run.
We need a repeatable way to capture fresh multi-source data without weakening sandbox isolation.
Overall Solution

Adopt a two-mode architecture:

Capture Mode (online) runs on a secured GitHub Actions/self-hosted runner with outbound access. It fetches Exa/You.com/Tavily data, normalizes it, and uploads artifacts (JSON/JSONL + manifest) to a private Supabase storage bucket.
Consumption Mode (fixture) is the default for sandbox/CI. It downloads the latest bundle from Supabase (or local fixtures) and runs all pipelines deterministically offline.
This mirrors the long-term “Acquire Service” design but keeps the MVP lean.

Key Outcomes

Fresh Day‑1 artifacts (verified/tavily/confidence outputs) are produced daily without devs needing network access.
Artifacts are versioned, timestamped, and validated (checksums/signature, expiry guardrails).
CI and developers run against fixtures for determinism, while a scheduled capture job keeps data fresh.
Tasks (with risk mitigation/guardrails)
1. Mode & Source Abstractions
Implement FUND_SIGNAL_MODE=online|fixture (how clients behave) and FUND_SIGNAL_SOURCE=local|supabase (where fixtures come from).
Default sandbox/CI: fixture + local. Capture runner: online + supabase.
Extend pipelines to verify bundle checksums/signatures and enforce expiry thresholds (warn >48h, fail >7d, configurable).
2. Capture Tool & Manifest (Supabase Storage)
Build tools/capture_pipeline.py that runs Exa → You.com → Tavily → normalization in one pass.
Support --concurrency, --qps-{youcom,tavily}, retries/backoff with jitter, and --resume.
Write outputs to a temp Supabase prefix (artifacts/YYYY/MM/DD/<bundle_id>/…), then atomically promote by updating latest.json.
Manifest fields:
schema_version, bundle_id, captured_at (UTC), expiry_days, git_commit, tool_version, per-provider stats (requests, 429s, dedup_ratio).
checksum per file, optional sig (HMAC/GPG) for tamper detection.
Store raw vendor payloads as gzipped JSONL to reduce size; canonical leads/*.json kept alongside.
3. Automation (GitHub Actions Cron)
Create a nightly workflow running on a network-enabled runner:
Export API keys from GitHub Secrets / SSM; never store them in repo or sandbox.
Run capture tool; on success upload artifacts to Supabase and update latest.json.
If capture fails or captured_at exceeds expiry_days, open an alert (issue/Slack).
Runner security: isolate VM, restrict egress to Exa/You.com/Tavily, rotate keys quarterly.
4. Fixture Consumption & Sync
Add make sync-fixtures (or python tools/sync_fixtures.py) to download the latest Supabase bundle into ./fixtures/latest.
Pipelines read from fixtures by default, verifying captured_at against SLA before proceeding.
Keep small anonymized sample fixtures in Git for CI; real datasets stay in Supabase. Pipelines look in fixtures/ or leads/ depending on FUND_SIGNAL_SOURCE.
5. CI & Freshness Gates
verify-fixtures job: run the entire Day‑1 pipeline against committed sample fixtures to ensure determinism.
check-freshness job: fetch latest.json and fail if now - captured_at > expiry_days or signature missing.
online-contract-test (weekly): tiny sample run with FUND_SIGNAL_MODE=online to detect schema drift/API breakage early.
6. Documentation & Governance
Write docs/network_modes.md covering: capture workflow, runner location, how to refresh manually, Supabase bucket structure, key management, retention policy.
Document SLA (captured daily, warn 48h, fail 7d) and how captured_at/expiry_days surface in the product (freshness watermark).
Outline licensing/retention: e.g., raw vendor JSON retained 30 days, canonical data 90 days.
Why Supabase?

Provides private bucket + auth without managing S3; easy to integrate with existing stack.
We can still move to S3 later; the abstraction (FUND_SIGNAL_SOURCE) keeps the code portable.
Risks Addressed

Stale data: manifest timestamps + freshness checks + capture alerts.
Single-point capture: automated GitHub Actions runner replaces manual laptop runs.
Data integrity: checksums/signatures before consumption.
Secret leakage: keys only live in capture runner secrets; sandbox stays offline.
Storage bloat: real data in Supabase, only small samples in Git.
This adjusted plan keeps the original intent (fast but not throwaway) while layering in the high-value guardrails from the feedback.