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
