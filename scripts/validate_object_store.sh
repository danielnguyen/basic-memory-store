#!/usr/bin/env bash
set -Eeuo pipefail

trap 'echo "❌ failed at line $LINENO" >&2' ERR

# ---- Config (override via env vars) ----
BASE="${BASE:-http://127.0.0.1:4321}"
KEY="${KEY:-${MEMORY_API_KEY:-dev-key}}"
CF_ACCESS_CLIENT_ID="${CF_ACCESS_CLIENT_ID:-}"
CF_ACCESS_CLIENT_SECRET="${CF_ACCESS_CLIENT_SECRET:-}"
OWNER_ID="${OWNER_ID:-daniel}"
MIME_TYPE="${MIME_TYPE:-text/plain}"
FILENAME="${FILENAME:-artifact test.txt}"
TMP_FILE="${TMP_FILE:-/tmp/artifact-test.txt}"

HDR=(-H "X-API-Key: $KEY" -H "Content-Type: application/json")
CF_HDR=()
if [[ -n "$CF_ACCESS_CLIENT_ID" && -n "$CF_ACCESS_CLIENT_SECRET" ]]; then
  CF_HDR=(-H "CF-Access-Client-Id: $CF_ACCESS_CLIENT_ID" -H "CF-Access-Client-Secret: $CF_ACCESS_CLIENT_SECRET")
  HDR+=("${CF_HDR[@]}")
fi

# ---- Helpers ----
die() { echo "❌ $*" >&2; exit 1; }
step() { echo; echo "== $* =="; }
need() { command -v "$1" >/dev/null 2>&1 || die "Missing required command: $1"; }

need curl
need jq
need wc
need tr
need head

echo "BASE=$BASE"
echo "OWNER_ID=$OWNER_ID"
echo "MIME_TYPE=$MIME_TYPE"

step "Create local test file"
echo "artifact validation $(date -u +%FT%TZ)" > "$TMP_FILE"
SIZE_BYTES="$(wc -c < "$TMP_FILE" | tr -d '[:space:]')"
[[ "$SIZE_BYTES" =~ ^[0-9]+$ ]] || die "Invalid SIZE_BYTES: $SIZE_BYTES"
echo "TMP_FILE=$TMP_FILE"
echo "SIZE_BYTES=$SIZE_BYTES"

step "Create upload intent"
INTENT="$(curl -sS -X POST "$BASE/v1/artifacts/init" \
  "${HDR[@]}" \
  -d "{
    \"owner_id\":\"$OWNER_ID\",
    \"filename\":\"$FILENAME\",
    \"mime\":\"$MIME_TYPE\",
    \"size\":$SIZE_BYTES
  }")"

echo "$INTENT" | jq . || die "upload-intent did not return valid JSON: $(echo "$INTENT" | head -c 300)"
ARTIFACT_ID="$(echo "$INTENT" | jq -r '.artifact_id')"
UPLOAD_URL="$(echo "$INTENT" | jq -r '.upload_url')"
[[ -n "$ARTIFACT_ID" && "$ARTIFACT_ID" != "null" ]] || die "artifact_id missing"
[[ -n "$UPLOAD_URL" && "$UPLOAD_URL" != "null" ]] || die "upload_url missing"
echo "ARTIFACT_ID=$ARTIFACT_ID"

step "Upload via presigned PUT"
PUT_OUT="$(mktemp)"
PUT_CODE="$(
  curl -sS -o "$PUT_OUT" -w "%{http_code}" -X PUT "$UPLOAD_URL" \
    "${CF_HDR[@]}" \
    -H "Content-Type: $MIME_TYPE" \
    --data-binary @"$TMP_FILE"
)"
echo "PUT status=$PUT_CODE"
if [[ ! "$PUT_CODE" =~ ^2 ]]; then
  echo "--- PUT response ---"
  cat "$PUT_OUT"
  echo
  rm -f "$PUT_OUT"
  die "Presigned PUT failed with HTTP $PUT_CODE"
fi
rm -f "$PUT_OUT"
echo "✅ PUT upload succeeded"

step "Complete upload"
COMPLETE="$(curl -sS -X POST "$BASE/v1/artifacts/complete" \
  "${HDR[@]}" \
  -d "{\"artifact_id\":\"$ARTIFACT_ID\",\"status\":\"completed\"}")"
echo "$COMPLETE" | jq . || die "complete did not return valid JSON: $(echo "$COMPLETE" | head -c 300)"
echo "✅ complete succeeded"

step "Get artifact metadata (includes download URL)"
DL="$(curl -sS "$BASE/v1/artifacts/$ARTIFACT_ID" "${HDR[@]}")"
echo "$DL" | jq . || die "download-url did not return valid JSON: $(echo "$DL" | head -c 300)"
DOWNLOAD_URL="$(echo "$DL" | jq -r '.download_url')"
[[ -n "$DOWNLOAD_URL" && "$DOWNLOAD_URL" != "null" ]] || die "download_url missing"

step "Smoke check presigned GET"
GET_OUT="$(mktemp)"
GET_CODE="$(curl -sS -o "$GET_OUT" -w "%{http_code}" "$DOWNLOAD_URL" "${CF_HDR[@]}")"
echo "GET status=$GET_CODE"
if [[ ! "$GET_CODE" =~ ^2 ]]; then
  echo "--- GET response ---"
  cat "$GET_OUT"
  echo
  rm -f "$GET_OUT"
  die "Presigned GET failed with HTTP $GET_CODE"
fi
echo "Downloaded bytes: $(wc -c < "$GET_OUT" | tr -d '[:space:]')"
head -c 120 "$GET_OUT" || true
echo
rm -f "$GET_OUT"
echo "✅ object-store flow validated"
