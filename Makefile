SHELL := /usr/bin/env bash

DEV_COMPOSE := docker-compose.dev.yml
ARTIFACT_SMOKE_COMPOSE := docker-compose.artifact-smoke.yml
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

.PHONY: test test-image test-postgres wave4-memory-test wave4-episode-test artifact-storage-test artifact-storage-smoke provenance-test replay-test raw-retrieval-test raw-retrieval-smoke derivation-replay-test derivation-version-test lifecycle-smoke dev-python-check dev-up dev-down dev-reset dev-bootstrap dev-seed-profiles dev-logs dev-setup dev-test dev-install dev-start dev-start-reload dev-migrate-status dev-migrate-check dev-migrate-adopt

test: test-image
	@docker run --rm $(TEST_ENV) $(TEST_IMAGE) sh -lc \
		'test ! -e .env && python --version && python -c "import psycopg, pytest; print(\"pytest=\" + pytest.__version__); print(\"psycopg=\" + psycopg.__version__)" && ./tests/run_test_group.sh fake'

test-image:
	@docker build -f api/Dockerfile -t $(TEST_IMAGE) .

test-postgres: test-image
	@TEST_IMAGE=$(TEST_IMAGE) ./scripts/test_postgres_docker.sh

wave4-memory-test: test-image
	@docker run --rm $(TEST_ENV) $(TEST_IMAGE) python -m pytest -q \
		tests/test_memory_promotion_service.py \
		tests/test_memory_items_api.py \
		tests/test_recall_api.py \
		tests/test_recall_service.py

wave4-episode-test: test-image
	@docker run --rm $(TEST_ENV) $(TEST_IMAGE) python -m pytest -q \
		tests/test_episode_intelligence_service.py \
		tests/test_episodes_api.py \
		tests/test_episodes_service.py

artifact-storage-test: test-image
	@docker run --rm $(TEST_ENV) $(TEST_IMAGE) python -m pytest -q \
		tests/test_object_store.py \
		tests/test_qdrant_artifact_scope.py \
		tests/test_main_functional.py::test_artifact_flow_with_object_store_enabled \
		tests/test_main_functional.py::test_text_artifact_completion_derives_same_artifact_and_is_idempotent \
		tests/test_main_functional.py::test_text_artifact_retry_repairs_qdrant_failure_after_row_insert \
		tests/test_main_functional.py::test_text_artifact_active_publication_failure_first_write_retries_and_retrieves \
		tests/test_main_functional.py::test_text_artifact_active_publication_failure_mid_attempt_retries_without_duplicates \
		tests/test_main_functional.py::test_file_ingestion_active_publication_failure_does_not_expose_completed_artifact \
		tests/test_main_functional.py::test_text_artifact_postgres_activation_failure_after_qdrant_publication_retries \
		tests/test_main_functional.py::test_text_artifact_retry_repairs_partial_multi_chunk_and_retrieves \
		tests/test_main_functional.py::test_text_artifact_invalid_utf8_does_not_complete \
		tests/test_main_functional.py::test_oversized_text_artifact_completes_without_derivation \
		tests/test_main_functional.py::test_artifact_object_store_public_errors_are_bounded \
		tests/test_main_functional.py::test_artifact_complete_rejects_owner_mismatch \
		tests/test_main_functional.py::test_file_ingestion_creates_artifacts_and_chunks \
		tests/test_main_functional.py::test_v2_retrieval_returns_same_uploaded_artifact_source_metadata

artifact-storage-smoke:
	@set -e; \
	trap 'docker compose -f $(ARTIFACT_SMOKE_COMPOSE) down -v --remove-orphans >/dev/null 2>&1 || true' EXIT; \
	docker compose -f $(ARTIFACT_SMOKE_COMPOSE) up --build --abort-on-container-exit --exit-code-from smoke smoke

provenance-test: test-image
	@TEST_IMAGE=$(TEST_IMAGE) ./scripts/test_postgres_docker.sh tests/test_provenance_postgres_integration.py

replay-test: test-image
	@docker run --rm $(TEST_ENV) $(TEST_IMAGE) python -m tools.replay_scenarios

raw-retrieval-test: test-image
	@docker run --rm $(TEST_ENV) $(TEST_IMAGE) python -m pytest -q tests/test_retrieve_bundle_mvp.py tests/test_retrieval_replay.py

raw-retrieval-smoke: test-image
	@docker run --rm $(TEST_ENV) $(TEST_IMAGE) python -m tools.raw_retrieval_smoke

derivation-replay-test: test-image
	@docker run --rm $(TEST_ENV) $(TEST_IMAGE) python -m tools.derivation_replay_scenarios

derivation-version-test: test-image
	@docker run --rm $(TEST_ENV) $(TEST_IMAGE) python -m tools.derivation_version_scan

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
