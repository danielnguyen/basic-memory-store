# Memory Service Usage

This document describes how operators and developers use the current Basic Memory Store API surface.

## Integration Rule

Normal chat clients call `chat-orchestrator` `POST /v1/chat`.

Basic Memory Store direct chat endpoints remain available for compatibility workflows, direct smoke tests, and substrate debugging:

- `POST /v1/chat`
- `POST /v1/orchestrate/chat`
- `POST /v1/retrieve`

## API Ownership

| Capability | Owner / Endpoint |
|------|------------------|
| Normal chat | `chat-orchestrator` `POST /v1/chat` |
| Conversation resolution | Basic Memory Store `POST /v1/conversations/resolve` |
| Direct retrieval | Basic Memory Store `POST /v2/conversations/{id}/retrieve` |
| Legacy retrieval | Basic Memory Store `POST /v1/conversations/{id}/retrieve` |
| Message append/backfill | Basic Memory Store `POST /v1/conversations/{id}/messages` |
| File ingestion | Basic Memory Store `POST /v1/ingestion/files` |
| Artifact metadata | Basic Memory Store artifact endpoints |
| Traces | Basic Memory Store `GET /v1/traces/{request_id}` |
| Proactive suggestions | Basic Memory Store proactive endpoints |
| Metrics | Basic Memory Store `GET /metrics` |

## Normal Request Flow

`surface/client -> chat-orchestrator POST /v1/chat -> basic-memory-store/cognitive-runtime/LiteLLM`

Basic Memory Store remains the durable memory substrate in that path.

## Core Behavior

- Clients stay stateless.
- Basic Memory Store owns conversations, messages, retrieval scope, artifacts, traces, and proactive suggestion state.
- Clients choose when to widen retrieval scope.
- Retrieval semantics are enforced in Basic Memory Store.
- `chat-orchestrator` decides how retrieved context is applied to a normal chat turn.

## Example Identifiers

- `owner_id`: `user_123`
- `client_id`: `car`, `phone`, `desktop`, `voice`
- Conversation IDs are UUIDs returned by the service.

## Resolve Or Reuse A Conversation

API call:

```text
POST /v1/conversations/resolve
```

Request:

```json
{
  "owner_id": "user_123",
  "client_id": "car",
  "idle_ttl_s": 1800
}
```

Response:

```json
{
  "conversation_id": "uuid",
  "reused": true
}
```

Behavior:

- Reuses the most recent active conversation for `(owner_id, client_id)`.
- Creates a new conversation when no active one matches.

## Normal Chat Turn

Resolve the conversation in Basic Memory Store, then call `chat-orchestrator` `POST /v1/chat`.

Example request:

```json
{
  "owner_id": "user_123",
  "client_id": "car",
  "conversation_id": "uuid-from-resolve",
  "messages": [
    { "role": "user", "content": "What device am I currently using?" }
  ],
  "retrieval": {
    "scope": "conversation",
    "k": 8,
    "min_score": 0.25
  }
}
```

Current retrieval scopes:

| Scope | Retrieval Filter |
|------|------------------|
| conversation | owner + conversation |
| client | owner + client |
| owner | owner only |

When `scope="conversation"` yields weak results, the service can perform a broader fallback retrieval pass. Explicit `client` or `owner` scope requests are respected as requested.

## Conversation Listing And Recovery

API call:

```text
GET /v1/conversations
```

Example query:

```text
/v1/conversations?owner_id=user_123&client_id=car&limit=20
```

Example response:

```json
{
  "conversations": [
    {
      "conversation_id": "uuid",
      "title": "Car session",
      "created_at": "...",
      "updated_at": "..."
    }
  ],
  "next_cursor": "..."
}
```

## Message Append And Backfill

API call:

```text
POST /v1/conversations/{conversation_id}/messages
```

Use this endpoint when you need to persist messages without invoking an LLM response path.

## Retrieval Endpoints

Direct retrieval:

```text
POST /v2/conversations/{conversation_id}/retrieve
```

Legacy retrieval shape:

```text
POST /v1/conversations/{conversation_id}/retrieve
POST /v1/retrieve
```

Example request:

```json
{
  "owner_id": "user_123",
  "client_id": "desktop",
  "surface": "vscode",
  "query": "what did I pin about travel?",
  "k": 8
}
```

Current response tiers can include:

- `working`
- `semantic`
- `pinned`
- `policy`
- `persona`

## File Ingestion

API call:

```text
POST /v1/ingestion/files
```

Example request:

```json
{
  "owner_id": "user_123",
  "client_id": "desktop",
  "source_surface": "vscode",
  "repo_name": "basic-memory-store",
  "paths": ["/abs/path/to/files/or/dirs"]
}
```

Current behavior:

- discovers local text and code files under the provided paths
- applies configured extension and exclude-glob filters
- chunks file contents
- embeds chunk text and stores vectors in Qdrant
- stores source attribution on `artifacts`

Current constraints:

- ingestion is not conversation-scoped
- retrieval mixes artifact chunk hits with message retrieval
- artifact refs are capped in retrieval output
- repeated ingest of the same file can surface duplicate `artifact_refs`

If your database needs the artifact-ingestion schema update, apply:

```bash
psql "$PG_DSN" -f db/migrations/20260402_artifact_ingestion_additive.sql
```

## Artifact Metadata Flow

Endpoints:

```text
POST /v1/artifacts/init
POST /v1/artifacts/complete
GET /v1/artifacts/{artifact_id}
```

Current behavior:

- upload and download are modeled as a presigned-URL flow
- `OBJECT_STORE_ENABLED=true` returns real signed MinIO or S3-compatible URLs
- object-store-disabled mode returns placeholder URLs for integration wiring
- if a signed PUT requires `Content-Type`, the upload must send the exact same header

## Direct Compatibility Endpoints

Endpoints:

```text
POST /v1/chat
POST /v1/orchestrate/chat
POST /v1/retrieve
```

These endpoints remain available for direct compatibility coverage and substrate debugging. For normal application chat flows, use `chat-orchestrator` `POST /v1/chat`.

## Traces And Metrics

Trace lookup:

```text
GET /v1/traces/{request_id}
```

Metrics:

```text
GET /metrics
```

`GET /metrics` returns Prometheus exposition format. `GET /v1/traces/{request_id}` returns stored request trace data for operator inspection.

## Scenario Summary

| Scenario | API |
|--------|-----|
| Start or resume session | Basic Memory Store `POST /v1/conversations/resolve` |
| Normal chat | `chat-orchestrator` `POST /v1/chat` |
| Long-term memory search | `chat-orchestrator` `POST /v1/chat` with broader retrieval scope |
| Direct compatibility chat | Basic Memory Store `POST /v1/chat` |
| Direct compatibility orchestration | Basic Memory Store `POST /v1/orchestrate/chat` |
| Direct retrieval | Basic Memory Store `POST /v2/conversations/{id}/retrieve` |
| Legacy retrieval | Basic Memory Store `POST /v1/retrieve` and `POST /v1/conversations/{id}/retrieve` |
| List conversations | Basic Memory Store `GET /v1/conversations` |
| Append messages | Basic Memory Store `POST /v1/conversations/{id}/messages` |
| File ingestion | Basic Memory Store `POST /v1/ingestion/files` |
| Artifact metadata | Basic Memory Store `POST /v1/artifacts/init` + `/complete` + `GET /v1/artifacts/{id}` |
| Trace lookup | Basic Memory Store `GET /v1/traces/{request_id}` |
| Metrics | Basic Memory Store `GET /metrics` |
