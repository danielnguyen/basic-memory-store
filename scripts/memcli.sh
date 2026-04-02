#!/usr/bin/env bash
set -euo pipefail

# memcli.sh - CLI client simulator for Basic Memory Store
#
# Usage:
#   MEMORY_API_KEY=... ./memcli.sh -c alexa_car "hello"
#   MEMORY_API_KEY=... ./memcli.sh -c alexa_home "remember my favorite snack is pretzels"
#   MEMORY_API_KEY=... ./memcli.sh -c telegram "what is my favorite snack?"
#   MEMORY_API_KEY=... ./memcli.sh -c alexa_car --new
#   MEMORY_API_KEY=... ./memcli.sh -c alexa_car --show
#
# Optional:
#   BASE_URL=http://127.0.0.1:4321 OWNER_ID=daniel ./memcli.sh -c alexa_car --debug "ping"
#   CF_ACCESS_CLIENT_ID=... CF_ACCESS_CLIENT_SECRET=... MEMORY_API_KEY=... ./memcli.sh -c alexa_car "hello"

BASE_URL="${BASE_URL:-http://127.0.0.1:4321}"
OWNER_ID="${OWNER_ID:-daniel}"
CLIENT_ID=""
DEBUG=false
NEW=false
SHOW=false
TITLE=""
IDLE_TTL_S="${IDLE_TTL_S:-7200}"  # 2 hours default (matches "rolling conversation" idea)
STATE_DIR="${STATE_DIR:-$HOME/.basic-memory-store}"
API_KEY="${MEMORY_API_KEY:-}"
CF_ACCESS_CLIENT_ID="${CF_ACCESS_CLIENT_ID:-}"
CF_ACCESS_CLIENT_SECRET="${CF_ACCESS_CLIENT_SECRET:-}"
CF_HDR=()
if [[ -n "$CF_ACCESS_CLIENT_ID" && -n "$CF_ACCESS_CLIENT_SECRET" ]]; then
  CF_HDR=(-H "CF-Access-Client-Id: $CF_ACCESS_CLIENT_ID" -H "CF-Access-Client-Secret: $CF_ACCESS_CLIENT_SECRET")
fi

die() { echo "error: $*" >&2; exit 1; }

need() {
  command -v "$1" >/dev/null 2>&1 || die "missing dependency: $1"
}

need curl
need jq

[[ -n "$API_KEY" ]] || die "MEMORY_API_KEY env var is required"

usage() {
  cat <<EOF
memcli.sh - simulate clients against Basic Memory Store

Env:
  MEMORY_API_KEY=...           required
  BASE_URL=...                 default: $BASE_URL
  CF_ACCESS_CLIENT_ID=...      optional (Cloudflare Access)
  CF_ACCESS_CLIENT_SECRET=...  optional (Cloudflare Access)
  OWNER_ID=...                 default: $OWNER_ID
  IDLE_TTL_S=...               default: $IDLE_TTL_S
  STATE_DIR=...                default: $STATE_DIR

Options:
  -c, --client <id>            client id (alexa_car, alexa_home, telegram, etc)
  --debug                      request debug info (if server supports it)
  --new                        force new conversation for this client (clears local state)
  --show                       show current conversation id for this client
  --title <title>              optional title for resolve endpoint

Examples:
  MEMORY_API_KEY=... ./memcli.sh -c alexa_car "hello"
  MEMORY_API_KEY=... ./memcli.sh -c alexa_home "remember my favorite snack is pretzels"
  MEMORY_API_KEY=... ./memcli.sh -c telegram "what is my favorite snack?"
  MEMORY_API_KEY=... ./memcli.sh -c alexa_car --new
  MEMORY_API_KEY=... ./memcli.sh -c alexa_car --show
EOF
}

# Parse args
while [[ $# -gt 0 ]]; do
  case "$1" in
    -c|--client) CLIENT_ID="${2:-}"; shift 2 ;;
    --debug) DEBUG=true; shift ;;
    --new) NEW=true; shift ;;
    --show) SHOW=true; shift ;;
    --title) TITLE="${2:-}"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) break ;;
  esac
