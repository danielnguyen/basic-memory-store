# Memory Service – User Scenarios & API Flow

This document describes the **expected user scenarios** and the **API calls** each client should make when interacting with the memory service.

The system is designed to be:
- Stateless on the client side
- Durable and authoritative on the server side
- Explicit about memory scope (no hidden magic)
- Suitable for multi-device use (voice, mobile, desktop, etc.)

---

## Current integration rule

Normal chat clients should call `chat-orchestrator` `POST /v1/chat`, not Basic Memory Store `POST /v1/chat`.

Use Basic Memory Store direct chat endpoints only for legacy compatibility, smoke testing, substrate debugging, or other intentional direct-mode workflows. Do not use Basic Memory Store `POST /v1/chat`, `POST /v1/orchestrate/chat`, or `POST /v1/retrieve` for new Telegram, voice, mobile, desktop, or Cluster 10+ chat flows.

## API ownership

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

## Recommended normal flow

`surface/client -> chat-orchestrator POST /v1/chat -> BMS/cognitive-runtime/LiteLLM as downstream services`

Clients may still call Basic Memory Store `POST /v1/conversations/resolve` before `chat-orchestrator` `POST /v1/chat`, but Basic Memory Store remains a substrate service in the normal chat path rather than the primary chat entrypoint.

## Core Principles

- **Clients are stateless.**
- **Basic Memory Store owns memory state**: conversations, messages, retrieval scope, artifacts, traces, and proactive suggestion state.
- Clients decide *when* to widen memory scope.
- Basic Memory Store enforces retrieval semantics.
- chat-orchestrator decides how retrieved context is applied to normal chat turns.

---

## Identifiers Used in Examples

- `owner_id`: `user_123`
- `client_id`: `car`, `phone`, `desktop`, `voice`
- Conversation IDs are UUIDs returned by the service.
- Example content is intentionally generic.

---

## 1. Start or Resume an Interaction (Any Client)

**Examples**
- Voice assistant invocation
- Car assistant request
- Mobile app opens
- Desktop app resumes

### Goal
Obtain the correct conversation ID without the client storing state.

### API Call
POST /v1/conversations/resolve

### Request
```json
{
  "owner_id": "user_123",
  "client_id": "car",
  "idle_ttl_s": 1800
}
```

### Response
```json
{
  "conversation_id": "uuid",
  "reused": true
}
```

### Behavior
- Reuses the most recent conversation for `(owner_id, client_id)` if active.
- Otherwise creates a new conversation.
- Client does **not** need to persist conversation IDs long-term.

---

## 2. Normal Conversational Turn (Default Behavior)

Normal chat clients should use `chat-orchestrator` `POST /v1/chat` after resolving a conversation in Basic Memory Store. Basic Memory Store `POST /v1/chat` is legacy/direct mode only and should not be the default for new chat surfaces.

**Examples**
- “What device am I using?”
- “What did we talk about earlier?”

### Goal
Append a user message, retrieve relevant context **from the current conversation**, and respond.

### API Call
`chat-orchestrator` `POST /v1/chat`

### Request
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

### Server Behavior
- Persist user message (Postgres)
- Index message for retrieval (Qdrant, best-effort)
- Retrieve context scoped to `owner_id + conversation_id`
- If retrieval is weak/empty, fallback to a broader scope (owner) when configured
- Assemble prompt and call LLM
- Persist assistant response and index it (best-effort)

### Response
```json
{
  "conversation_id": "uuid",
  "answer": "You are currently interacting from your car system.",
  "retrieved_count": 6
}
```

---

## 3. Long-Term Recall (“Search My Memory”)

**Examples**
- “Search my memory for previous discussions about travel”
- “Do you remember what I said about my preferences?”

### Goal
Widen retrieval beyond the current conversation while still using the normal chat entrypoint.

### API Call
`chat-orchestrator` `POST /v1/chat`

### Request
```json
{
  "owner_id": "user_123",
  "client_id": "phone",
  "conversation_id": "uuid",
  "messages": [
    { "role": "user", "content": "Search my memory for previous travel discussions." }
  ],
  "retrieval": {
    "scope": "owner",
    "k": 12,
    "min_score": 0.2
  }
}
```

### Retrieval Scopes

| Scope | Retrieval Filter |
|------|------------------|
| conversation | owner + conversation |
| client | owner + client |
| owner | owner only |

---

## 4. Two-pass retrieval fallback (conversation → owner)

When `scope="conversation"` and the results are weak (empty, or fewer than ~half of `k`), the service may perform a second pass at a broader scope (typically `owner`) to improve recall.

Notes:
- This fallback only happens for `scope="conversation"`.
- If the client explicitly requests `scope="client"` or `scope="owner"`, that request is respected.
- The service drops self-matches so `retrieved_count` stays meaningful.

---

## 5. Multi-Device Usage

Each device:
- Uses a unique `client_id`
- Has its own rolling conversation
- Can still access shared memory via broader scopes

