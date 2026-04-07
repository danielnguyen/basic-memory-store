#!/usr/bin/env sh
set -eu

BASE="${BASE:-http://127.0.0.1:4322}"
API_KEY="${MEMORY_API_KEY:-${API_KEY:-}}"
OWNER_ID="${OWNER_ID:-cluster6-r16-owner}"
SUFFIX="$(date +%Y%m%d%H%M%S)"
OWNER="${OWNER_ID}-${SUFFIX}"

if [ -z "$API_KEY" ]; then
  echo "MEMORY_API_KEY or API_KEY is required" >&2
  exit 1
fi

api() {
  method="$1"
  path="$2"
  request_id="$3"
  body="${4:-}"
  if [ -n "$body" ]; then
    curl -sS -X "$method" "$BASE$path" \
      -H "X-API-Key: $API_KEY" \
      -H "X-Request-ID: $request_id" \
      -H 'Content-Type: application/json' \
      -d "$body"
  else
    curl -sS -X "$method" "$BASE$path" \
      -H "X-API-Key: $API_KEY"
  fi
}

echo "owner=$OWNER"

body=$(jq -nc --arg owner "$OWNER" '{owner_id:$owner,client_id:"vscode",title:"Cluster 6 validation"}')
CONVO_JSON=$(api POST "/v1/conversations" "r16-convo-$SUFFIX" "$body")
CONVO_ID=$(echo "$CONVO_JSON" | jq -r '.conversation_id')
test -n "$CONVO_ID"

echo "1. proactive disabled => no suggestion created"
body=$(jq -nc --arg owner "$OWNER" '{owner_id:$owner,enabled:false,allowed_surfaces_json:[],rule_prefs_json:{}}')
DISABLED_PREFS=$(curl -sS -X PUT "$BASE/v1/proactive/preferences" -H "X-API-Key: $API_KEY" -H 'Content-Type: application/json' -d "$body")
echo "$DISABLED_PREFS" | jq -e '.enabled == false' >/dev/null
body=$(jq -nc --arg rid "r16-git-disabled-$SUFFIX" --arg owner "$OWNER" --arg source_event_id "git-disabled-$SUFFIX" '{request_id:$rid,owner_id:$owner,source_type:"git",source_event_id:$source_event_id,event_type:"push",payload_json:{summary:"auth flow touch",repo:"basic-memory-store"}}')
GIT_EVENT_1=$(api POST "/v1/events/ingest" "r16-git-disabled-$SUFFIX" "$body")
GIT_EVENT_1_ID=$(echo "$GIT_EVENT_1" | jq -r '.event_log_id')
body=$(jq -nc --arg rid "r16-eval-disabled-$SUFFIX" --arg owner "$OWNER" --arg event_log_id "$GIT_EVENT_1_ID" '{request_id:$rid,owner_id:$owner,event_log_id:$event_log_id}')
EVAL_DISABLED=$(api POST "/v1/internal/proactive/evaluate" "r16-eval-disabled-$SUFFIX" "$body")
echo "$EVAL_DISABLED" | jq -e '.created_count == 0' >/dev/null

body=$(jq -nc --arg owner "$OWNER" '{owner_id:$owner,enabled:true,allowed_surfaces_json:["telegram"],rule_prefs_json:{git:{min_score:0.3},portfolio:{drift_threshold:0.05}}}')
ENABLED_PREFS=$(curl -sS -X PUT "$BASE/v1/proactive/preferences" -H "X-API-Key: $API_KEY" -H 'Content-Type: application/json' -d "$body")
echo "$ENABLED_PREFS" | jq -e '.enabled == true and .allowed_surfaces_json[0] == "telegram"' >/dev/null
body=$(jq -nc --arg owner "$OWNER" '{owner_id:$owner,role:"user",content:"We discussed auth regressions in basic-memory-store and should check risk before shipping more auth changes.",client_id:"vscode"}')
SEED_MESSAGE=$(curl -sS -X POST "$BASE/v1/conversations/$CONVO_ID/messages" -H "X-API-Key: $API_KEY" -H 'Content-Type: application/json' -d "$body")
echo "$SEED_MESSAGE" | jq -e '.message_id != null' >/dev/null

echo "2. git event + prior related memory => suggestion created with explanation"
body=$(jq -nc --arg rid "r16-git-enabled-$SUFFIX" --arg owner "$OWNER" --arg source_event_id "git-enabled-$SUFFIX" '{request_id:$rid,owner_id:$owner,source_type:"git",source_event_id:$source_event_id,event_type:"push",payload_json:{summary:"auth flow refactor",repo:"basic-memory-store",branch:"main"}}')
GIT_EVENT_2=$(api POST "/v1/events/ingest" "r16-git-enabled-$SUFFIX" "$body")
GIT_EVENT_2_ID=$(echo "$GIT_EVENT_2" | jq -r '.event_log_id')
body=$(jq -nc --arg rid "r16-eval-git-$SUFFIX" --arg owner "$OWNER" --arg event_log_id "$GIT_EVENT_2_ID" '{request_id:$rid,owner_id:$owner,event_log_id:$event_log_id}')
EVAL_GIT=$(api POST "/v1/internal/proactive/evaluate" "r16-eval-git-$SUFFIX" "$body")
echo "$EVAL_GIT" | jq -e '.created_count == 1 and .suggestions[0].status == "pending" and .suggestions[0].delivery_status == "not_attempted" and .suggestions[0].target_surface == "telegram" and .suggestions[0].explanation_json.rule == "git_risk_scan"' >/dev/null
GIT_SUGGESTION_ID=$(echo "$EVAL_GIT" | jq -r '.suggestions[0].suggestion_id')

