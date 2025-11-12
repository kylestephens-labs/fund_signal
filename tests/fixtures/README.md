# Test Fixtures

This directory hosts deterministic bundles that mirror the on-disk layout emitted by `tools.normalize_and_resolve`. Each bundle lives under `tests/fixtures/bundles/<name>` and always ships with:

- `leads/` artifacts (candidate/normalized JSON plus synthetic verification evidence when needed),
- a `manifest.json` describing capture metadata + SHA256 digests, and
- a bundle-specific `README.md` that documents intent, edge cases, and when the bundle should be used.

Current bundles:

- `sample/bundle-sample` – end-to-end sanity bundle referenced by resolver + normalize tests.
- `feedback_case` – low-confidence resolver scenario used by forthcoming feedback tests. See the nested README for scenario details and file-by-file guidance.

When adding or updating bundles:

1. Re-run the pipeline that generated the data (usually `python -m tools.normalize_and_resolve ...`) so the JSON matches production schema.
2. Recompute manifest hashes (`python -m tools.manifest_utils ...`) and keep `captured_at` timestamps deterministic.
3. Document the purpose plus any intentional anomalies inside the bundle README.
4. Validate every JSON payload with `python3 -m json.tool` before committing.