done

[[ -n "$CLIENT_ID" ]] || die "--client is required (e.g. -c alexa_car)"

mkdir -p "$STATE_DIR"
STATE_FILE="$STATE_DIR/${OWNER_ID}__${CLIENT_ID}.cid"

if $NEW; then
  rm -f "$STATE_FILE"
  echo "cleared conversation state for owner=$OWNER_ID client=$CLIENT_ID"
  exit 0
fi

if $SHOW; then
  if [[ -f "$STATE_FILE" ]]; then
    echo "conversation_id=$(cat "$STATE_FILE")"
  else
    echo "conversation_id=<none>"
  fi
  exit 0
fi

TEXT="${1:-}"
[[ -n "$TEXT" ]] || die "missing input text. example: ./memcli.sh -c alexa_car \"hello\""

# Step 1: resolve conversation (prod-like entrypoint)
if [[ -n "$TITLE" ]]; then
  resolve_payload=$(jq -n \
    --arg owner_id "$OWNER_ID" \
    --arg client_id "$CLIENT_ID" \
    --argjson idle_ttl_s "$IDLE_TTL_S" \
    --arg title "$TITLE" \
    '{
      owner_id: $owner_id,
      client_id: $client_id,
      idle_ttl_s: $idle_ttl_s,
      title: $title
    }')
else
  resolve_payload=$(jq -n \
    --arg owner_id "$OWNER_ID" \
    --arg client_id "$CLIENT_ID" \
    --argjson idle_ttl_s "$IDLE_TTL_S" \
    '{
      owner_id: $owner_id,
      client_id: $client_id,
      idle_ttl_s: $idle_ttl_s
    }')
fi

resolve_resp=$(
  curl -sS "$BASE_URL/v1/conversations/resolve" \
    -H "Content-Type: application/json" \
    -H "X-API-Key: $API_KEY" \
    "${CF_HDR[@]}" \
    -d "$resolve_payload"
)

cid=$(echo "$resolve_resp" | jq -r '.conversation_id // empty')
[[ -n "$cid" ]] || die "resolve failed: $resolve_resp"

echo "$cid" > "$STATE_FILE"

# Step 2: chat
if $DEBUG; then
  chat_payload=$(jq -n \
    --arg owner_id "$OWNER_ID" \
    --arg client_id "$CLIENT_ID" \
    --arg conversation_id "$cid" \
    --arg text "$TEXT" \
    '{
      owner_id: $owner_id,
      client_id: $client_id,
      conversation_id: $conversation_id,
      messages: [{role:"user", content:$text}],
      debug: true
    }')
else
  chat_payload=$(jq -n \
    --arg owner_id "$OWNER_ID" \
    --arg client_id "$CLIENT_ID" \
    --arg conversation_id "$cid" \
    --arg text "$TEXT" \
    '{
      owner_id: $owner_id,
      client_id: $client_id,
      conversation_id: $conversation_id,
      messages: [{role:"user", content:$text}]
    }')
fi

chat_resp=$(
  curl -sS "$BASE_URL/v1/chat" \
    -H "Content-Type: application/json" \
    -H "X-API-Key: $API_KEY" \
    "${CF_HDR[@]}" \
    -d "$chat_payload"
)

# Pretty output
answer=$(echo "$chat_resp" | jq -r '.answer // empty')
retrieved_count=$(echo "$chat_resp" | jq -r '.retrieved_count // 0')
new_cid=$(echo "$chat_resp" | jq -r '.conversation_id // empty')

echo
echo "client=$CLIENT_ID owner=$OWNER_ID"
echo "conversation_id=$new_cid"
echo "retrieved_count=$retrieved_count"
echo
echo "$answer"

# If debug exists, show a compact summary
if $DEBUG; then
  if echo "$chat_resp" | jq -e '.debug' >/dev/null 2>&1; then
    echo
    echo "--- debug ---"
    echo "$chat_resp" | jq '.debug'
  else
    echo
    echo "(debug requested, but server did not include a debug block)"
  fi
fi
