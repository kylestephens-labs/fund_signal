.PHONY: help install dev test lint format docker-build docker-run docker-compose-up clean test-integration maybe-install setup setup-dev prove-quick prove-full

export UV_CACHE_DIR ?= $(abspath .uv-cache)
USE_UV ?= 1
SKIP_INSTALL ?= 1

ifeq ($(USE_UV),1)
PYTEST ?= uv run pytest
RUFF ?= uv run ruff
else
PYTEST ?= python -m pytest
RUFF ?= ruff
endif

PYTEST_FLAGS ?= -m "not slow and not contract"
PYTEST_FULL_FLAGS ?=

# Default target
help: ## Show this help message
	@echo "Available commands:"
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | sort | awk 'BEGIN {FS = ":.*?## "}; {printf "\033[36m%-20s\033[0m %s\n", $$1, $$2}'

install: ## Install base + dev dependencies
	@if [ "$(USE_UV)" = "1" ] && command -v uv >/dev/null 2>&1; then \
		echo "Installing requirements.txt via uv"; \
		UV_CACHE_DIR=$${UV_CACHE_DIR:-.uv-cache} uv pip install -r requirements.txt || pip install -r requirements.txt; \
		echo "Installing requirements-dev.txt via uv"; \
		UV_CACHE_DIR=$${UV_CACHE_DIR:-.uv-cache} uv pip install -r requirements-dev.txt || pip install -r requirements-dev.txt; \
	else \
		if [ "$(USE_UV)" != "1" ]; then \
			echo "USE_UV=0; skipping uv and using pip installs"; \
		else \
			echo "uv not found; falling back to pip installs"; \
		fi; \
		pip install -r requirements.txt; \
		pip install -r requirements-dev.txt; \
	fi

setup: ## Create a local virtualenv and install base dependencies (skips if .venv already exists)
	@if [ ! -d ".venv" ]; then \
		echo "Creating virtualenv via uv"; \
		uv venv; \
		created=1; \
	else \
		echo "Reusing existing virtualenv (.venv)"; \
		created=0; \
	fi; \
	if [ "$(SKIP_INSTALL)" = "1" ]; then \
		echo "Skipping dependency install (SKIP_INSTALL=1)"; \
	else \
		UV_CACHE_DIR=$${UV_CACHE_DIR:-.uv-cache} uv pip install -r requirements.txt || (.venv/bin/python -m ensurepip --upgrade && .venv/bin/python -m pip install -r requirements.txt); \
	fi

setup-dev: setup ## Install dev dependencies needed for lint + tests
	@if [ "$(SKIP_INSTALL)" = "1" ]; then \
		echo "Skipping dev dependency install (SKIP_INSTALL=1)"; \
	else \
		UV_CACHE_DIR=$${UV_CACHE_DIR:-.uv-cache} uv pip install -r requirements-dev.txt || (.venv/bin/python -m ensurepip --upgrade && .venv/bin/python -m pip install -r requirements-dev.txt); \
	fi

maybe-install:
	@if [ "$(SKIP_INSTALL)" = "1" ]; then \
		echo "Skipping dependency install (SKIP_INSTALL=1)"; \
	else \
		$(MAKE) --no-print-directory install; \
	fi

serve: ## Run API server via uv
	uv run uvicorn app.main:app --reload --host 0.0.0.0 --port 8000

dev: serve ## Back-compat alias for serve

migrate: maybe-install ## Apply Alembic migrations to the configured DATABASE_URL
	@if [ -z "$$DATABASE_URL" ]; then echo "DATABASE_URL is not set"; exit 1; fi
	UV_NO_SYNC=1 uv run alembic upgrade head

test: maybe-install ## Run tests with coverage using uv (ensures deps are installed unless SKIP_INSTALL=1)
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

online-contract-test: setup-dev ## Minimal live API contract test (requires provider keys)
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

email-demo-deliver: ## Render and send the Day-3 email digest via SMTP (--deliver flag)
	DELIVERY_SCORING_RUN=$${DELIVERY_SCORING_RUN:-demo-day3} uv run python -m pipelines.day3.email_delivery --output output/email_demo.md --deliver

email-cron: ## Cron-friendly Day-3 email delivery (Monday 9 AM PT)
	DELIVERY_SCORING_RUN=$${DELIVERY_SCORING_RUN:-demo-day3} uv run python -m pipelines.day3.email_schedule --output output/email_cron.md --deliver --company-limit 25 --min-score 80

email-cron-seed: ## Seed scoring run, then run the cron-friendly Day-3 email delivery (Monday 9 AM PT)
	$(MAKE) seed-scores
	$(MAKE) email-cron

slack-demo: ## Render the Day-3 Slack payload from persisted scores
	DELIVERY_SCORING_RUN=$${DELIVERY_SCORING_RUN:-demo-day3} uv run python -m pipelines.day3.slack_delivery --output output/slack_demo.json

# Legacy pip commands (for reference)
install-pip: ## Install dependencies with pip (legacy)
	pip install -r requirements.txt
	pip install -r requirements-dev.txt

dev-pip: ## Run development server with pip (legacy)
	uvicorn app.main:app --reload --host 0.0.0.0 --port 8000

# Fast feedback gate: lint + targeted tests
prove-quick: setup-dev
	UV_NO_SYNC=1 $(RUFF) format --check app tests
	UV_NO_SYNC=1 $(RUFF) check app tests
	UV_NO_SYNC=1 $(PYTEST) -q $(PYTEST_FLAGS)

# Full gate: mirrors CI bar; extend with typing/contracts as they land
prove-full: setup-dev
	UV_NO_SYNC=1 $(RUFF) format --check app tests
	UV_NO_SYNC=1 $(RUFF) check app tests
	UV_NO_SYNC=1 $(PYTEST) $(PYTEST_FULL_FLAGS)
