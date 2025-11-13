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

Optimization Ideas

Promote the new regression loader into a shared test utility so future regression suites (online mode, alternate models) can reuse it without re-importing from this file.

Consider parameterizing the regression test via pytest.mark.parametrize to surface per-persona failures earlier while keeping ranking assertions as a final aggregate check.