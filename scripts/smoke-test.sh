#!/usr/bin/env bash
set -euo pipefail

BASE="${BASE:-http://127.0.0.1:4321}"
KEY="${MEMORY_API_KEY:-dev-local}"

hdr=(-H "X-API-Key: $KEY" -H "Content-Type: application/json")

echo "Health:"
curl -sS "${hdr[@]}" "$BASE/healthz" | jq .

echo "Create conversation:"
CID=$(curl -sS -X POST "$BASE/v1/conversations" \
  "${hdr[@]}" \
  -d '{"owner_id":"test_user","client_id":"smoke","title":"smoke test"}' | jq -r .conversation_id)
echo "CID=$CID"

echo "Chat:"
curl -sS -X POST "$BASE/v1/chat" \
  "${hdr[@]}" \
  -d "{\"owner_id\":\"test_user\",\"conversation_id\":\"$CID\",\"client_id\":\"smoke\",\"messages\":[{\"role\":\"user\",\"content\":\"Remember that my favorite snack is pretzels.\"}]}" | jq .

echo "Retrieve:"
curl -sS -X POST "$BASE/v1/retrieve" \
  "${hdr[@]}" \
  -d '{"owner_id":"test_user","query":"What is my favorite snack?","k":5}' | jq .
