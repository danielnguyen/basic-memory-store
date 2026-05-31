# Cluster 9A R20 Smoke Flow

Set:

```sh
BASE=http://127.0.0.1:4322
API_KEY=dev-local
OWNER=cluster9a-smoke
```

Promote a memory manually:

```sh
curl -sS -X POST "$BASE/v1/internal/memory/promote" \
  -H "X-API-Key: $API_KEY" \
  -H "X-Request-ID: c9a-promote-1" \
  -H "Content-Type: application/json" \
  -d '{
    "request_id":"c9a-promote-1",
    "owner_id":"cluster9a-smoke",
    "memory_type":"core",
    "summary":"User prefers concise operational answers.",
    "source_refs":[{"ref_type":"message","ref_id":"00000000-0000-0000-0000-000000000001"}],
    "scores":{"utility_score":0.9},
    "confidence":0.8,
    "explanation":{"rationale":"manual smoke promotion"},
    "reinforce":false
  }'
```

Repeat the same promote with `reinforce=true`; it should return the existing active memory, avoid a duplicate, and append reinforcement audit state:

```sh
curl -sS -X POST "$BASE/v1/internal/memory/promote" \
  -H "X-API-Key: $API_KEY" \
  -H "X-Request-ID: c9a-promote-2" \
  -H "Content-Type: application/json" \
  -d '{
    "request_id":"c9a-promote-2",
    "owner_id":"cluster9a-smoke",
    "memory_type":"core",
    "summary":"User prefers concise operational answers.",
    "source_refs":[{"ref_type":"message","ref_id":"00000000-0000-0000-0000-000000000001"}],
    "scores":{"utility_score":0.9},
    "reinforce":true
  }'
```

Inspect the audit trail:

```sh
curl -sS "$BASE/v1/internal/memory/<memory_id>/debug" \
  -H "X-API-Key: $API_KEY"
```
