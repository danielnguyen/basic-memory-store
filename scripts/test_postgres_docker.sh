#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TEST_IMAGE="${TEST_IMAGE:-basic-memory-store:test}"
RUN_ID="bms-test-$RANDOM-$$"
NETWORK_NAME="${RUN_ID}-network"
POSTGRES_NAME="${RUN_ID}-postgres"
TEST_CONTAINER_NAME="${RUN_ID}-api"

cleanup() {
  docker rm -f "${TEST_CONTAINER_NAME}" >/dev/null 2>&1 || true
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

if ! docker exec "${POSTGRES_NAME}" pg_isready -U postgres -d postgres >/dev/null 2>&1; then
  echo "Disposable PostgreSQL 16 did not become ready." >&2
  exit 1
fi

docker run -d --rm \
  --name "${TEST_CONTAINER_NAME}" \
  --network "${NETWORK_NAME}" \
  -e TEST_PG_DSN="postgresql://postgres:postgres@${POSTGRES_NAME}:5432/postgres" \
  "${TEST_IMAGE}" \
  sleep infinity >/dev/null

docker cp "${ROOT_DIR}/docker-compose.yml" "${TEST_CONTAINER_NAME}:/app/docker-compose.yml"
if [[ "$#" -gt 0 ]]; then
  docker exec "${TEST_CONTAINER_NAME}" python -m pytest -q "$@"
else
  docker exec "${TEST_CONTAINER_NAME}" ./tests/run_test_group.sh postgres
fi
