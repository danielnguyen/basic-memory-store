#!/usr/bin/env bash
set -euo pipefail

# Bootstrap Postgres schema for dev.
# Assumes dev containers are running (pg-test).
#
# Usage:
#   ./scripts/dev_bootstrap.sh
#
# Optional:
#   RUN_REINDEX=1 ./scripts/dev_bootstrap.sh

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON_RUN=("${ROOT_DIR}/scripts/dev_python.sh" run)

PG_CONTAINER="${PG_CONTAINER:-pg-test}"
PG_USER="${PG_USER:-memory_user}"
PG_DB="${PG_DB:-memory_db}"
PG_PASSWORD="${PG_PASSWORD:-pass}"
PG_HOST="${PG_HOST:-127.0.0.1}"
PG_PORT="${PG_PORT:-15432}"

echo "==> Waiting for Postgres (${PG_CONTAINER}) to be ready..."
for i in {1..60}; do
  if docker exec "${PG_CONTAINER}" pg_isready -U "${PG_USER}" -d "${PG_DB}" >/dev/null 2>&1; then
    break
  fi
  sleep 1
done

if ! docker exec "${PG_CONTAINER}" pg_isready -U "${PG_USER}" -d "${PG_DB}" >/dev/null 2>&1; then
  echo "Postgres not ready after waiting."
  exit 1
fi

echo "==> Running schema upgrade via migration runner"
(
  cd "${ROOT_DIR}/api"
  PG_DSN="postgresql://${PG_USER}:${PG_PASSWORD}@${PG_HOST}:${PG_PORT}/${PG_DB}" \
  BMS_DB_DIR="${ROOT_DIR}/db" \
  "${PYTHON_RUN[@]}" -m tools.schema_migrations upgrade
)

echo "==> Schema is current."

if [[ "${RUN_REINDEX:-0}" == "1" ]]; then
  echo "==> Running dev reindex (optional)..."
  (
    cd "${ROOT_DIR}/api"
    "${PYTHON_RUN[@]}" -m tools.reindex
  )
  echo "==> Reindex complete."
fi

echo "==> Dev bootstrap complete."