Recommended defaults:
- Use `scope="conversation"` for normal turns.
- Use `scope="owner"` only when the user explicitly asks to “search memory”.

---

## 6. Conversation Recovery & Introspection

### API Call
GET /v1/conversations

### Request
`/v1/conversations?owner_id=user_123&client_id=car&limit=20`

### Response
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

---

## 7. Direct Message Append (Optional)

### API Call
POST /v1/conversations/{conversation_id}/messages

Use when:
- you want to store messages without calling the LLM
- you want to backfill history from another system
- you want deterministic ingestion separate from chat

---

## 8. Tier-aware Retrieval (Additive)

### API Call
POST /v1/conversations/{conversation_id}/retrieve

### Request
```json
{
  "owner_id": "user_123",
  "client_id": "desktop",
  "surface": "vscode",
  "query": "what did I pin about travel?",
  "k": 8
}
```

### Response shape
- `working`: recent conversation window
- `semantic`: vector matches
- `pinned`: pinned-memory overlay hooks
- `policy`: policy overlay hooks
- `persona`: persona overlay hooks

---

## 9. File Ingestion

### API Call
POST /v1/ingestion/files

### Request
```json
{
  "owner_id": "user_123",
  "client_id": "desktop",
  "source_surface": "vscode",
  "repo_name": "basic-memory-store",
  "paths": ["/abs/path/to/files/or/dirs"]
}
```

### Behavior
- discovers local text/code files under the provided paths
- applies configured extension and exclude-glob filters
- chunks file contents
- embeds chunk text and stores vectors in Qdrant
- stores source attribution on `artifacts`

### Notes
- ingestion is not conversation-scoped
- retrieval mixes artifact chunk hits alongside existing message retrieval
- artifact refs are capped in retrieval output
- repeated ingest of the same file may currently surface duplicate `artifact_refs`

---

## 10. Artifact Metadata Flow (Additive)

### Initialize upload
POST /v1/artifacts/init

### Complete upload
POST /v1/artifacts/complete

### Get artifact metadata
GET /v1/artifacts/{artifact_id}

Notes:
- Existing chat clients do not need to use these endpoints.
- Object/blob upload is modeled as a presigned-url style flow.
- With `OBJECT_STORE_ENABLED=true`, `upload_url` and `download_url` are real signed URLs from MinIO/S3.
- With object-store disabled, these remain placeholder URLs for integration wiring.
- If PUT signing includes `Content-Type`, clients must upload with the exact same `Content-Type` header.

---

## 11. Legacy/direct orchestration + traces

### API Calls
- POST /v1/orchestrate/chat
- GET /v1/traces/{request_id}

`POST /v1/orchestrate/chat` is a legacy/direct-mode Basic Memory Store endpoint. Do not use it for new Telegram, voice, mobile, desktop, or Cluster 10+ chat flows. Use it only when you intentionally need direct substrate-level orchestration and trace inspection.

---

## 12. Ops Metrics

### API Call
GET /metrics

Returns Prometheus exposition format including retrieval telemetry counters.

---

## Summary: Scenarios → APIs

| Scenario | API |
|--------|-----|
| Start / resume session | Basic Memory Store `POST /v1/conversations/resolve` |
| Normal chat | `chat-orchestrator` `POST /v1/chat` |
| Long-term memory search | `chat-orchestrator` `POST /v1/chat` with broader retrieval scope |
| Legacy/direct chat | Basic Memory Store `POST /v1/chat` |
| Legacy/direct orchestration | Basic Memory Store `POST /v1/orchestrate/chat` |
| Legacy/direct retrieval | Basic Memory Store `POST /v1/retrieve` and `POST /v1/conversations/{id}/retrieve` |
| Direct retrieval | Basic Memory Store `POST /v2/conversations/{id}/retrieve` |
| List conversations | Basic Memory Store `GET /v1/conversations` |
| Manual message append | Basic Memory Store `POST /v1/conversations/{id}/messages` |
| File ingestion | Basic Memory Store `POST /v1/ingestion/files` |
| Artifact metadata flow | Basic Memory Store `POST /v1/artifacts/init` + `/complete` + `GET /v1/artifacts/{id}` |
| Explainability trace lookup | Basic Memory Store `GET /v1/traces/{request_id}` |
| Prometheus metrics | Basic Memory Store `GET /metrics` |

## 12. Legacy/direct-mode endpoints

These Basic Memory Store endpoints still exist for compatibility and debugging, but they are not the recommended normal chat path:

- `POST /v1/chat`
- `POST /v1/orchestrate/chat`
- `POST /v1/retrieve`

Do not use them for new Telegram, voice, mobile, desktop, or Cluster 10+ chat flows. Normal chat clients should resolve the conversation in Basic Memory Store, then call `chat-orchestrator` `POST /v1/chat`.

---

## Design Rationale

- Stateless clients
- Centralized memory semantics
- Explicit retrieval scope
- No premature topic modeling
- Easy future extensibility
