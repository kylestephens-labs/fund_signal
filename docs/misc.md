python -m tools.capture_pipeline \
  --input leads/exa_seed.json \
  --out artifacts/dry-run \
  --concurrency 1 \
  --qps-youcom 0.2 \
  --qps-tavily 1 \
  --expiry-days 7