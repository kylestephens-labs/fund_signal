# Developer Workflow Guardrails

To keep local environments healthy and reproducible, follow the repo’s baked-in guardrails:

## Python Interpreter

- VS Code automatically targets `${workspaceFolder}/.venv/bin/python` via `.vscode/settings.json`.
- If the interpreter warning ever reappears, open the Command Palette → “Python: Select Interpreter” and choose the same path.

## Make Targets

- `make install` – Installs runtime + dev dependencies using `uv pip` (with `UV_CACHE_DIR` exported by the Makefile). Run this after cloning or whenever requirements change.
- `make test` – Executes the full pytest suite through `uv run`, ensuring the `.venv` is used consistently.
- `make serve` (alias `make dev`) – Starts the FastAPI server via `uv run uvicorn …`, so the same interpreter/venv powers the API.

Because these commands wrap `uv`, you never have to remember to set `UV_CACHE_DIR` or avoid raw `pip`. Stick to the Make targets to prevent environment drift and keep CI parity.

## UI Drawer Smoke Test

Day 2 promises every score explains itself, so keep the Playwright drawer test green:

1. Start the API in fixture mode: `FUND_SIGNAL_MODE=fixture SCORING_MODEL=fixture-rubric uvicorn app.main:app --reload --port 8000`.
2. Launch the UI locally (or point `UI_BASE_URL` at your deployed preview). The test expects the drawer to surface the `High Growth SaaS` fixture company by default; override via the `UI_SMOKE_*` env vars when needed.
3. Install UI test deps once: `cd frontend && npm install && npx playwright install --with-deps`.
4. Run the smoke: `cd frontend && UI_BASE_URL=http://localhost:3000 API_BASE_URL=http://localhost:8000 npm run test:ui`.

Artifacts (videos, traces, screenshots) land in `frontend/artifacts/`; ship them to CI storage so Render/Supabase dashboards can link to the latest drawer validation run.

If you need to debug without the forced “missing proof” mutation, set `UI_SMOKE_DISABLE_PROOF_PATCH=true` before launching Playwright so the test only verifies the happy-path drawer.
