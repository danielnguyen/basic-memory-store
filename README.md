# Basic Memory Store

A durable, inspectable memory substrate for conversational systems.

## What This Repo Does

`basic-memory-store` owns durable memory state and memory-adjacent operator surfaces:

- conversations and message persistence in Postgres
- semantic retrieval backed by Qdrant
- file ingestion and artifact metadata
- trace storage and lookup
- proactive suggestion state
- direct substrate APIs for inspection, retrieval, and compatibility workflows

Normal chat entrypoint ownership lives in `chat-orchestrator` at `POST /v1/chat`. Use Basic Memory Store direct chat endpoints only when you intentionally need compatibility coverage, substrate debugging, or direct smoke validation.

## Service Boundaries

- `basic-memory-store` owns memory persistence, retrieval semantics, artifact ingestion, and traces.
- `chat-orchestrator` owns the normal chat request path.
- `cognitive-runtime` owns runtime overlays, companion contract compilation, and runtime diagnostics.
- LiteLLM provides model and embedding access for this repo.

## Architecture

- **Postgres** is the system of record for authoritative data.
- **Qdrant** stores derivable semantic vectors.
- **LiteLLM** provides embedding and chat-model access.
- **FastAPI** exposes the API surface.

Core operating rules:

- Postgres remains the source of truth.
- Qdrant contents are rebuildable from Postgres-backed data.
- Memory behavior stays explicit in code and APIs.
- The API surface favors inspectability over hidden orchestration.

## Identity Model

- `owner_id` identifies who the memory belongs to.
- `client_id` identifies the source surface or device.

This separation keeps memory ownership explicit and preserves cross-device traceability.

## Schema Lifecycle

Install baseline:

- `db/baseline.sql` is the current schema snapshot for fresh installs and explicit baseline adoption.

Managed lifecycle:

- `db/migrations/managed/` is the only executable migration directory for future schema changes.
- Managed filenames must be unique and lexically sortable: `YYYYMMDDHHMMSS_domain_description.sql`.
- Applied managed migrations are immutable. Baseline refreshes must ship with a
  forward migration and preserve recognition of the previously enrolled
  baseline checksum.

Historical evidence:

- `db/migrations/legacy/` preserves historical SQL files for audit and operator reference only.
- Legacy files are never replayed automatically.

Lifecycle commands:

```bash
cd api
python -m tools.schema_migrations status
python -m tools.schema_migrations check
python -m tools.schema_migrations adopt-baseline
python -m tools.schema_migrations upgrade
```

Operational rules:

- `upgrade` installs the current baseline into an empty database, then applies pending managed migrations.
- `upgrade` refuses to guess about a non-empty untracked database and returns `adoption_required`.
- `adopt-baseline` is the explicit enrollment path for an existing database that already matches the current baseline.
- The runner records file checksums in `schema_migrations` and fails hard on checksum drift, missing files, invalid ordering, or unknown ledger state.
- Migration and adoption operations are serialized with a PostgreSQL advisory lock.
- Managed migrations are forward-only and transactional. Unsupported non-transactional SQL such as `CREATE INDEX CONCURRENTLY` is rejected.
- Failed migrations roll back fully and do not leave a successful ledger row behind.

Primary tables:

- `conversations`
- `messages`
- `artifacts`
- `derived_text`
- `embeddings`

Qdrant stores vector records for message and derived-text retrieval. If Qdrant data is lost, the retrieval index can be rebuilt from persisted source data.

## Current API Ownership

- Conversation creation and resolution:
  - `POST /v1/conversations`
  - `POST /v1/conversations/resolve`
  - `GET /v1/conversations`
- Message persistence:
  - `POST /v1/conversations/{conversation_id}/messages`
- Retrieval:
  - `POST /v2/conversations/{conversation_id}/retrieve`
  - `POST /v1/conversations/{conversation_id}/retrieve`
  - `POST /v1/retrieve` for legacy direct retrieval
- Direct compatibility chat:
  - `POST /v1/chat`
  - `POST /v1/orchestrate/chat`