echo "3. pending suggestion visible via API for Node-RED pickup"
PENDING=$(curl -sS "$BASE/v1/proactive/suggestions?owner_id=$OWNER&status=pending&surface=telegram" -H "X-API-Key: $API_KEY")
echo "$PENDING" | jq -e --arg sid "$GIT_SUGGESTION_ID" '.suggestions | map(select(.suggestion_id == $sid)) | length == 1' >/dev/null

echo "4. successful delivery-attempt callback marks transport state only"
body=$(jq -nc --arg owner "$OWNER" --arg external_id "node-red-$SUFFIX" '{owner_id:$owner,surface:"telegram",status:"delivered",external_id:$external_id}')
DELIVERED=$(curl -sS -X POST "$BASE/v1/proactive/suggestions/$GIT_SUGGESTION_ID/delivery-attempt" -H "X-API-Key: $API_KEY" -H 'Content-Type: application/json' -d "$body")
echo "$DELIVERED" | jq -e '.status == "pending" and .delivery_status == "delivered" and .delivery_surface == "telegram" and .delivery_external_id != null' >/dev/null

echo "5. portfolio drift beyond threshold => suggestion created with explanation"
body=$(jq -nc --arg rid "r16-port-$SUFFIX" --arg owner "$OWNER" --arg source_event_id "port-$SUFFIX" '{request_id:$rid,owner_id:$owner,source_type:"portfolio",source_event_id:$source_event_id,event_type:"allocation_drift",payload_json:{account:"taxable account",allocation_drift_pct:0.09,summary:"NVDA overweight"}}')
PORT_EVENT=$(api POST "/v1/events/ingest" "r16-port-$SUFFIX" "$body")
PORT_EVENT_ID=$(echo "$PORT_EVENT" | jq -r '.event_log_id')
body=$(jq -nc --arg rid "r16-eval-port-$SUFFIX" --arg owner "$OWNER" --arg event_log_id "$PORT_EVENT_ID" '{request_id:$rid,owner_id:$owner,event_log_id:$event_log_id}')
EVAL_PORT=$(api POST "/v1/internal/proactive/evaluate" "r16-eval-port-$SUFFIX" "$body")
echo "$EVAL_PORT" | jq -e '.created_count == 1 and .suggestions[0].kind == "portfolio_drift_review" and .suggestions[0].explanation_json.observed_drift == 0.09 and .suggestions[0].explanation_json.threshold == 0.05' >/dev/null
PORT_SUGGESTION_ID=$(echo "$EVAL_PORT" | jq -r '.suggestions[0].suggestion_id')

echo "6. failed delivery-attempt callback records failure without user dismissal"
body=$(jq -nc --arg owner "$OWNER" '{owner_id:$owner,surface:"telegram",status:"failed",error:"node-red timeout"}')
FAILED=$(curl -sS -X POST "$BASE/v1/proactive/suggestions/$PORT_SUGGESTION_ID/delivery-attempt" -H "X-API-Key: $API_KEY" -H 'Content-Type: application/json' -d "$body")
echo "$FAILED" | jq -e '.status == "pending" and .delivery_status == "failed" and .delivery_error == "node-red timeout"' >/dev/null

echo "7. feedback endpoints remain user-feedback only"
body=$(jq -nc --arg owner "$OWNER" '{owner_id:$owner,feedback_type:"useful"}')
USEFUL=$(curl -sS -X POST "$BASE/v1/proactive/suggestions/$PORT_SUGGESTION_ID/feedback" -H "X-API-Key: $API_KEY" -H 'Content-Type: application/json' -d "$body")
echo "$USEFUL" | jq -e '.status == "pending" and .feedback_type == "useful"' >/dev/null
body=$(jq -nc --arg owner "$OWNER" '{owner_id:$owner,feedback_type:"not_useful"}')
NOT_USEFUL=$(curl -sS -X POST "$BASE/v1/proactive/suggestions/$PORT_SUGGESTION_ID/feedback" -H "X-API-Key: $API_KEY" -H 'Content-Type: application/json' -d "$body")
echo "$NOT_USEFUL" | jq -e '.status == "pending" and .feedback_type == "not_useful"' >/dev/null
body=$(jq -nc --arg owner "$OWNER" '{owner_id:$owner,feedback_type:"dismissed",reason:"not now"}')
DISMISSED=$(curl -sS -X POST "$BASE/v1/proactive/suggestions/$PORT_SUGGESTION_ID/feedback" -H "X-API-Key: $API_KEY" -H 'Content-Type: application/json' -d "$body")
echo "$DISMISSED" | jq -e '.status == "dismissed" and .feedback_type == "dismissed"' >/dev/null

echo "validation_ok=true"
