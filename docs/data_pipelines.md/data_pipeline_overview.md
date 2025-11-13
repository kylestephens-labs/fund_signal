## Day 1 Data Pipeline Overview

This document outlines the end-to-end flow for assembling FundSignal’s verified, explainable prospect lists. Each stage is deterministic, versioned, and produces artifacts stored under `artifacts/YYYY/MM/DD/bundle-<timestamp>/…`.

### 1. Capture Bundle
- **What it is:** `tools.capture_pipeline` pulls fresh Exa seeds, You.com snippets, and Tavily articles into a date-stamped bundle (raw/fixtures/leads/manifest).  
- **Why we do it:** Create a reproducible snapshot of all raw inputs + provider telemetry so later stages can run offline and auditors can trace provenance.  
- **Goal:** Collect ≥50 candidate companies with enough metadata (title, source_url) to drive downstream extraction.  
- **Expected outcome:** `artifacts/.../bundle-*/` containing `raw/exa_seed.json`, `fixtures/*`, `leads/youcom_verified.json`, `leads/tavily_confirmed.json`, and a signed `manifest.json`.  
- **Success looks like:** Capture logs show all providers succeeded within QPS limits, manifest hashes verified, and `bundle_id` recorded for downstream steps.

### 2. Normalize + Resolve
- **What it is:** `tools.normalize_and_resolve` turns noisy Exa headlines into structured rows (company_name, funding_stage, amount, source_url) and scores candidates via `configs/resolver_rules.v1.1.yaml`.  
- **Why we do it:** Collapse multiple textual spans into a single canonical company name per article while preserving metadata for auditing/tie-breaking.  
- **Goal:** Emit `leads/exa_seed.normalized.json` with deterministic ordering, resolver scores, and resolver SHA.  
- **Expected outcome:** Each row has `company_name`, `resolution` info, and normalized amounts/dates ready for validation.  
- **Success looks like:** `pytest -k "normalize_and_resolve"` stays green; reruns produce byte-identical files; resolver logs reference the current ruleset SHA.

### 3. Feedback Resolver (FSQ‑008)
- **What it is:** `tools.verify_feedback_resolver` inspects low-confidence normalized rows (`final_label=EXCLUDE` or score<2) and cross-checks You.com/Tavily evidence for consistent entity spans across ≥2 domains.  
- **Why we do it:** Reduce false negatives caused by publisher-style titles or localization quirks without manual intervention.  
- **Goal:** Produce `leads/exa_seed.feedback_resolved.json` + optional manifest update with `feedback_version=v1` and `feedback_sha256`.  
- **Expected outcome:** Rows needing promotion gain `feedback_applied=true`, `feedback_reason`, `feedback_domains`, and a deterministic hash; telemetry logs the summary.  
- **Success looks like:** Determinism tests (`pytest -k feedback_determinism`) pass, manifest SHA updates when requested, and auditors can trace each promotion to specific domains.

### 4. Unified Verification
- **What it is:** `pipelines.day1.unified_verify` merges the normalized (or feedback-resolved) leads with You.com and Tavily confirmations, requesting fresh articles when running online.  
- **Why we do it:** Attach explainable “Verified by” evidence, timestamps, and domain counts so downstream scoring and users understand provenance.  
- **Goal:** Generate `leads/unified_verify.json` containing merged confirmation metadata, `verified_by`, and `proof_links`.  
- **Expected outcome:** Each lead records which providers confirmed it (or warnings when none did); logs track API failures/QPS usage.  
- **Success looks like:** At least 2 independent confirmations for true positives, rate-limited calls handled gracefully, and bundle telemetry summarizing hits per provider.

### 5. Confidence Scoring / Filtering
- **What it is:** `pipelines.day1.confidence_scoring_v2` applies versioned heuristics (`configs/verification_rules.v1.yaml`) to label leads as VERIFIED, LIKELY, or EXCLUDE, factoring in evidence strength, freshness, hiring signals, etc.  
- **Why we do it:** Provide a simple, explainable score per company so sales teams only see high-quality prospects.  
- **Goal:** Emit `leads/day1_scored.json` with `confidence_points`, `final_label`, `verified_by`, `proof_links`, and bundle-level metadata (`ruleset_version`, `scored_at`).  
- **Expected outcome:** ≥50 leads with scores; only VERIFIED/LIKELY forwarded to delivery; warnings highlight missing data.  
- **Success looks like:** Scoring logs report counts per label (e.g., VERIFIED=2, LIKELY=4, EXCLUDE=70); reruns remain deterministic; downstream Slack/email scripts filter on `final_label in {"VERIFIED","LIKELY"}` before sending.
