SHELL := /usr/bin/env bash

DEV_COMPOSE := docker-compose.dev.yml
TEST_IMAGE := basic-memory-store:test
TEST_ENV := \
	-e MEMORY_API_KEY=testkey \
	-e PG_DSN=postgresql://test:test@127.0.0.1:1/test \
	-e QDRANT_URL=http://127.0.0.1:1 \
	-e LITELLM_BASE_URL=http://127.0.0.1:1 \
	-e LITELLM_API_KEY=testkey \
	-e CHAT_MODEL=test-chat \
	-e EMBED_MODEL=test-embed \
	-e OBJECT_STORE_ENABLED=false

.PHONY: test test-image test-postgres provenance-test replay-test lifecycle-smoke dev-python-check dev-up dev-down dev-reset dev-bootstrap dev-seed-profiles dev-logs dev-setup dev-test dev-install dev-start dev-start-reload dev-migrate-status dev-migrate-check dev-migrate-adopt

test: test-image
	@docker run --rm $(TEST_ENV) $(TEST_IMAGE) sh -lc \
		'test ! -e .env && python --version && python -c "import psycopg, pytest; print(\"pytest=\" + pytest.__version__); print(\"psycopg=\" + psycopg.__version__)" && ./tests/run_test_group.sh fake'

test-image:
	@docker build -f api/Dockerfile -t $(TEST_IMAGE) .

test-postgres: test-image
	@TEST_IMAGE=$(TEST_IMAGE) ./scripts/test_postgres_docker.sh

provenance-test: test-image
	@TEST_IMAGE=$(TEST_IMAGE) ./scripts/test_postgres_docker.sh tests/test_provenance_postgres_integration.py

replay-test: test-image
	@docker run --rm $(TEST_ENV) $(TEST_IMAGE) python -m tools.replay_scenarios

lifecycle-smoke: test-image
	@TEST_IMAGE=$(TEST_IMAGE) ./scripts/lifecycle_smoke.sh

dev-python-check:
	@./scripts/dev_python.sh check

dev-up: dev-python-check
	@docker compose -f $(DEV_COMPOSE) up -d
	@./scripts/dev_bootstrap.sh

dev-down:
	@docker compose -f $(DEV_COMPOSE) down

# Full reset: wipes containers (and any anonymous volumes), then boots clean.
dev-reset: dev-python-check
	@docker compose -f $(DEV_COMPOSE) down -v --remove-orphans
	@docker compose -f $(DEV_COMPOSE) up -d
	@./scripts/dev_bootstrap.sh

dev-bootstrap:
	@./scripts/dev_bootstrap.sh

dev-migrate-status:
	@cd api && BMS_DB_DIR="$$(cd .. && pwd)/db" ../scripts/dev_python.sh run -m tools.schema_migrations status

dev-migrate-check:
	@cd api && BMS_DB_DIR="$$(cd .. && pwd)/db" ../scripts/dev_python.sh run -m tools.schema_migrations check

dev-migrate-adopt:
	@cd api && BMS_DB_DIR="$$(cd .. && pwd)/db" ../scripts/dev_python.sh run -m tools.schema_migrations adopt-baseline

dev-seed-profiles:
	@./scripts/dev_seed_profiles.sh

dev-logs:
	@docker compose -f $(DEV_COMPOSE) logs -f --tail=200

dev-setup:
	@./scripts/dev_python.sh setup

dev-test:
	@cd api && ../scripts/dev_python.sh run -m pytest -q

dev-install: dev-setup

dev-start:
	@cd api && BMS_DB_DIR="$$(cd .. && pwd)/db" ../scripts/dev_python.sh run -m tools.schema_migrations check
	@cd api && ../scripts/dev_python.sh run -m uvicorn main:app --host 0.0.0.0 --port "$${APP_PORT:-4321}"

dev-start-reload:
	@cd api && BMS_DB_DIR="$$(cd .. && pwd)/db" ../scripts/dev_python.sh run -m tools.schema_migrations check
	@cd api && ../scripts/dev_python.sh run -m uvicorn main:app --host 0.0.0.0 --port "$${APP_PORT:-4321}" --reload
