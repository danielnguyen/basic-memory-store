# Cluster 9B R21 Smoke Flow

Set:

```sh
BASE=http://127.0.0.1:4322
API_KEY=dev-local
OWNER=cluster9b-smoke
```

Create an episode manually:

```sh
curl -sS -X POST "$BASE/v1/internal/episodes" \
  -H "X-API-Key: $API_KEY" \
  -H "X-Request-ID: c9b-episode-1" \
  -H "Content-Type: application/json" \
  -d '{
    "request_id":"c9b-episode-1",
    "owner_id":"cluster9b-smoke",
    "title":"Cluster 9A completion",
    "summary":"Manual capture of the Cluster 9A milestone.",
    "episode_type":"milestone",
    "source_refs":[{"ref_type":"memory_item","ref_id":"00000000-0000-0000-0000-000000000001"}],
    "trigger":{"kind":"manual"},
    "time_window":{"start":"2026-01-01","end":"2026-01-02"},
    "participants":["operator"],
    "confidence":0.8,
    "explanation":{"rationale":"manual incident capture"}
  }'
```

Repeat with a changed summary; it should update the same active episode:

```sh
curl -sS -X POST "$BASE/v1/internal/episodes" \
  -H "X-API-Key: $API_KEY" \
  -H "X-Request-ID: c9b-episode-2" \
  -H "Content-Type: application/json" \
  -d '{
    "request_id":"c9b-episode-2",
    "owner_id":"cluster9b-smoke",
    "title":"Cluster 9A completion",
    "summary":"Manual capture of the Cluster 9A milestone with revised wording.",
    "episode_type":"milestone",
    "source_refs":[{"ref_type":"memory_item","ref_id":"00000000-0000-0000-0000-000000000001"}],
    "trigger":{"kind":"manual"},
    "time_window":{"start":"2026-01-01","end":"2026-01-02"},
    "participants":["operator"]
  }'
```

Add explicit links:

```sh
curl -sS -X POST "$BASE/v1/internal/episodes/<episode_id>/links" \
  -H "X-API-Key: $API_KEY" \
  -H "X-Request-ID: c9b-links-1" \
  -H "Content-Type: application/json" \
  -d '{
    "request_id":"c9b-links-1",
    "owner_id":"cluster9b-smoke",
    "links":[
      {"ref_type":"memory_item","ref_id":"00000000-0000-0000-0000-000000000001","relationship":"supports"},
      {"ref_type":"message","ref_id":"00000000-0000-0000-0000-000000000002","relationship":"documents"}
    ]
  }'
```

Inspect the episode, links, and lifecycle events:

```sh
curl -sS "$BASE/v1/internal/episodes/<episode_id>/debug" \
  -H "X-API-Key: $API_KEY"
```
