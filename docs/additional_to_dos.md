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