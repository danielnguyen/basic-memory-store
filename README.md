# Basic Memory Store

Basic Memory Store is the durable storage and retrieval service for CCP. It
persists conversations, messages, artifacts, and traces, and exposes bounded
retrieval and memory-lifecycle APIs.

## Service boundaries

- Basic Memory Store owns durable memory, retrieval, artifacts, and traces.
- Chat Orchestrator owns the normal `POST /v1/chat` request path.
- Cognitive Runtime owns runtime policy and overlays.

Basic Memory Store also exposes direct compatibility chat endpoints, but new
chat integrations should enter through Chat Orchestrator.

## Architecture

- PostgreSQL is the authoritative store for durable records.
- Qdrant is a derivable semantic index that can be rebuilt from PostgreSQL.
- FastAPI exposes the HTTP service.
- LiteLLM provides model and embedding access where a feature needs it.

`owner_id` identifies the person or account that owns durable data.
`client_id` identifies the source surface or device. Callers must keep these
identities distinct.

## API capabilities

The service provides APIs for:

- conversations, messages, and event ingestion;
- semantic and conversation-scoped retrieval;
- artifact initialization, upload completion, derivation, and download;
- traces and bounded diagnostics;
- internal memory and episode lifecycle operations;
- proactive preferences, suggestions, and feedback.

See [API and service behavior](docs/api.md) for current routes and ownership
details.

## Run locally

Install Python 3.12 and Docker with Compose support, then run from the
repository root:

```bash
cp api/.env.example api/.env
make dev-setup PYTHON_BIN=/path/to/python3.12
make dev-up
make dev-start
```

`make dev-up` starts local dependencies and applies the managed schema. Keep
local configuration in `api/.env`; use [`api/.env.example`](api/.env.example)
as the current reference.

The local API defaults to `http://127.0.0.1:4321`:

- health: `GET /healthz`
- readiness: `GET /readyz`
- metrics: `GET /metrics`
- Swagger UI: `/docs`

## Validation

Primary validation commands are:

```bash
make test
make test-postgres
make dev-test
make process-naming-check
```

See [Validation](docs/validation.md) for the supported suites and their runtime
needs.

## Documentation

- [API and service behavior](docs/api.md)
- [Operations](docs/operations.md)
- [Validation](docs/validation.md)
