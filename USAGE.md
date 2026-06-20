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

## Schema Operations

Basic Memory Store now uses a frozen baseline plus forward-only managed migrations.

- `db/baseline.sql` is the immutable install and adoption snapshot.
- `db/migrations/managed/` contains all future executable migrations.
- `db/migrations/legacy/` contains historical SQL evidence only and is never replayed automatically.

Runner commands:

```bash
cd api
python -m tools.schema_migrations status
python -m tools.schema_migrations check
python -m tools.schema_migrations adopt-baseline
python -m tools.schema_migrations upgrade
```

Behavior:

- `status` is read-only and reports whether the database is empty, requires adoption, is current, has pending migrations, or has checksum or ledger errors.
- `check` is the startup guard and succeeds only when the ledger exists, the baseline checksum matches, all applied migration checksums match, there are no unknown ledger rows, and there are no pending managed migrations.
- `upgrade` installs the baseline into an empty database or applies pending managed migrations to a ledger-tracked database.
- `upgrade` refuses to auto-enroll a non-empty untracked database.
- `adopt-baseline` is the only supported path for enrolling an existing database or a restored production dump into the new ledger.
- The runner uses a PostgreSQL advisory lock so only one migration actor changes schema state at a time.
- Managed migrations are transactional and forward-only. Unsupported non-transactional statements such as `CREATE INDEX CONCURRENTLY` are rejected.
- If a managed migration fails, its transaction rolls back fully and no success row is written to `schema_migrations`.

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

Do not apply historical SQL files manually during normal operations. Use the migration runner instead.

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

## Production Adoption Runbook

1. Take or verify a current PostgreSQL backup.
2. Restore that backup into an isolated PostgreSQL 16 instance.
3. Run `python -m tools.schema_migrations status`.
4. Run `python -m tools.schema_migrations adopt-baseline`.
5. Run `python -m tools.schema_migrations upgrade`.
6. Run `python -m tools.schema_migrations check` and the Basic Memory Store smoke tests.
7. Verify table, constraint, and index parity.
8. Stop the live API for the maintenance window.
9. Run `python -m tools.schema_migrations adopt-baseline` against live production.
10. Deploy the new Compose stack and image.
11. Verify the `memory-db-migrate` one-shot service completed successfully.
12. Verify API health plus the memory, episode, recall, and initiative endpoints.

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
