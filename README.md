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
│   ├── schema.sql
│   └── migrations/
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
- Ingests local text/code files into chunked artifact knowledge
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
CHAT_MODEL=chat_voice_openai
EMBED_MODEL=embed

# Request-ID / Trace contract
REQUIRE_REQUEST_ID=true
ENFORCE_REQUEST_ID_HEADER_BODY_MATCH=true
ENABLE_TRACE_STORAGE=true
ENABLE_PROFILE_RESOLVE=true

# File ingestion / retrieval
RETRIEVAL_ARTIFACT_K=3
RETRIEVAL_ARTIFACT_MAX_SNIPPET_CHARS=500
INGEST_MAX_FILE_BYTES=262144
INGEST_MAX_FILES_PER_REQUEST=200
INGEST_ALLOWED_EXTENSIONS=.py,.md,.txt,.json,.yaml,.yml,.toml,.js,.ts,.tsx,.jsx,.sql,.sh,.env,.ini,.cfg,.html,.css
INGEST_EXCLUDE_GLOBS_DEFAULT=.git/*,node_modules/*,.venv/*,venv/*,dist/*,build/*,__pycache__/*,.pytest_cache/*
INGEST_CHUNK_SIZE_CHARS=1200
INGEST_CHUNK_OVERLAP_CHARS=150
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

Additions for file ingestion and retrieval:

- `artifacts` stores file-level source metadata
- `derived_text` stores rebuildable chunk text
- `embeddings` links chunk refs to Qdrant point ids
- Qdrant now stores both message vectors and derived-text chunk vectors

---

## Local dev (fast bootstrap)

For development, this repo provides a dev compose file to stand up dependencies quickly:

- Postgres: `pg-test` → `127.0.0.1:15432`
- Qdrant: `qdrant-test` → `127.0.0.1:16333`
- LiteLLM: `litellm-test` → `127.0.0.1:4000`
- MinIO: `minio-test` → `127.0.0.1:16335`

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
export EMBED_MODEL="embed"
export CHAT_MODEL="chat_voice_openai"
export ARTIFACTS_OBJECT_PREFIX="artifacts"
export ARTIFACTS_PRESIGN_TTL_S="900"
export OBJECT_STORE_ENABLED="true"
export OBJECT_STORE_ENDPOINT="http://127.0.0.1:16335"
export OBJECT_STORE_BUCKET="memory-artifacts"
export OBJECT_STORE_ACCESS_KEY="minioadmin"
export OBJECT_STORE_SECRET_KEY="minioadmin"
export OBJECT_STORE_REGION="us-east-1"
export RETRIEVAL_ARTIFACT_K="3"
export RETRIEVAL_ARTIFACT_MAX_SNIPPET_CHARS="500"
export INGEST_MAX_FILE_BYTES="262144"
export INGEST_MAX_FILES_PER_REQUEST="200"
export INGEST_ALLOWED_EXTENSIONS=".py,.md,.txt,.json,.yaml,.yml,.toml,.js,.ts,.tsx,.jsx,.sql,.sh,.env,.ini,.cfg,.html,.css"
export INGEST_EXCLUDE_GLOBS_DEFAULT=".git/*,node_modules/*,.venv/*,venv/*,dist/*,build/*,__pycache__/*,.pytest_cache/*"
export INGEST_CHUNK_SIZE_CHARS="1200"
export INGEST_CHUNK_OVERLAP_CHARS="150"

uvicorn main:app --host 0.0.0.0 --port 4321 --reload
```

Then open Swagger at:

`http://127.0.0.1:4321/docs`

Authorize with:

`X-API-Key = dev-key`

Health check:

`GET /healthz` returns:
- `status`
- `service`
- `time` (ISO8601)
- best-effort `dependencies` status for Postgres/Qdrant

### Local vs Docker defaults

- Local app mode (recommended for day-to-day dev):
  - `basic-memory-store` API: `http://127.0.0.1:4321`
  - `chat-orchestrator` API: `http://127.0.0.1:4361`
  - LiteLLM: `http://127.0.0.1:4000`
  - Postgres: `127.0.0.1:15432`
  - Qdrant: `127.0.0.1:16333`
  - MinIO: `127.0.0.1:16335`

- Docker compose mode (`docker-compose.yml` in this repo):
  - Service-to-service base URL inside network: `http://basic-memory-store:8000`
  - Host-published API port (if enabled in compose): `http://127.0.0.1:11440`

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
MEMORY_API_KEY=change_me BASE=http://127.0.0.1:4321 ./scripts/smoke-test.sh
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

### Tier-aware retrieval (additive)

```
POST /v1/conversations/{conversation_id}/retrieve
x-api-key: <MEMORY_API_KEY>
```

```json
{
  "owner_id": "user_123",
  "query": "what did I pin?",
  "surface": "vscode",
  "k": 8
}
```

---

### Artifact upload metadata flow (additive)

1) Initialize:

```
POST /v1/artifacts/init
```

2) Complete:

```
POST /v1/artifacts/complete
```

3) Fetch metadata:

```
GET /v1/artifacts/{artifact_id}
```

Current status: `upload_url`/`download_url` are placeholder URLs for integration wiring, not real cryptographic presigned URLs yet.
When `OBJECT_STORE_ENABLED=true`, these are real presigned S3-compatible URLs (MinIO/S3).
If PUT signing includes `Content-Type`, uploads must send the exact same `Content-Type` header.

---

### File ingestion

```
POST /v1/ingestion/files
x-api-key: <MEMORY_API_KEY>
```

```json
{
  "owner_id": "user_123",
  "client_id": "vscode",
  "source_surface": "vscode",
  "repo_name": "basic-memory-store",
  "paths": ["/abs/path/to/files/or/dirs"]
}
```

Behavior:
- ingests local text/code files only
- chunks and embeds file content
- stores source metadata on `artifacts`
- returns chunk-based `artifact_refs` in retrieval results

Current MVP constraints:
- ingestion is not conversation-scoped
- artifact retrieval is capped and mixed alongside normal memory retrieval
- repeated ingest of the same file may currently produce duplicate `artifact_refs`

Apply the additive migration before using file ingestion against an existing DB:

```bash
psql "$PG_DSN" -f db/migrations/20260402_artifact_ingestion_additive.sql
```

---

### Orchestration + traces (additive)

```
POST /v1/orchestrate/chat
GET /v1/traces/{request_id}
```

`/v1/chat` remains unchanged for existing clients. Use `/v1/orchestrate/chat` when you want surface and artifact traceability.

---

### Ops metrics

```
GET /metrics
```

Prometheus-style endpoint for lightweight service telemetry.

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
