python -m tools.capture_pipeline \
  --input leads/exa_seed.json \
  --out artifacts/dry-run \
  --concurrency 1 \
  --qps-youcom 0.2 \
  --qps-tavily 1 \
  --expiry-days 7


  source .venv/bin/activate – run this once per terminal session to enter the virtualenv. As long as your prompt shows (fund_signal) you can skip it

  /Users/kylestephens/repo/fund_signal_vscode/.venv/bin/activate

  Restarting the api
uvicorn app.main:app --reload – run this every time you want to start (or restart) the API. Stop with Ctrl+C and rerun whenever you need a fresh server.
