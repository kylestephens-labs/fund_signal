.PHONY: help install dev test lint format docker-build docker-run docker-compose-up clean test-integration

export UV_CACHE_DIR ?= $(abspath .uv-cache)
PYTEST ?= uv run pytest
RUFF ?= uv run ruff

# Default target
help: ## Show this help message
	@echo "Available commands:"
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | sort | awk 'BEGIN {FS = ":.*?## "}; {printf "\033[36m%-20s\033[0m %s\n", $$1, $$2}'

install: ## Install base + dev dependencies with uv
	uv pip install -r requirements.txt
	uv pip install -r requirements-dev.txt

serve: ## Run API server via uv
	uv run uvicorn app.main:app --reload --host 0.0.0.0 --port 8000

dev: serve ## Back-compat alias for serve

test: install ## Run tests with coverage using uv (ensures deps are installed)
	$(PYTEST) tests/ -v --cov=app --cov-report=html --cov-report=term

test-integration: ## Run integration tests (expects DATABASE_URL); skips cleanly if not set
	@if [ -z "$$DATABASE_URL" ]; then \
		echo "Skipping integration tests: DATABASE_URL is not set."; \
	else \
		$(PYTEST) tests/ -v -m integration --cov=app --cov-report=term; \
		rc=$$?; \
		if [ $$rc -eq 5 ]; then \
			echo "No integration tests collected; treating as success."; \
			exit 0; \
		else \
			exit $$rc; \
		fi; \
	fi

lint: ## Run linting with uv
	$(RUFF) check app/ tests/
	$(RUFF) format --check app/ tests/

format: ## Format code with uv
	$(RUFF) format app/ tests/

docker-build: ## Build Docker image
	docker build -t fastapi-template .

docker-run: ## Run Docker container locally
	docker run -p 8000:8000 --env-file .env fastapi-template

docker-compose-up: ## Start full stack with Docker Compose
	docker-compose up --build

docker-compose-down: ## Stop Docker Compose services
	docker-compose down

clean: ## Clean up temporary files
	find . -type f -name "*.pyc" -delete
	find . -type d -name "__pycache__" -delete
	rm -rf .pytest_cache/
	rm -rf htmlcov/
	rm -rf .coverage
	rm -rf .ruff_cache/

sync-fixtures: ## Download, verify, and install the latest fixture bundle
	uv run python -m tools.sync_fixtures

verify-fixtures: ## Run Day-1 pipelines against local fixtures
	FUND_SIGNAL_MODE=fixture FUND_SIGNAL_SOURCE=local $(PYTEST) tests/test_verify_fixtures.py -q

check-freshness: ## Enforce freshness/integrity gates for fixtures
	$(PYTEST) -k "freshness_gate or verify_bundle" -q

online-contract-test: ## Minimal live API contract test (requires provider keys)
	FUND_SIGNAL_MODE=online $(PYTEST) tests/test_online_contract.py -m contract -q

seed-scores: ## Seed deterministic scoring runs into Supabase/Postgres for delivery jobs
	uv run python scripts/seed_scores.py --fixture tests/fixtures/scoring/regression_companies.json --scoring-run $${DELIVERY_SCORING_RUN:-demo-day3} --seed-all --force

ui-smoke-seed: ## Seed the UI smoke persona (requires DATABASE_URL + UI_SMOKE env vars)
	@if [ -z "$$DATABASE_URL" ]; then \
		echo "DATABASE_URL must be set before running ui-smoke-seed."; \
		exit 1; \
	fi
	@if [ -z "$$UI_SMOKE_COMPANY_ID" ]; then \
		echo "UI_SMOKE_COMPANY_ID must be set before running ui-smoke-seed."; \
		exit 1; \
	fi
	uv run python scripts/seed_scores.py \
		--fixture tests/fixtures/scoring/regression_companies.json \
		--company-id $${UI_SMOKE_COMPANY_ID} \
		--scoring-run $${UI_SMOKE_SCORING_RUN_ID:-ui-smoke} \
		--force

email-demo: ## Render the Day-3 email digest from persisted scores
	DELIVERY_SCORING_RUN=$${DELIVERY_SCORING_RUN:-demo-day3} uv run python -m pipelines.day3.email_delivery --output output/email_demo.md

slack-demo: ## Render the Day-3 Slack payload from persisted scores
	DELIVERY_SCORING_RUN=$${DELIVERY_SCORING_RUN:-demo-day3} uv run python -m pipelines.day3.slack_delivery --output output/slack_demo.json

# Legacy pip commands (for reference)
install-pip: ## Install dependencies with pip (legacy)
	pip install -r requirements.txt
	pip install -r requirements-dev.txt

dev-pip: ## Run development server with pip (legacy)
	uvicorn app.main:app --reload --host 0.0.0.0 --port 8000

.PHONY: prove-quick prove-full

# Fast feedback gate: run the test suite
prove-quick:
	pytest -q

# Full gate: for now same as quick; later we can add linting, type checks, etc.
prove-full:
	pytest -q
