SHELL := /usr/bin/env bash

DEV_COMPOSE := docker-compose.dev.yml

.PHONY: dev-up dev-down dev-reset dev-bootstrap dev-logs

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

dev-logs:
	@docker compose -f $(DEV_COMPOSE) logs -f --tail=200
