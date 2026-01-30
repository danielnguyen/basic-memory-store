# Basic Memory Store

A minimal, durable, inspectable conversation memory service.

**Disclaimers:**
- AI and LLMs were used in the development of this repo.
- This repo was created for personal purposes, and is provided as-is.

## Motivation

This was created with the intention of persisting conversational history and context, agnostic of LLMs (OpenAI, Anthropic, Ollama, etc) and clients (car, phone, Alexa, local UIs, scripts, etc). This is meant to be a basic and simple store, without any additional fancy capabilities.

---

## Overview

- **Postgres** = system of record (all messages, timestamps, ownership)
- **Qdrant** = semantic retrieval only
- **LiteLLM** = LLM + embeddings gateway
- **FastAPI** = thin, explicit API layer

---

## Core principles

- Postgres always holds the truth
- Vector DB is fully derivable and disposable
- All memory behavior is visible in code
- API surface is small and stable
- Easy to migrate, back up, and reason about

---

## Repository layout

```
BASIC-MEMORY-STORE/
├── api/
│   ├── Dockerfile
│   ├── requirements.txt
│   ├── settings.py
│   ├── main.py
│   ├── clients/
│   │   └── litellm.py
│   ├── prompts/
│   │   └── context.py
│   └── storage/
│       ├── postgres.py
│       └── qdrant.py
│
├── db/
│   └── schema.sql
│
├── scripts/
│   ├── smoke-test.sh
│   └── dev_bootstrap.sh
│
├── docker-compose.yml        (optional, for non-Portainer users)
├── docker-compose.dev.yml    (local dev helpers)
├── .env.example
├── README.md
├── USAGE.md
└── LICENSE
```

---

## What this service does

- Stores **every message** durably in Postgres
- Embeds selected messages via LiteLLM
- Stores vectors in Qdrant
- Retrieves relevant historical context
- Assembles prompts deterministically
- Acts as a single “memory gateway” for all clients

Primary endpoint:

```
POST /v1/chat
```

Which:
1. Persists incoming user messages
2. Retrieves relevant past context
3. Builds a controlled prompt
4. Calls LiteLLM
5. Persists the assistant response
6. Returns the answer

---

## Identity model

- **owner_id** → who the memory belongs to (you, family member, test user, etc.)
- **client_id** → where it came from (car, phone, alexa, web, script)

This allows:
- shared long-term memory per person
- traceability across devices
- future extension to hard multi-tenancy if needed

---

## Requirements

- Python 3.12 (for local API dev)
- Docker / Docker Compose (for local dev dependencies)
- A running Qdrant container
- A running Postgres container
- A running LiteLLM container (local models or OpenAI passthrough)

---

## Environment variables

Typical `.env` contents:

```bash
# Postgres (used by docker-compose.yml)
POSTGRES_PASSWORD=change_me

# Memory API
MEMORY_API_KEY=change_me

# LiteLLM / provider
OPENAI_API_KEY=sk-...
LITELLM_API_KEY=

# Models (LiteLLM names)
CHAT_MODEL=gpt-4o-mini
EMBED_MODEL=text-embedding-3-small
```

---

## Database schema

All authoritative data lives in Postgres.

Defined in:

`db/schema.sql`

Core tables:

- `conversations`
- `messages`

Qdrant stores only:

- `message_id`
- `owner_id`
- `conversation_id`
- `role`
- embedding vector

If Qdrant is lost, it can be rebuilt entirely from Postgres.

---

## Local dev (fast bootstrap)

For development, this repo provides a dev compose file to stand up dependencies quickly:

- Postgres: `pg-test` → `127.0.0.1:15432`
- Qdrant: `qdrant-test` → `127.0.0.1:16333`
- LiteLLM: `litellm-test` → `127.0.0.1:4000`

### 1) Start dev dependencies + apply schema

```bash
make dev-up
```

This runs:
- `docker compose -f docker-compose.dev.yml up -d`
- `./scripts/dev_bootstrap.sh` (applies `db/schema.sql`)

### 2) Run the API locally

From `api/` with your venv active:

```bash
export MEMORY_API_KEY="dev-key"
export PG_DSN="postgresql://memory_user:pass@127.0.0.1:15432/memory_db"
export QDRANT_URL="http://127.0.0.1:16333"
export LITELLM_BASE_URL="http://127.0.0.1:4000"
export EMBED_MODEL="text-embedding-3-small"
export CHAT_MODEL="gpt-4o-mini"

uvicorn main:app --host 0.0.0.0 --port 4321 --reload
```

Then open Swagger at:

`http://127.0.0.1:4321/docs`

Authorize with:

`X-API-Key = dev-key`

### 3) Reset dev environment (wipe DB)

```bash
make dev-reset
```

### 4) Tail logs

```bash
make dev-logs
```

### 5) Stop dev dependencies

```bash
make dev-down
```

---

## Running (non-Portainer)

If you want to run this outside Portainer:

```bash
docker compose up -d --build
```

Then test:

```bash
MEMORY_API_KEY=change_me BASE=http://127.0.0.1:11500 ./scripts/smoke-test.sh
```

---

## API overview (examples)

### Create conversation

```
POST /v1/conversations
x-api-key: <MEMORY_API_KEY>
```

```json
{
  "owner_id": "user_123",
  "client_id": "phone",
  "title": "general chat"
}
```

---

### Chat with memory

```
POST /v1/chat
x-api-key: <MEMORY_API_KEY>
```

```json
{
  "owner_id": "user_123",
  "conversation_id": "uuid",
  "client_id": "car",
  "messages": [
    { "role": "user", "content": "Remember that I prefer oat milk." }
  ],
  "retrieval": { "k": 8 }
}
```

---

### Retrieve memories directly

```
POST /v1/retrieve
x-api-key: <MEMORY_API_KEY>
```

```json
{
  "owner_id": "user_123",
  "query": "milk preference",
  "k": 5
}
```

---

## Backups

Only two things matter:

- Postgres volume
- (optionally) Qdrant volume

Postgres is sufficient to fully rebuild memory state.

---

## Non-goals (by design)

- No agent lifecycle management
- No automatic fact extraction
- No hidden summarization jobs
- No framework-level abstractions

Those can be layered later without changing the storage contract.

---

## Status

v0.1 — minimal memory gateway

Built to be:
- understandable
- migratable
- auditable
- and safe to entrust with long-lived conversational data.
