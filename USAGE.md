# Memory Service – User Scenarios & API Flow

This document describes the **expected user scenarios** and the **API calls** each client should make when interacting with the memory service.

The system is designed to be:
- Stateless on the client side
- Durable and authoritative on the server side
- Explicit about memory scope (no hidden magic)
- Suitable for multi-device use (voice, mobile, desktop, etc.)

---

## Core Principles

- **Clients are stateless.**
- **The memory service owns all state**: conversations, messages, retrieval scope, context.
- Clients decide *when* to widen memory scope.
- The server enforces *how* memory is retrieved and applied.

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

**Examples**
- “What device am I using?”
- “What did we talk about earlier?”

### Goal
Append a user message, retrieve relevant context **from the current conversation**, and respond.

### API Call
POST /v1/chat

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
- If retrieval is weak/empty, fallback to a broader scope (owner) when configured (see below)
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
Widen retrieval beyond the current conversation.

### API Call
POST /v1/chat

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
- If the client explicitly requests `scope="client"` or `scope="owner"`, that request is respected (no fallback).
- The service drops self-matches (the message(s) inserted in the current request) so `retrieved_count` stays meaningful.

---

## 5. Multi-Device Usage

Each device:
- Uses a unique `client_id`
- Has its own rolling conversation
- Can still access shared memory via broader scopes

Recommended defaults:
- Use `scope="conversation"` for normal turns.
- Use `scope="owner"` only when the user explicitly asks to “search memory” or when conversation-local retrieval is inadequate.

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

## Summary: Scenarios → APIs

| Scenario | API |
|--------|-----|
| Start / resume session | POST /v1/conversations/resolve |
| Normal chat | POST /v1/chat |
| Long-term memory search | POST /v1/chat (scope=owner) |
| List conversations | GET /v1/conversations |
| Manual message append | POST /v1/conversations/{id}/messages |

---

## Design Rationale

- Stateless clients
- Centralized memory semantics
- Explicit retrieval scope
- No premature topic modeling
- Easy future extensibility
