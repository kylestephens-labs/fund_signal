## Outstanding Items (Day 1 Tasks D1-01 → D1-03)

- **You.com API key acquisition is blocked.** Automatic verification cannot run until the platform allows key generation or support manually provisions one.
- **Rerun You.com verification once key exists.** Execute `python -m pipelines.day1.youcom_verify --input=leads/exa_seed.json --min_articles=2` to produce `leads/youcom_verified.json`.
- **Trigger Tavily confirmation after You.com completes.** With the You.com output in place, run `python -m pipelines.day1.tavily_confirm --input=leads/youcom_verified.json --min_confirmations=2` to generate proof links.
- **Spot-check JSON outputs.** Use `python -m tools.peek leads/exa_seed.json | head`, `... youcom_verified.json`, and `... tavily_confirmed.json` to validate record counts and fields.
- **Optional: log blockers.** Note in project tracker that D1-02/D1-03 are code-complete pending You.com API access, so downstream teams are aware of the dependency.
