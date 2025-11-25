## Outstanding Items (Day 1 Tasks D1-01 → D1-03)

- **You.com DNS issue:** Calls to `https://api.you.com/v1/search` fail with `nodename nor servname provided` even after exporting the API key. Need confirmation from You.com on the correct News endpoint or updated hostname.
- **Once the endpoint resolves:** Re-run `python -m pipelines.day1.youcom_verify --input=leads/exa_seed.json --min_articles=2` to regenerate `leads/youcom_verified.json` with populated `news_sources`/`press_articles`.
- **Unblock Tavily + confidence exports:** After a successful You.com pass, rerun `python -m pipelines.day1.tavily_confirm --input=leads/youcom_verified.json --min_confirmations=2` followed by `python -m pipelines.day1.confidence_scoring --input=leads/tavily_confirmed.json --output=leads/day1_output.json`.
- **Validate artifacts:** Inspect each stage with `python -m tools.peek …` (or `jq`) to ensure counts and freshness metadata look correct before handing off to Day 2.
- **Day 3 prep (FSQ‑019):** When we build the Slack/email/CSV delivery scripts, ensure they only emit rows with `final_label in {"VERIFIED","LIKELY"}` from `day1_scored.json`. Add that filter to the automation so EXCLUDE leads never reach customer-facing channels.

## Task FS-D2-01: Build ChatGPT scoring engine
Optimization Ideas

Extract the OpenAI client/payload parsing behind an interface so higher-level tests can swap in deterministic stubs without touching the main engine.
Introduce a persistence-backed ScoreRepository (e.g., SQLModel) to warm API caches across processes once the in-memory implementation is no longer sufficient.


## FS-D2-03: Scores Align With Intuition Regression Suite
Needs Improvement / Follow-ups

Online-mode scoring still lacks a corresponding regression guard; once prompt responses are stable we should add one alongside this fixture-based suite.
Regression fixture format is parsed defensively now, but automated hash/version enforcement would further ensure changes remain intentional.

Regression suite currently only covers the fixture rubric; add a similar guard once the online/prompted model is ready (maybe via recorded responses).

Personas cover only three clusters; consider adding edge cases (e.g., negative scores, neutral hiring) to better detect drift.

Fixture JSON lacks automated hash/versioning—documented regression_version but no enforcement that updates are intentional.

Optimization Ideas

Promote the new regression loader into a shared test utility so future regression suites (online mode, alternate models) can reuse it without re-importing from this file.

Consider parameterizing the regression test via pytest.mark.parametrize to surface per-persona failures earlier while keeping ranking assertions as a final aggregate check.

Introduce a helper factory (e.g., load_regression_personas() in a shared test util) so future regression suites (online mode, other scoring models) can reuse the same loader and validation logic without duplicating code.

## FS-D2-04: Scores Align With Intuition – Bundle Regression & Drift Detection

Needs Improvement / Follow-ups

Bundle fixtures remain static; consider version pinning or hashing to ensure updates are intentional before CI relies on them.

Regression helpers now live inside this test file; moving them into a shared tests/utils module would allow reuse as more suites depend on bundle personas.

Bundle fixtures are static; no automation to resync from real bundles or validate they match latest schema—future drift requires manual editing.

Tier boundaries are hardcoded (High ≥80, etc.); consider centralizing to settings or metadata to avoid test duplication when business rules shift.

Suite currently prints context via print; a structured logging helper might integrate better with CI output.

Optimization Ideas

Build a small factory on top of BundleRegressionFixture that returns parametrized pytest cases, so failures point directly at the offending persona/tier without scanning aggregated assertions.

Cache parsed fixtures across tests (module-level functools.lru_cache) to avoid repeated file I/O if additional regression tests are added later.

Extract shared fixture-loading/helpers into a dedicated test utility module so other scoring/drift tests (or future online-mode regressions) can reuse the parsing and context handling without touching the main regression test file.

## FSQ-032: Evidence Integrity Verificatio

Optimization Ideas:

Expose a Make target (or CLI alias) that bootstraps the QA job with fixture inputs and a local SQLite sink so developers can exercise the whole pipeline without relying on Supabase.

## Evidence Integrity Verification — Proof Timestamp Enforcement

Optimization Ideas:

Consider a lightweight test fixture that sets settings.proof_max_age_days explicitly, keeping unit tests insulated from future default changes.

Cache the computed “age days” in SignalProof.ensure_fresh so downstream logging/metrics can reuse it without recomputing in hydrators or future consumers.

## Task FSQ-034: Evidence Integrity Verification — Proof Domain Freshness Replay

Optimization Ideas:

Consider logging the dedupe key when a proof is skipped so ops can diagnose unexpected drops without re-running the full replay.

## Task FSQ-035A: ProofLinkHydrator Load Test Harness

Optimization Ideas:

Add a Make/CI target that runs tools.proof_links_load_test with sane defaults so ops folks don’t need to remember the CLI arguments.

