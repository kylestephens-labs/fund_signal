## Artifact Storage Layout

```
artifacts/
  YYYY/
    MM/
      DD/
        bundle-<timestamp>/
          manifest.json
          raw/
            exa_seed.json
            youcom.jsonl
            tavily.jsonl
          fixtures/
            youcom/articles.json
            tavily/articles.json
          leads/
            youcom_verified.json
            tavily_confirmed.json
```

- Each capture run writes to a unique bundle directory.
- Bundles are immutable. Never overwrite files in-place.
- After a bundle is fully uploaded, update `latest.json` at the bucket root to reference the new prefix. Consumers read this pointer to locate the newest bundle atomically.

### Manifest Schema

`manifest.json` includes:

| Field | Description |
|-------|-------------|
| `schema_version` | Integer schema identifier |
| `bundle_id` | Directory name (e.g., `bundle-20251106T000000Z`) |
| `captured_at` | UTC ISO timestamp |
| `expiry_days` | Validity window for freshness checks |
| `git_commit` | Commit SHA used during capture (if available) |
| `tool_version` | Capture tool version string |
| `providers` | Array of per-provider stats (`requests`, `successes`, `rate_limits`, `errors`, `dedup_ratio`) |
| `files` | Array of `{path, size, checksum}` for every file except `manifest.json` |
| `signature` | Optional HMAC-SHA256 of the manifest payload |

Use `python -m tools.verify_bundle --manifest <bundle>/manifest.json` to validate freshness, checksums, and signature before consumption.

### Resolver ruleset reference

Keep a running table of the active resolver config so bundle consumers can confirm what version generated their leads:

| Version | SHA256 | Rolled out | Notes |
| --- | --- | --- | --- |
| v1.1 | `490356d1cfa7ecd84cf13a197768a1c5012d41c878f19ebab7aad6c9ad8bdd4a` | 2025‑11‑11 | Adds publisher prefix/verb penalties, slug-head proximity bonus, locale verb hits, possessive repair boost, and per-candidate feature flags. |
| v1 | `4759d2bf03ce5b3991cc77444430d455084cbd51911543438639123fe4fd9029` | 2025‑08‑14 | Baseline resolver launch (publisher token penalty, slug edit distance ≤2). |

Each bundle’s `leads/exa_seed.normalized.json` and resolver telemetry payloads embed `resolver_ruleset_version` + `resolver_ruleset_sha256`; auditors should compare those values against this table to ensure the captured data matches the documented configuration.
