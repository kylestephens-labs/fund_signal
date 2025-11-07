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
