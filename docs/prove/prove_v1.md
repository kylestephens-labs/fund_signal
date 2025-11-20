# Prove v1 Plan — Backend Quality Gates

## Goal Recap
- Provide two deterministic commands (`make prove-quick`, `make prove-full`) that answer “Is this backend ready to ship/merge?”
- Become the shared contract for Builder, Refactorer, future Prove CLI integration, and CI/CD. No green prove, no merge.

## Key Outcomes
- **Consistency**: All agents run the same gates, under the same virtualenv/dependency setup, before handoff.
- **Speed + Depth**: `prove-quick` stays fast enough for inner loop (<1 min) while covering lint + targeted tests; `prove-full` mirrors CI’s entire bar.
- **Drop-in Prove CLI**: Targets already structured so MCP/Prove can hook in without rewriting prompts.

## Proposed Implementation Improvements

### 1. Makefile Targets

```make
.PHONY: setup setup-dev prove-quick prove-full

PYTEST_FLAGS ?=
PYTEST_FULL_FLAGS ?=

setup:
	uv venv
	uv pip install -r requirements.txt

setup-dev: setup
	uv pip install -r requirements-dev.txt

prove-quick: setup-dev
	uv run ruff format --check
	uv run ruff check
	uv run pytest -q $(PYTEST_FLAGS)

prove-full: setup-dev
	uv run ruff format --check
	uv run ruff check
	uv run pytest $(PYTEST_FULL_FLAGS)
	# Optional as soon as repo adopts typing/contracts:
	# uv run mypy .
	# uv run python scripts/verify-fixtures.py
	# uv run python scripts/check-freshness.py
	# prove --config prove/prove.config.toml
```

✅ **Status:** These targets now live in the root `Makefile`. Run `make setup-dev` once to create the `uv` virtualenv, `make prove-quick` before every handoff, and `make prove-full` for CI / merge checks. Adjust `PYTEST_FLAGS` or `PYTEST_FULL_FLAGS` as needed (e.g., `PYTEST_FLAGS='-m "not slow"' make prove-quick`).

**Notes**
- `setup-dev` installs lint/type deps so both gates succeed locally and in CI.
- `PYTEST_FLAGS` keep quick vs full suites configurable (by default `-m "not slow and not contract"` to skip heavy pipelines/benchmarks plus online contract checks).
- Ruff format + lint run in both gates so formatting/lint drift can’t land.
- Hooks for fixture/contract scripts documented to avoid losing existing CI coverage.

### 2. Prove Config Skeleton

`prove/prove.config.example.toml`
```toml
[project]
name = "fund_signal_backend"
language = "python"

[paths]
src = "app"
tests = "tests"

[gates.quick]
commands = ["make prove-quick"]

[gates.full]
commands = ["make prove-full"]
```

Add README snippet telling engineers to copy this to `prove/prove.config.toml` when Prove CLI lands.

### 3. Role / Prompt Updates
- Task Writer template commands: list `make prove-quick` under Builder requirements.
- Builder role: “Run `make prove-quick` before handoff; include command + result in summary.”
- Refactorer role: rerun `make prove-quick`; report same command output.
- Later, add “Prove” agent instructions that call `make prove-full` for merge gate.

### 4. CI Alignment
- Update GitHub workflows to call `make prove-quick` on PRs and `make prove-full` on main.
- Remove bespoke pytest/ruff invocations so the Make targets stay the single source of truth.

### 5. Future Enhancements
- Add `uv run mypy` and contract validation once typing/specs stabilize.
- Introduce labels/markers (`@slow`, `@integration`) so `PYTEST_FLAGS` cleanly toggle suites.
- Capture artifacts (coverage XML, Playwright screenshots) from `prove-full` for CI dashboards.

## Next Steps
1. Land Makefile + config skeleton changes.
2. Update role files/task template to reference `make prove-quick`.
3. Wire CI to these targets.
4. Iterate with Prove CLI once available.

This keeps the quality bar explicit, reproducible, and ready for automation.

## Tasks

### Task A: Wire Makefile prove targets
Implement the `setup-dev`, `prove-quick`, and `prove-full` targets exactly as outlined above so every contributor and CI job can run the same quality bar. Ensure Ruff + pytest + optional flags work locally and document how to extend them with additional checks later. 📚 ESSENTIAL CONTEXT (read before drafting the task): 
  • docs/prove/prove_v1.md
  • Makefile
  • README.md

### Task B: Add Prove config skeleton
Create `prove/prove.config.example.toml` plus a brief README snippet describing how Prove CLI will consume these commands. The goal is to give future automation a drop-in config without changing today’s workflow. 📚 ESSENTIAL CONTEXT:
  • docs/prove/prove_v1.md
  • prove/prove.config.example.toml (new)
  • docs/prove/prove_overview.md

### Task C: Update Codex roles + Task Template
Amend Task Writer, Builder, and Refactorer instructions so they all cite `make prove-quick` as the required quick-test command, and clarify that `make prove-full` runs in CI/post-merge (optional locally before merging). This keeps the human/agent instructions aligned with the new gates without slowing development. 📚 ESSENTIAL CONTEXT:
  • .project/codex/roles/task_writer.md
  • .project/codex/roles/builder.md
  • .project/codex/roles/refactorer.md
  • docs/prove/prove_v1.md

