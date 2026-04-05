SHELL := /usr/bin/env bash

DEV_COMPOSE := docker-compose.dev.yml

.PHONY: dev-up dev-down dev-reset dev-bootstrap dev-seed-profiles dev-logs dev-test dev-install dev-start dev-start-reload

dev-up:
	@docker compose -f $(DEV_COMPOSE) up -d
	@./scripts/dev_bootstrap.sh

dev-down:
	@docker compose -f $(DEV_COMPOSE) down

# Full reset: wipes containers (and any anonymous volumes), then boots clean.
dev-reset:
	@docker compose -f $(DEV_COMPOSE) down -v --remove-orphans
	@docker compose -f $(DEV_COMPOSE) up -d
	@./scripts/dev_bootstrap.sh

dev-bootstrap:
	@./scripts/dev_bootstrap.sh

dev-seed-profiles:
	@./scripts/dev_seed_profiles.sh

dev-logs:
	@docker compose -f $(DEV_COMPOSE) logs -f --tail=200

dev-test:
	@cd api && ./.venv/bin/python -m pytest -q

dev-install:
	@cd api && ./.venv/bin/python -m pip install -r requirements.txt

dev-start:
	@cd api && ./.venv/bin/uvicorn main:app --host 0.0.0.0 --port "$${APP_PORT:-4321}"

dev-start-reload:
	@cd api && ./.venv/bin/uvicorn main:app --host 0.0.0.0 --port "$${APP_PORT:-4321}" --reload
