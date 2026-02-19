SHELL := /usr/bin/env bash

DEV_COMPOSE := docker-compose.dev.yml

.PHONY: dev-up dev-down dev-reset dev-bootstrap dev-logs dev-test dev-install dev-start dev-start-reload

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

dev-test:
	@cd api && ./.venv/bin/python -m pytest -q

dev-install:
	@cd api && ./.venv/bin/python -m pip install -r requirements.txt

dev-start:
	@cd api && \
	MEMORY_API_KEY="$${MEMORY_API_KEY:-dev-key}" \
	PG_DSN="$${PG_DSN:-postgresql://memory_user:pass@127.0.0.1:15432/memory_db}" \
	QDRANT_URL="$${QDRANT_URL:-http://127.0.0.1:16333}" \
	LITELLM_BASE_URL="$${LITELLM_BASE_URL:-http://127.0.0.1:4000}" \
	OBJECT_STORE_ENABLED="$${OBJECT_STORE_ENABLED:-true}" \
	OBJECT_STORE_ENDPOINT="$${OBJECT_STORE_ENDPOINT:-http://127.0.0.1:16335}" \
	OBJECT_STORE_BUCKET="$${OBJECT_STORE_BUCKET:-memory-artifacts}" \
	OBJECT_STORE_ACCESS_KEY="$${OBJECT_STORE_ACCESS_KEY:-minioadmin}" \
	OBJECT_STORE_SECRET_KEY="$${OBJECT_STORE_SECRET_KEY:-minioadmin}" \
	./.venv/bin/uvicorn main:app --host 0.0.0.0 --port "$${APP_PORT:-4321}"

dev-start-reload:
	@cd api && \
	MEMORY_API_KEY="$${MEMORY_API_KEY:-dev-key}" \
	PG_DSN="$${PG_DSN:-postgresql://memory_user:pass@127.0.0.1:15432/memory_db}" \
	QDRANT_URL="$${QDRANT_URL:-http://127.0.0.1:16333}" \
	LITELLM_BASE_URL="$${LITELLM_BASE_URL:-http://127.0.0.1:4000}" \
	OBJECT_STORE_ENABLED="$${OBJECT_STORE_ENABLED:-true}" \
	OBJECT_STORE_ENDPOINT="$${OBJECT_STORE_ENDPOINT:-http://127.0.0.1:16335}" \
	OBJECT_STORE_BUCKET="$${OBJECT_STORE_BUCKET:-memory-artifacts}" \
	OBJECT_STORE_ACCESS_KEY="$${OBJECT_STORE_ACCESS_KEY:-minioadmin}" \
	OBJECT_STORE_SECRET_KEY="$${OBJECT_STORE_SECRET_KEY:-minioadmin}" \
	./.venv/bin/uvicorn main:app --host 0.0.0.0 --port "$${APP_PORT:-4321}" --reload
