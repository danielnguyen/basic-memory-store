#!/bin/sh
set -eu

case "${1:-}" in
  fake)
    exec python -m pytest -q \
      tests/test_object_store.py \
      tests/test_main_functional.py \
      tests/test_profiles_resolve.py \
      tests/test_request_id_contract.py \
      tests/test_retrieve_bundle_mvp.py \
      tests/test_events_ingest_api.py \
      tests/test_hygiene_api.py \
      tests/test_initiative_api.py \
      tests/test_proactive_api.py \
      tests/test_derived_contract.py \
      tests/test_derivation_lifecycle.py \
      tests/test_memory_items_service.py \
      tests/test_memory_lifecycle_service.py \
      tests/test_memory_items_api.py \
      tests/test_recall_api.py \
      tests/test_episodes_service.py \
      tests/test_episodes_api.py \
      tests/test_claim_records_api.py \
      tests/test_traces_api.py \
      tests/test_retrieval_replay.py \
      tests/test_reindex_policy_metadata.py
    ;;
  postgres)
    exec python -m pytest -q \
      tests/test_memory_items_migration.py \
      tests/test_episodes_migration.py \
      tests/test_recall_migration.py \
      tests/test_schema_migrations_integration.py \
      tests/test_claim_records_postgres.py \
      tests/test_memory_lifecycle_postgres.py \
      tests/test_provenance_postgres_integration.py \
      tests/test_trace_postgres_integration.py
    ;;
  *)
    echo "Usage: $0 {fake|postgres}" >&2
    exit 2
    ;;
esac