- Artifacts and ingestion:
  - `POST /v1/ingestion/files`
  - `POST /v1/artifacts/init`
  - `POST /v1/artifacts/complete`
  - `GET /v1/artifacts/{artifact_id}`
- Diagnostics:
  - `GET /v1/traces/{request_id}`
  - `GET /v1/internal/memory/{memory_id}/debug?owner_id={owner_id}`
  - `GET /v1/internal/derived/{derivative_class}/{derived_id}?owner_id={owner_id}`
  - `GET /metrics`
  - `GET /healthz`
- Internal memory lifecycle:
  - `POST /v1/internal/memory/{memory_id}/transition`

## Local Run

Requirements:

- Python 3.12
- Docker / Docker Compose for local dependencies
- Postgres
- Qdrant
- LiteLLM

## Tests

The canonical test command uses the repository Dockerfile, so Docker-backed tests do not require host Python or a local virtual environment:

```bash
make test
```

This builds or reuses `basic-memory-store:test`, verifies Python 3.12 plus the checked-in pytest and psycopg dependencies, supplies deterministic test-only configuration, and runs the fake/unit/API group without live Postgres, Qdrant, LiteLLM, MinIO, private `.env` files, or external model credentials.

PostgreSQL integration tests use a disposable PostgreSQL 16 container:

```bash
make test-postgres
```

These tests cover the migration lifecycle and schema assertions, including the `memory_entities` and `memory_edges` tables. They are separate from the regular fake/unit/API group.

The focused derivative provenance proof creates derived text, a proactive
suggestion, a promoted memory item, and an episode through their production
paths, inspects their bounded shared contract, exercises derivative-assisted
retrieval, verifies owner isolation, and reopens the PostgreSQL client:

```bash
make provenance-test
```

Deterministic retrieval replay fixtures can be run independently:

```bash
make replay-test
```

The replay command loads the versioned persisted corpus, executes raw and augmented retrieval without live services, and reports a structural diff when IDs, order, provenance, token estimates, adjustments, or fallback outcomes change.

The durable lifecycle smoke uses only a disposable PostgreSQL 16 container. It
installs the schema, creates and transitions neutral memory items, verifies a
bidirectional correction relationship and ordered audit events, reopens the
storage client, and checks migration status:

```bash
make lifecycle-smoke
```

Live smoke scripts such as `scripts/validate_object_store.sh` and `scripts/validate_cluster6_r16.sh` require a disposable running stack. Do not point them at a deployed environment. The proactive smoke also requires local embedding capability; it must not be run with paid or external model credentials merely to validate this repository.

Host-local API development does require a validated Python 3.12 virtual environment. Create it with an explicit interpreter:

```bash
make dev-setup PYTHON_BIN=/path/to/python3.12
make dev-test
```

All local migration and API start targets validate that `api/.venv` uses Python 3.12. If an existing venv uses another version, move or remove it explicitly, then rerun `make dev-setup`; the setup command will not overwrite an incompatible venv.

Config split:

- local host-run config: `api/.env`
- container/compose config: repo-root `.env`

Typical local `api/.env`:

```bash
MEMORY_API_KEY=dev-local
PG_DSN=postgresql://memory_user:pass@127.0.0.1:15432/memory_db
QDRANT_URL=http://127.0.0.1:16333
LITELLM_BASE_URL=http://127.0.0.1:4000
LITELLM_API_KEY=
OPENAI_API_KEY=sk-...
CHAT_MODEL=chat_voice_openai
EMBED_MODEL=embed
REQUIRE_REQUEST_ID=true
ENFORCE_REQUEST_ID_HEADER_BODY_MATCH=true
ENABLE_TRACE_STORAGE=true
ENABLE_PROFILE_RESOLVE=true
ARTIFACTS_OBJECT_PREFIX=artifacts
ARTIFACTS_PRESIGN_TTL_S=900
OBJECT_STORE_ENABLED=true
OBJECT_STORE_ENDPOINT=http://127.0.0.1:16335
OBJECT_STORE_BUCKET=memory-artifacts
OBJECT_STORE_ACCESS_KEY=minioadmin
OBJECT_STORE_SECRET_KEY=minioadmin
OBJECT_STORE_REGION=us-east-1
RETRIEVAL_ARTIFACT_K=3
RETRIEVAL_ARTIFACT_MAX_SNIPPET_CHARS=500
INGEST_MAX_FILE_BYTES=262144
INGEST_MAX_FILES_PER_REQUEST=200
INGEST_ALLOWED_EXTENSIONS=.py,.md,.txt,.json,.yaml,.yml,.toml,.js,.ts,.tsx,.jsx,.sql,.sh,.env,.ini,.cfg,.html,.css
INGEST_EXCLUDE_GLOBS_DEFAULT=.git/*,node_modules/*,.venv/*,venv/*,dist/*,build/*,__pycache__/*,.pytest_cache/*
INGEST_CHUNK_SIZE_CHARS=1200
INGEST_CHUNK_OVERLAP_CHARS=150
```

