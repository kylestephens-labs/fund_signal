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
