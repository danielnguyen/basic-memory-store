#!/usr/bin/env bash
set -euo pipefail

# Seed minimal profile/default data for local dev.
#
# Usage:
#   ./scripts/dev_seed_profiles.sh
#   OWNER_ID=alice SURFACE=vscode CLIENT_ID=vscode PROFILE_NAME=dev ./scripts/dev_seed_profiles.sh

PG_CONTAINER="${PG_CONTAINER:-pg-test}"
PG_USER="${PG_USER:-memory_user}"
PG_DB="${PG_DB:-memory_db}"

OWNER_ID="${OWNER_ID:-daniel}"
SURFACE="${SURFACE:-vscode}"
CLIENT_ID="${CLIENT_ID:-vscode}"
PROFILE_NAME="${PROFILE_NAME:-dev}"
PROFILE_VERSION="${PROFILE_VERSION:-1}"

if ! docker exec "${PG_CONTAINER}" pg_isready -U "${PG_USER}" -d "${PG_DB}" >/dev/null 2>&1; then
  echo "Postgres container '${PG_CONTAINER}' is not ready. Start dev deps first (make dev-up)." >&2
  exit 1
fi

echo "==> Seeding profile '${PROFILE_NAME}' (v${PROFILE_VERSION}) for owner='${OWNER_ID}'"
echo "==> Surface defaults: surface='${SURFACE}', client_id='${CLIENT_ID}', global client_id=''"

docker exec -i "${PG_CONTAINER}" psql -U "${PG_USER}" -d "${PG_DB}" <<SQL
INSERT INTO profiles (
  owner_id,
  profile_name,
  profile_version,
  active,
  prompt_overlay,
  retrieval_policy_json,
  routing_policy_json,
  response_style_json,
  safety_policy_json,
  tool_policy_json
)
SELECT
  '${OWNER_ID}',
  '${PROFILE_NAME}',
  ${PROFILE_VERSION},
  true,
  'You are a pragmatic coding assistant. Prefer concrete, minimal-risk changes.',
  '{"scope":"conversation","k":8,"min_score":0.25}'::jsonb,
  '{"cost_mode":"balanced","latency_mode":"balanced","local_only":false}'::jsonb,
  '{"verbosity":"concise"}'::jsonb,
  '{"sensitivity_default":"private"}'::jsonb,
  '{"allow_patch_output":true}'::jsonb
WHERE NOT EXISTS (
  SELECT 1
  FROM profiles
  WHERE owner_id = '${OWNER_ID}'
    AND profile_name = '${PROFILE_NAME}'
    AND profile_version = ${PROFILE_VERSION}
);

INSERT INTO surface_profile_defaults (
  owner_id,
  surface,
  client_id,
  profile_name
)
VALUES ('${OWNER_ID}', '${SURFACE}', '${CLIENT_ID}', '${PROFILE_NAME}')
ON CONFLICT (owner_id, surface, client_id)
DO UPDATE SET
  profile_name = EXCLUDED.profile_name,
  updated_at = now();

INSERT INTO surface_profile_defaults (
  owner_id,
  surface,
  client_id,
  profile_name
)
VALUES ('${OWNER_ID}', '${SURFACE}', '', '${PROFILE_NAME}')
ON CONFLICT (owner_id, surface, client_id)
DO UPDATE SET
  profile_name = EXCLUDED.profile_name,
  updated_at = now();
SQL

echo "==> Seed complete"