Fast local bootstrap:

1. Create or validate the local Python 3.12 environment:

```bash
make dev-setup PYTHON_BIN=/path/to/python3.12
```

2. Start dependencies and apply schema:

```bash
make dev-up
```

3. Start the API:

```bash
make dev-start
```

Or run from `api/`:

```bash
uvicorn main:app --host 0.0.0.0 --port 4321 --reload
```

Local defaults:

- API: `http://127.0.0.1:4321`
- Postgres: `127.0.0.1:15432`
- Qdrant: `127.0.0.1:16333`
- LiteLLM: `http://127.0.0.1:4000`
- MinIO: `127.0.0.1:16335`

Docker Compose network default:

- service URL: `http://basic-memory-store:8000`

Useful local commands:

```bash
make dev-reset
make dev-logs
make dev-down
```

Swagger UI is available at `http://127.0.0.1:4321/docs` with `X-API-Key: dev-local`.

## Running In Compose

```bash
docker compose up -d --build
```

Compose startup order:

- PostgreSQL starts first.
- `memory-db-migrate` runs `python -m tools.schema_migrations upgrade` once and must exit successfully.
- `basic-memory-store` starts only after the migration service completes successfully.
- The API container runs `python -m tools.schema_migrations check` before `uvicorn`, so checksum or schema drift prevents the service from listening.

Smoke validation:

```bash
MEMORY_API_KEY=change_me BASE=http://127.0.0.1:4321 ./scripts/smoke-test.sh
```

## Current Integration Notes

- Normal chat surfaces resolve or reuse conversations through Basic Memory Store and send turns to `chat-orchestrator`.
- Retrieval bundles from this repo can include artifact references for prompt-time file snippet injection downstream.
- File ingestion remains owned here; `chat-orchestrator` does not ingest files.
- Runtime overlay and companion policy surfaces remain downstream in `cognitive-runtime`.

## Current Operator Notes

- Fresh install: run `python -m tools.schema_migrations upgrade` against an empty database.
- Normal upgrade: deploy the new image and let the one-shot migration service run `upgrade`.
- Existing database adoption: run `status`, then `adopt-baseline`, then `upgrade`.
- Restored backup validation: restore into isolated PostgreSQL 16, run `status`, `adopt-baseline`, `upgrade`, `check`, and smoke-test the memory, episode, recall, and initiative paths before touching production.
- Rollbacks are forward-only at the schema layer. Recover by restoring a PostgreSQL backup rather than attempting down migrations.
- `GET /healthz` returns service status, time, and best-effort dependency status for Postgres and Qdrant.
- `GET /metrics` exposes Prometheus-format counters.
- `GET /v1/traces/{request_id}` exposes stored request traces.
- When `OBJECT_STORE_ENABLED=true`, artifact upload and download URLs are real signed S3-compatible URLs.
- When object store support is disabled, artifact URLs remain placeholder wiring values.

## Backups

- Back up the Postgres volume.
- Back up the Qdrant volume if you want faster recovery, though it is rebuildable.
- Before production adoption, take or verify a current PostgreSQL backup and validate the restored copy in isolation.

Postgres alone is sufficient to recover authoritative memory state.
