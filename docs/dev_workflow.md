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
