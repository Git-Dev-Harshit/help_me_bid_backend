# ---------------------------------------------------------------------------
# Convenience wrappers. The application runs in Docker; PostgreSQL runs on the
# host machine. If you do not have `make`, run the underlying `docker compose`
# commands shown in each recipe directly.
# ---------------------------------------------------------------------------
.DEFAULT_GOAL := help
COMPOSE := docker compose
COMPOSE_TEST := $(COMPOSE) -f docker-compose.yml -f docker-compose.test.yml

# psql is not installed in the application image, so database shells run in a
# throwaway postgres client container pointed at the host server.
PSQL_URL := postgresql://$${POSTGRES_USER:-postgres}:$${POSTGRES_PASSWORD:-postgres}@host.docker.internal:5432
PSQL_RUN := docker run --rm -it --add-host=host.docker.internal:host-gateway postgres:16-alpine psql

.PHONY: help
help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-18s\033[0m %s\n", $$1, $$2}'

# --- Lifecycle -------------------------------------------------------------
.PHONY: up
up: ## Build and start the stack (migrations, api, worker) against the host DB
	$(COMPOSE) up --build -d
	@echo "API:  http://localhost:8000"
	@echo "Docs: http://localhost:8000/docs"

.PHONY: down
down: ## Stop all services (host database is left untouched)
	$(COMPOSE) down

.PHONY: clean
clean: ## Stop all services and DROP the ipo_tracker database on the host
	$(COMPOSE) down
	$(PSQL_RUN) "$(PSQL_URL)/postgres" -c 'DROP DATABASE IF EXISTS ipo_tracker'

.PHONY: logs
logs: ## Tail logs from every service
	$(COMPOSE) logs -f

.PHONY: logs-worker
logs-worker: ## Tail scraper/notification worker logs
	$(COMPOSE) logs -f worker

.PHONY: ps
ps: ## Show service status
	$(COMPOSE) ps

# --- Database --------------------------------------------------------------
.PHONY: migrate
migrate: ## Create the database if needed and apply migrations
	$(COMPOSE) run --rm migrate

.PHONY: migration
migration: ## Autogenerate a migration: make migration m="add xyz"
	@test -n "$(m)" || (echo 'usage: make migration m="description"' && exit 1)
	$(COMPOSE) run --rm -v "$(CURDIR)/alembic/versions:/app/alembic/versions" \
		migrate alembic revision --autogenerate -m "$(m)"

.PHONY: psql
psql: ## Open a psql shell on the host database
	$(PSQL_RUN) "$(PSQL_URL)/ipo_tracker"

# --- Jobs ------------------------------------------------------------------
.PHONY: scrape
scrape: ## Trigger one scrape immediately
	$(COMPOSE) exec worker python -m app.workers.run_once scrape

.PHONY: notify
notify: ## Trigger one notification evaluation immediately
	$(COMPOSE) exec worker python -m app.workers.run_once notify

.PHONY: schedule
schedule: ## Show the next scheduled run for every job
	$(COMPOSE) exec worker python -m app.workers.run_once schedule

# --- Tests / quality -------------------------------------------------------
.PHONY: test
test: ## Run the full test suite
	$(COMPOSE_TEST) run --rm --build test

.PHONY: test-unit
test-unit: ## Run unit tests only (no database required)
	$(COMPOSE_TEST) run --rm --build test pytest tests/unit -p no:cacheprovider

.PHONY: test-integration
test-integration: ## Run integration tests only
	$(COMPOSE_TEST) run --rm --build test pytest tests/integration -p no:cacheprovider

.PHONY: coverage
coverage: ## Run the suite with a coverage report
	$(COMPOSE_TEST) run --rm --build test \
		pytest --cov=app --cov-report=term-missing -p no:cacheprovider

.PHONY: lint
lint: ## Run ruff and mypy
	$(COMPOSE_TEST) run --rm --build test ruff check --no-cache app tests scripts
	$(COMPOSE_TEST) run --rm test mypy app

.PHONY: format
format: ## Auto-format the codebase in place
	$(COMPOSE_TEST) run --rm \
		-v "$(CURDIR)/app:/app/app" -v "$(CURDIR)/tests:/app/tests" \
		test ruff check --no-cache --fix app tests

.PHONY: fixtures
fixtures: ## Regenerate the HTML scraper fixtures
	$(COMPOSE_TEST) run --rm -v "$(CURDIR)/tests:/app/tests" \
		test python scripts/generate_html_fixtures.py