## Task FSQ-035B: Proof Providers Outage Simulation Suite

Optimization Ideas:

The identical _log_retry_event helpers in the three Day-1 pipelines could move into a shared utility to keep future provider additions consistent and easier to tweak.

## Task FSQ-035C: ProofLink Cache Benchmark & Reporting

Optimization Ideas:

Teach tools/proof_links_benchmark.py (line 268) to optionally push the StatsD payload straight to Supabase or a metrics sink so FSQ-035D can wire alerts without additional wrappers.

## Task FSQ-035D: ProofLink Metrics & Alert Wiring

Optimization Ideas:

Share a reusable stub metrics fixture across tests (instead of redefining _StubMetrics), which would simplify future instrumentation tests and keep expectations centralized.



## Post-MVP ⚠️ Error/latency budgets

Hydrator caches proofs and raises alerts (see README.md:292-319), but there’s still no mention of Render/Supabase dashboards, no benchmarking or metrics confirming <300 ms P95, and no tests that simulate Exa/You/Tavily outages.

Prove metrics wiring end-to-end – Still need to run tools.proof_links_load_test/tools.proof_links_benchmark with METRICS_BACKEND=stdout and hit a real StatsD sink to confirm alert payloads and packet formatting. Post-MVP: docs/mvp.md focuses on shipping verified leads within 7 days; instrumentation polish can follow once core delivery works.

Add/verify optional StatsD / Supabase hooks in proof_links_load_test – Builder flagged this as pending after FSQ‑035D; without it, latency/error telemetry never reaches ops dashboards. Post-MVP: valuable for SLO tracking, but the MVP only promises functioning lead delivery, not full observability.

Replay job should fetch score exports from Supabase instead of assuming local files, eliminating manual prep steps. Post-MVP: MVP scope tolerates manual artifacts so long as leads ship (docs/mvp.md “What You’re Building”).

Guard against oversized proof_hash in (…) queries – Batch Supabase lookups so very large bundles don’t exceed URL limits. Post-MVP: MVP bundles are small (25‑75 leads/week), so the current approach suffices short term.

Stream/Chunk large fixture loads – Entire fixture buffering may spike memory for future-scale runs; add backpressure. Post-MVP: not required for the initial 7-day build where data volumes stay modest.

Wire outage simulation env vars into real CLIs/telemetry – Today they only affect fake clients; extend to actual tooling. Post-MVP: outage toggles are nice-to-have resilience levers beyond MVP commitments.

Provide a lighter “smoke” preset for the benchmark – Current suite takes ~3.6 s for 60 companies; allow reduced sample sizes automatically when laptops struggle. Post-MVP: developer convenience, not customer-facing functionality.
Speed up the Tavily slow-response test – Replace the ~1.2 s sleep with a stubbed timer so the suite scales better. Post-MVP: purely test ergonomics.


## Post-MVP ⚠️ Supabase replay automation

Replay job still assumes score exports are provided locally; wiring a Supabase fetcher (as noted in Builder’s follow-ups) would eliminate that manual step.

CLI flag to pull scores from Supabase: post-MVP (nice-to-have automation; MVP already satisfied by existing replay CLI using local data).

Supabase REST fetcher (load scores via API): post-MVP (automation convenience; not required to ship Day-2 promise).

Headers/auth handling for REST fetcher: post-MVP (only needed once the fetcher exists).

Pagination support for Supabase fetch: post-MVP (depends on the fetcher; not part of MVP scope).

README/docs updates describing Supabase replay mode: post-MVP (documentation change once fetcher is live).

Tests covering the REST fetcher/pagination: post-MVP (tied to the fetcher work itself).

The MVP only requires the replay job to exist and run (which current CLI + proof-domain monitor already cover); moving score-loading automation to Supabase is a follow-on enhancement.


## Post-MVP ⚠️ Monitoring + docs

README now explains the proof schema (README.md:292-319), but there’s still no mention of Render/Supabase dashboards or log wiring for proof metrics; no monitoring configuration or dashboard references were updated. Clarify whether the following deliverables can remain post-MVP:

- FSQ-037C: Monitoring runbook – add structured logging/metrics emitters in scoring + proof QA jobs (success/error counters, latency gauges) and expose them via Render-compatible sinks.
- FSQ-037B: Supabase dashboards & alerts – create/update Supabase dashboards/tables for proof/scoring metrics, configure alert policies (e.g., failure thresholds, stale proofs), and document how to view them.
- FSQ-037C: Monitoring runbook update – expand README/docs/dev_workflow with monitoring setup steps, expected metrics, alert escalation paths, and schema snapshot/versioning instructions (Pact or stored JSON).

## Post-MVP ⚠️ FSQ-CTA-001 | Add “Provide feedback” mailto CTA to Day 3 email

Optimization Ideas:

Consider adding a short note in README alongside email env vars describing the optional EMAIL_FEEDBACK_TO behavior for future clarity.
