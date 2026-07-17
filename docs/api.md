# API and service behavior

Basic Memory Store exposes durable storage, retrieval, artifact, trace, and
memory-lifecycle operations over FastAPI. PostgreSQL is authoritative for
durable records; Qdrant is a rebuildable semantic index.

## Identity model

`owner_id` is the durable data owner. Authorization and retrieval boundaries
are scoped to this identity.

`client_id` identifies the source surface or device that produced a request or
record. It is provenance, not ownership, and must not be substituted for
`owner_id`.

## Conversations and messages

| Operation | Endpoint |
| --- | --- |
| Create a conversation | `POST /v1/conversations` |
| List conversations | `GET /v1/conversations` |
| Resolve a conversation identity | `POST /v1/conversations/resolve` |
| Persist a message | `POST /v1/conversations/{conversation_id}/messages` |
| Ingest an event | `POST /v1/events/ingest` |

Conversation resolution uses bounded identifying inputs to find or create the
durable conversation used by later message and retrieval calls. Message
persistence writes the authoritative record to PostgreSQL and updates the
derivable semantic index where configured.

## Retrieval

The preferred conversation retrieval API is:

```text
POST /v2/conversations/{conversation_id}/retrieve
```

It returns the current bounded retrieval bundle used by orchestration. The
following routes remain compatibility surfaces:

- `POST /v1/conversations/{conversation_id}/retrieve` for the earlier
  conversation-scoped response;
- `POST /v1/retrieve` for direct legacy retrieval.

Callers should use the v2 conversation route unless they are maintaining an
existing compatibility integration.

PostgreSQL remains authoritative when Qdrant is unavailable or needs repair.
Qdrant vectors can be rebuilt from stored messages with
[`api/tools/reindex.py`](../api/tools/reindex.py); rebuilding may require the
configured embedding service.

## Artifacts

The artifact lifecycle separates durable metadata from client-to-object-store
transfer:

1. `POST /v1/artifacts/init` creates the artifact record and returns bounded
   upload instructions.
2. The client uploads bytes directly to the configured object store.
3. `POST /v1/artifacts/complete` verifies the object and completes supported
   derivation before marking the artifact complete.
4. `GET /v1/artifacts/{artifact_id}` returns owned metadata and download
   access.

`POST /v1/ingestion/files` remains available for bounded local file ingestion.
Artifact ownership is enforced with `owner_id`; object-store access does not
replace service-level ownership checks.

## Traces and diagnostics

- `POST /v1/traces` persists a bounded trace.
- `GET /v1/traces/{request_id}` retrieves the stored trace by request ID for an
  authenticated service caller.
- `POST /v1/hygiene/scan` evaluates stored content hygiene.
- `GET /v1/hygiene/flags` lists bounded hygiene findings.

The service also exposes scoped internal diagnostic routes alongside memory,
episode, derived-data, and recall operations. These routes return bounded
diagnostics rather than raw model prompts or unrestricted dependency output.

## Internal claim records

Authenticated internal callers can persist and retrieve bounded claim-calibration
results through:

- `POST /v1/internal/claim-records`
- `GET /v1/internal/claim-records/{claim_id}?owner_id={owner_id}&conversation_id={conversation_id}`
- `GET /v1/internal/claim-records?owner_id={owner_id}&conversation_id={conversation_id}`

Claim records are immutable and scoped to one owner and conversation. Creation
requires an exact assistant-message and request-trace association. Every evidence
reference must appear in the request trace's bounded reference set; message,
artifact, and derived-text references are also checked against locally owned
records. Identities owned by another service can be stored when trace-associated,
but Basic Memory Store does not claim to dereference or independently verify them.

The optional `acquisition_manifest_id` links a claim to the bounded acquisition
process retained under `prompt.evidence_acquisition` in the same request trace.
When supplied, Basic Memory Store verifies the exact request trace, assistant
message, response digest, attempted acquisition, ready plan, and matching
sufficient status before storing the link. The field is omitted from responses
when it is absent, preserving the legacy create, get, and list response shapes.

The manifest link and `validated_evidence_references` serve different purposes.
The link records which acquisition process preceded the answer; the validated
references remain only the evidence actually used to support this specific
claim. Basic Memory Store does not infer that every considered, returned, or
prompt-delivered acquisition item supports the claim, and it does not copy
manifest contents into the claim record. This contract adds no manifest
retrieval endpoint.

The list endpoint can be filtered by `assistant_message_id` or `request_id` and
returns records in deterministic assistant-response order for later orchestration.
It does not interpret follow-up intent. Claim-record responses exclude full
answers, prompts, raw evidence, private memory, hidden reasoning, credentials,
and trace payloads.

## Internal memory lifecycle

Internal callers can evaluate, promote, reinforce, decay, and transition
memory records through:

- `POST /v1/internal/memory/evaluate`
- `POST /v1/internal/memory/promote`
- `POST /v1/internal/memory/{memory_id}/reinforce`
- `POST /v1/internal/memory/{memory_id}/decay`
- `POST /v1/internal/memory/{memory_id}/transition`
- `GET /v1/internal/memory/{memory_id}/debug?owner_id={owner_id}`

Related internal routes support episode extraction and retrieval, derived-data
invalidation and replay, and bounded recall selection. These APIs own durable
state transitions; runtime policy remains outside Basic Memory Store.

## Direct chat compatibility

`POST /v1/chat` and `POST /v1/orchestrate/chat` remain compatibility endpoints.
The normal chat entry point is Chat Orchestrator's `POST /v1/chat`, which calls
Basic Memory Store through its service boundary.

## Service endpoints

The local API defaults to `http://127.0.0.1:4321`.

| Purpose | Location |
| --- | --- |
| Health | `GET /healthz` |
| Readiness | `GET /readyz` |
| Prometheus metrics | `GET /metrics` |
| Swagger UI | `/docs` |

Most data endpoints use the configured API-key boundary. Consult
[`api/.env.example`](../api/.env.example) for current configuration names and
defaults.
