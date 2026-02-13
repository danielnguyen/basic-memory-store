#!/usr/bin/env bash
set -euo pipefail

# ---- Config (override via env vars) ----
BASE="${BASE:-http://127.0.0.1:4321}"
KEY="${MEMORY_API_KEY:-dev-local}"

OWNER_ID="${OWNER_ID:-test_user}"
CLIENT_ID="${CLIENT_ID:-smoke}"
TITLE="${TITLE:-smoke test}"

HDR=(-H "X-API-Key: $KEY" -H "Content-Type: application/json")

# ---- Helpers ----
die() { echo "❌ $*" >&2; exit 1; }

need() { command -v "$1" >/dev/null 2>&1 || die "Missing required command: $1"; }

need curl
need jq
need grep

step() { echo; echo "== $* =="; }

json() {
  # Pretty-print JSON or fail loudly if it's not JSON
  jq . >/dev/null 2>&1 || die "Expected JSON but got: $(head -c 200)"
}

# ---- Tests ----

step "Health"
curl -sS "${HDR[@]}" "$BASE/healthz" | jq .
echo "✅ /healthz ok"

step "Ready"
curl -sS "${HDR[@]}" "$BASE/readyz" | jq .
echo "✅ /readyz ok"

step "Resolve conversation (prod-like entrypoint)"
CID=$(
  curl -sS -X POST "$BASE/v1/conversations/resolve" \
    "${HDR[@]}" \
    -d "{\"owner_id\":\"$OWNER_ID\",\"client_id\":\"$CLIENT_ID\",\"title\":\"$TITLE\"}" \
  | jq -r '.conversation_id'
)
[[ -n "$CID" && "$CID" != "null" ]] || die "conversation_id missing"
echo "✅ CID=$CID"

step "Chat 1 (write memory)"
CHAT1=$(
  curl -sS -X POST "$BASE/v1/chat" \
    "${HDR[@]}" \
    -d "{\"owner_id\":\"$OWNER_ID\",\"conversation_id\":\"$CID\",\"client_id\":\"$CLIENT_ID\",\"messages\":[{\"role\":\"user\",\"content\":\"Remember that my favorite snack is pretzels.\"}]}"
)
echo "$CHAT1" | jq .
RC1=$(echo "$CHAT1" | jq -r '.retrieved_count')
[[ "$RC1" =~ ^[0-9]+$ ]] || die "Chat 1 retrieved_count not numeric: $RC1"
echo "✅ Chat 1 ok (retrieved_count=$RC1)"

step "Chat 2 (should retrieve context)"
CHAT2=$(
  curl -sS -X POST "$BASE/v1/chat" \
    "${HDR[@]}" \
    -d "{\"owner_id\":\"$OWNER_ID\",\"conversation_id\":\"$CID\",\"client_id\":\"$CLIENT_ID\",\"messages\":[{\"role\":\"user\",\"content\":\"What is my favorite snack?\"}]}"
)
echo "$CHAT2" | jq .
ANS2=$(echo "$CHAT2" | jq -r '.answer')
RC2=$(echo "$CHAT2" | jq -r '.retrieved_count')

[[ -n "$ANS2" && "$ANS2" != "null" ]] || die "Chat 2 missing answer"
[[ "$RC2" =~ ^[0-9]+$ ]] || die "Chat 2 retrieved_count not numeric: $RC2"
(( RC2 > 0 )) || die "Expected retrieved_count > 0 on Chat 2, got $RC2"
echo "✅ Chat 2 ok (retrieved_count=$RC2)"

if echo "$ANS2" | grep -qi "pretzel"; then
  echo "✅ Chat 2 answer mentions pretzels"
else
  echo "⚠️  Chat 2 answer did not explicitly mention pretzels (not failing):"
  echo "    $ANS2"
fi

step "Retrieve (semantic search should return pretzels)"
RETR=$(
  curl -sS -X POST "$BASE/v1/retrieve" \
    "${HDR[@]}" \
    -d "{\"owner_id\":\"$OWNER_ID\",\"query\":\"What is my favorite snack?\",\"k\":5}"
)
echo "$RETR" | jq .

HITS_LEN=$(echo "$RETR" | jq -r '.hits | length')
[[ "$HITS_LEN" =~ ^[0-9]+$ ]] || die "Retrieve hits length not numeric: $HITS_LEN"
(( HITS_LEN > 0 )) || die "Expected retrieve hits > 0, got $HITS_LEN"

# Deterministic assertion: retrieved documents contain the memory text
echo "$RETR" | jq -r '.hits[].content' | grep -qi "pretzel" || die "Retrieve hits did not contain pretzels"
echo "✅ Retrieve ok ($HITS_LEN hits, contains pretzels)"

echo
echo "🎉 Smoke test passed"
