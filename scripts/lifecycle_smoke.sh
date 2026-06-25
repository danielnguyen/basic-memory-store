#!/usr/bin/env bash
set -euo pipefail

TEST_IMAGE="${TEST_IMAGE:-basic-memory-store:test}"
RUN_ID="bms-lifecycle-smoke-$RANDOM-$$"
NETWORK_NAME="${RUN_ID}-network"
POSTGRES_NAME="${RUN_ID}-postgres"

cleanup() {
  docker rm -f "${POSTGRES_NAME}" >/dev/null 2>&1 || true
  docker network rm "${NETWORK_NAME}" >/dev/null 2>&1 || true
}
trap cleanup EXIT

docker network create "${NETWORK_NAME}" >/dev/null
docker run -d --rm \
  --name "${POSTGRES_NAME}" \
  --network "${NETWORK_NAME}" \
  -e POSTGRES_DB=postgres \
  -e POSTGRES_USER=postgres \
  -e POSTGRES_PASSWORD=postgres \
  postgres:16 >/dev/null

for _ in {1..60}; do
  if docker exec "${POSTGRES_NAME}" pg_isready -U postgres -d postgres >/dev/null 2>&1; then
    break
  fi
  sleep 1
done

docker run --rm \
  --network "${NETWORK_NAME}" \
  "${TEST_IMAGE}" \
  python -m tools.lifecycle_smoke \
    --dsn "postgresql://postgres:postgres@${POSTGRES_NAME}:5432/postgres" \
    --db-dir /app/db
