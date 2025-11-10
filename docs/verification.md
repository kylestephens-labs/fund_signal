## Verification Pipeline Overview

The Day-1 pipeline now promotes a deterministic normalization pass ahead of You.com/Tavily verification:

1. **Exa capture** writes `raw/exa_seed.jsonl.gz`.
2. **`tools.normalize_exa_seed`** converts each noisy item into `{company_name, funding_stage, amount, announced_date, source_url}` tuples and records metrics + skip reasons.
3. The normalized file (`leads/exa_seed.normalized.json`) feeds You.com/Tavily so their regex lookups operate on consistent entities.

### Running the normalizer

```bash
export FUND_SIGNAL_MODE=fixture
export FUND_SIGNAL_SOURCE=local

python -m tools.normalize_exa_seed \
  --input artifacts/<bundle>/raw/exa_seed.jsonl.gz \
  --output artifacts/<bundle>/leads/exa_seed.normalized.json
```

The command accepts JSON arrays, JSON Lines, or `.jsonl.gz` files. The output includes:

```json
{
  "normalizer_version": "1.0.0",
  "items_total": 71,
  "items_parsed": 52,
  "items_skipped": 19,
  "coverage_by_field": {
    "company_name": 52,
    "funding_stage": 52,
    "amount": 52,
    "announced_date": 18
  },
  "data": [...],
  "skipped": [
    {"line_number": 14, "skip_reason": "MISSING_COMPANY", "raw_title": "Seed Round | The SaaS News"}
  ]
}
```

### When to re-run

- After each Exa capture for nightly bundles.
- Whenever regex rules change—rerun to regenerate canonical seeds before verification.

### Troubleshooting

| Issue | Mitigation |
| -- | -- |
| `MISSING_COMPANY` skips spike | Inspect `skipped` block to confirm article titles can be parsed; adjust regexes if needed. |
| Performance slower than target | Files >5k rows should still complete in <2s on an M2 Pro. Verify you are running from local SSD and not piping through slow network FS. |
| Determinism | Output is fully deterministic. If SHA changes for same input, ensure no timestamps/unstable ordering were added in downstream edits. |

### Programmatic use

Automation steps that need the normalized payload without touching disk can import `normalize_records` from `tools.normalize_exa_seed`. It accepts any iterable of raw Exa rows and returns the same payload structure emitted by the CLI entrypoint, which keeps downstream tests fast while sharing the identical validation logic.

## Deterministic confidence scoring (v2)

The second-stage scorer consumes the unified verification bundle from SCV-003 and emits lead-level points, labels, and observability metadata.

```
python -m pipelines.day1.confidence_scoring_v2 \
  --input artifacts/<bundle>/leads/unified_verify.json \
  --rules configs/verification_rules.v1.yaml \
  --out artifacts/<bundle>/leads/day1_scored.json
```

### Ruleset + heuristics

- `configs/verification_rules.v1.yaml` is the single source of truth for weights, thresholds, and the “mainstream domains” allowlist. Changing those values requires bumping the version.
- Each lead starts at zero points and gains:
  - `+2` for at least two unique domains from the mainstream list across all confirming articles.
  - `+1` when any confirming article exactly matches the normalized stage or amount.
  - `+1` when both You.com and Tavily each contribute at least one confirming article.
- Labels are derived from the versioned thresholds: `VERIFIED (>=3)`, `LIKELY (>=2)`, `EXCLUDE (<2)`.
- Missing normalized fields or confirmation sources never drop the item; instead, they add entries to `warnings[]` and the lead simply receives zero points for that rule.

### Output contract

`day1_scored.json` is deterministic (same bundle → same SHA) and includes:

```json
{
  "ruleset_version": "v1",
  "ruleset_sha256": "<sha of configs/verification_rules.v1.yaml>",
  "scored_at": "2025-11-07T03:00:00Z",
  "leads": [
    {
      "id": "lead_001",
      "company_name": "Acme AI",
      "confidence_points": 3,
      "final_label": "VERIFIED",
      "verified_by": ["Exa", "You.com", "Tavily"],
      "proof_links": [
        "https://techcrunch.com/...",
        "https://www.businesswire.com/..."
      ],
      "warnings": []
    }
  ]
}
```

`ruleset_version` can be temporarily overridden via `RULES_VERSION_OVERRIDE` for fixture tests, but the SHA always reflects the on-disk config.

### Tests

Use `pytest -k "test_confidence_scoring_v2 or test_rules_determinism or test_rules_backward_compat" -q` to exercise the heuristics plus determinism guarantees. The fixture inputs cover the FSQ-001 scenario: two mainstream confirmations with an exact amount match must score `>=3` and land in `VERIFIED`.
