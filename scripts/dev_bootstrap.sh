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
SCHEMA="${ROOT_DIR}/db/schema.sql"

PG_CONTAINER="${PG_CONTAINER:-pg-test}"
PG_USER="${PG_USER:-memory_user}"
PG_DB="${PG_DB:-memory_db}"

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

echo "==> Applying schema: ${SCHEMA}"
docker exec -i "${PG_CONTAINER}" psql -U "${PG_USER}" -d "${PG_DB}" < "${SCHEMA}"

echo "==> Schema applied."

if [[ "${RUN_REINDEX:-0}" == "1" ]]; then
  echo "==> Running dev reindex (optional)..."
  python -m tools.reindex
  echo "==> Reindex complete."
fi

echo "==> Dev bootstrap complete."
