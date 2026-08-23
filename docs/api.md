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
| Get owner-scoped conversation facts | `GET /v1/conversations/{conversation_id}` |
| Update conversation lifecycle | `POST /v1/conversations/{conversation_id}/lifecycle` |
| Resolve a rolling same-client conversation | `POST /v1/conversations/resolve` |
| Persist a message | `POST /v1/conversations/{conversation_id}/messages` |
| Ingest an event | `POST /v1/events/ingest` |

Every conversation has a durable `lifecycle_state` of `open`, `closed`, or
`superseded`. A superseded conversation records the UUID of its open replacement.
That relationship does not redirect reads, move messages, or create a second
conversation identity.

Exact lookup requires both `conversation_id` and `owner_id`. A missing row and
an owner mismatch produce the same bounded not-found response. The projection
contains conversation metadata and lifecycle facts, never retained message
content. `GET /v1/conversations` remains owner-scoped and accepts optional
`client_id`, `lifecycle_state`, `updated_since`, and `updated_before` filters in
addition to its existing cursor and limit controls. Activity timestamps must
include a timezone. `updated_since` applies the inclusive
`updated_at >= updated_since` boundary, while `updated_before` applies the
strict `updated_at < updated_before` boundary. Both filters may be supplied and
compose with owner, client, lifecycle, cursor, and limit filtering before the
result limit.

The activity cutoff is only a mechanical durable-fact filter. Raw recency does
not select a conversation; callers remain responsible for continuation and
retirement policy and for deciding which conversation fits their current
interaction.

The lifecycle update endpoint accepts an exact owner, a target state, and a
replacement UUID only when the target is `superseded`. Open conversations may be
closed or superseded, closed conversations may be explicitly reopened or
superseded, and superseded conversations are terminal except for an identical
repeat. A replacement must be a different, open conversation owned by the same
owner. Rejected updates are atomic and disclose no cross-owner replacement facts.
The request may also include a timezone-aware `expected_updated_at`. When a real
transition is required, PostgreSQL compares that instant with the locked
conversation's current durable activity inside the lifecycle transaction. A
stale value returns the existing `conversation_lifecycle_conflict`; an identical
already-completed target remains idempotent even when the original expected
activity is repeated. Omitting the precondition preserves normal lifecycle
behavior.

`POST /v1/conversations/resolve` is a rolling same-client compatibility
resolver. It reuses only a recent open conversation for the supplied owner and
client; otherwise it creates a new open conversation. It does not select across
clients or owners and does not use message semantics or provider inference.

Message persistence writes the authoritative record to PostgreSQL and updates
the derivable semantic index where configured. Before inserting, PostgreSQL
locks and validates that the conversation exists, belongs to the submitted
owner, and is open. A rejected append inserts nothing, does not update
conversation activity, and does not invoke indexing.

The append request accepts an optional `message_id` UUID. When omitted, Basic
Memory Store generates a fresh durable UUID, so repeated requests without an ID
remain distinct appends. When supplied, the caller selects the durable message
UUID. An exact retry with the same conversation, owner, client, role, content,
stored metadata, history lineage, and policy metadata returns the same UUID
without inserting another row or advancing conversation activity. JSON object
key order does not affect equivalence.

Reusing a supplied UUID with different append content or scope returns `409`
with `message_append_conflict` and does not reveal the existing message. An
exact retry may still return a message after its conversation is later closed
or superseded because it performs no append. A new message, whether its UUID is
supplied or generated, still requires an open owner-scoped conversation. The
response remains exactly `{"message_id": "<uuid>"}`. The supplied UUID is the
message's existing durable identity; it does not create another conversation
identity, reservation, or append token.

Assistant message append accepts one optional dedicated
`history_root_lineage` field:

```json
{
  "owner_id": "owner_123",
  "role": "assistant",
  "content": "A historical explanation.",
  "client_id": "telegram:stable-client",
  "metadata": {
    "request_id": "explanation_request_1"
  },
  "history_root_lineage": {
    "schema_version": "history-root-lineage.v1",
    "root_assistant_message_id": "550e8400-e29b-41d4-a716-446655440001",
    "record_kind": "acquisition"
  },
  "policy_metadata": null
}
```

The field is accepted only for an assistant. Its strict object contains only
the exact schema version, a UUID root assistant message ID, and a closed
`support` or `acquisition` record kind. Callers cannot inject the reserved
`history_root_lineage` key through arbitrary `metadata`. A lineage-bearing
explanation must also have non-empty content and ordinary metadata containing
a bounded valid `request_id` under the immediate-history identifier contract.

The submitted object is untrusted. Before inserting the explanation, Basic
Memory Store verifies in one PostgreSQL transaction that the root already
exists in the same owner and conversation, is an original assistant message
without lineage, and directly owns a valid record of the declared kind. Root
request and record associations are derived from stored state. A valid
canonical object is stored privately under the existing message metadata JSONB
while all ordinary metadata and dedicated policy metadata remain intact. The
normal append response remains only the message acknowledgement and never
returns lineage.

Invalid lineage rejects the whole append with bounded
`history_root_lineage_invalid` detail. The message is not inserted, the root is
not altered, and no submitted request identity, lineage value, or root identity
is returned. Missing or invalid explanation request identity is the same bounded
whole-append failure.
Append has no surface field; record-surface comparison occurs when v2 history
is resolved. Appends without lineage retain their existing behavior. This
contract adds no column, table, migration, signing, encryption, or token.

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

## Internal immediate-history resolution

Authenticated internal callers can resolve the single newest durable assistant
response in an exact owner and conversation through:

```text
POST /v1/internal/immediate-history/resolve
```

The existing `immediate-history-resolution.v1` contract remains supported
unchanged. It retains its exact request and response shapes, reason codes, and
strict rejection of client-owned history hints.

The request is strict and versioned:

```json
{
  "schema_version": "immediate-history-resolution.v1",
  "request_id": "history_lookup_1",
  "owner_id": "owner_123",
  "conversation_id": "550e8400-e29b-41d4-a716-446655440000",
  "surface": "telegram",
  "explanation_kind": "support"
}
```

`explanation_kind` is either `support` for an immutable claim record or
`acquisition` for a retained acquisition manifest. The caller does not send the
previous assistant response, a response digest or first paragraph, an assistant
message ID, a claim ID, a trace ID, or a manifest ID. Such extra fields are
rejected.

The resolver first asks PostgreSQL for exactly one assistant candidate scoped to
the supplied owner and conversation. It does not inspect an older assistant
message when the newest candidate is missing, malformed, mismatched, or has no
record. For `support`, it then requests at most two claim records filtered by the
server-resolved assistant message and original request IDs so multiple records
fail as `ambiguous`. For `acquisition`, it validates the newest message, request
trace, surface, response digest, manifest association, and manifest privacy
boundary using server-owned data.

A resolved support response has this shape:

```json
{
  "schema_version": "immediate-history-resolution.v1",
  "request_id": "history_lookup_1",
  "owner_id": "owner_123",
  "conversation_id": "550e8400-e29b-41d4-a716-446655440000",
  "surface": "telegram",
  "explanation_kind": "support",
  "resolution_status": "resolved",
  "match_count": 1,
  "reason_code": "support_record_resolved",
  "record": {
    "record_kind": "support",
    "assistant_message_id": "550e8400-e29b-41d4-a716-446655440001",
    "original_request_id": "original_chat_request_1",
    "support_record": {},
    "acquisition_record": null
  }
}
```

An acquisition result uses `record_kind: "acquisition"`, sets
`support_record` to null, and returns the existing bounded acquisition-history
record in `acquisition_record`. Unresolved responses contain no record and use
one of the bounded statuses `no_record`, `ambiguous`, `invalid`, or
`unavailable`. Resolution is read-only: it performs no retrieval, model call,
acquisition, verification, or write. User-facing explanation wording and any
explicit fresh verification remain orchestration responsibilities.

The same endpoint also accepts a strict v2 request:

```json
{
  "schema_version": "immediate-history-resolution.v2",
  "request_id": "history_lookup_2",
  "owner_id": "owner_123",
  "conversation_id": "550e8400-e29b-41d4-a716-446655440000",
  "surface": "telegram",
  "explanation_kind": "acquisition"
}
```

V2 accepts no client-supplied target, history text, lineage, root or assistant
message ID, current-user-message anchor, digest, paragraph, claim, trace,
manifest, source identity, alternate record kind, or fallback kind. The
requested `explanation_kind` remains authoritative and is never inferred,
changed, or retried as the other kind.

A successful direct v2 result has this shape:

```json
{
  "schema_version": "immediate-history-resolution.v2",
  "request_id": "history_lookup_2",
  "owner_id": "owner_123",
  "conversation_id": "550e8400-e29b-41d4-a716-446655440000",
  "surface": "telegram",
  "explanation_kind": "acquisition",
  "resolution_status": "resolved",
  "resolution_source": "direct_record",
  "lineage_dereference_count": 0,
  "match_count": 1,
  "reason_code": "direct_acquisition_record_resolved",
  "record": {},
  "history_root_lineage": {
    "schema_version": "history-root-lineage.v1",
    "root_assistant_message_id": "550e8400-e29b-41d4-a716-446655440001",
    "record_kind": "acquisition"
  }
}
```

V2 is direct-first. It inspects exactly one newest assistant message and first
runs the complete existing validator for the requested kind. A direct
`resolved`, `invalid`, `ambiguous`, or `unavailable` result is final. Only the
exact direct outcome `no_record` permits inspection of that newest message's
private lineage. If lineage is absent, resolution stops. If present, the
resolver validates its strict schema and requested kind, loads exactly the
stated root once, requires the same owner and conversation and an original
assistant root without lineage, derives the root request from stored metadata,
and reruns the complete direct record association and privacy checks. Current
request surface is compared with the root record's stored surface at this time.

Successful root resolution uses `resolution_source: "root_lineage"`,
`lineage_dereference_count: 1`, the validated original root record, and the same
minimal canonical lineage. The resolver never follows lineage on a root,
inspects a second newest candidate, scans backward, performs semantic
retrieval, or retries another kind. An ordinary newest assistant without
lineage terminates the chain.

V2 uses the closed status values `resolved`, `no_record`, `ambiguous`,
`invalid`, and `unavailable`; source values `direct_record`, `root_lineage`,
and `none`; and dereference counts `0` and `1`. Its closed reason codes are:

- resolved: `direct_support_record_resolved`,
  `direct_acquisition_record_resolved`,
  `root_lineage_support_record_resolved`, and
  `root_lineage_acquisition_record_resolved`;
- no record: `direct_record_absent_lineage_absent`,
  `lineage_root_missing`, `lineage_root_unresolvable`, and
  `lineage_root_not_direct_record_owner`;
- ambiguous: `direct_support_record_ambiguous`;
- invalid direct result: `direct_response_invalid`,
  `direct_support_record_invalid`, and `direct_acquisition_record_invalid`;
- invalid lineage: `lineage_malformed`, `lineage_version_unsupported`,
  `lineage_record_kind_mismatch`, `lineage_owner_mismatch`,
  `lineage_conversation_mismatch`, `lineage_surface_mismatch`,
  `lineage_root_role_invalid`, `lineage_root_recursive`, and
  `lineage_root_association_invalid`;
- unavailable: `history_store_unavailable`.

Every unsuccessful v2 response uses `resolution_source: "none"` with null
`record` and null `history_root_lineage`. It truthfully reports whether zero or
one root lookup occurred and exposes no root ID, lineage value, private record
content, prompt, trace, digest, source identity, or arbitrary message metadata.

## Internal acquisition-history resolution

Authenticated internal callers can resolve one retained assistant response to
its acquisition manifest through:

```text
POST /v1/internal/acquisition-history/resolve
```

The resolver starts from assistant messages in the exact owner and conversation
and associates a message with its request trace through the bounded
`metadata.request_id` value already stored on the message. It does not query or
require a claim record. It performs no retrieval, acquisition, verification,
model call, or write.

An immediate-response lookup supplies the SHA-256 digest of the exact complete
assistant response and its normalized first paragraph:

```json
{
  "schema_version": "acquisition-history-resolution.v1",
  "request_id": "lookup_request_1",
  "owner_id": "owner_123",
  "conversation_id": "550e8400-e29b-41d4-a716-446655440000",
  "surface": "web",
  "target_mode": "immediate_previous",
  "response_digest": "sha256:0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
  "normalized_first_paragraph": "The exact first response paragraph."
}
```

Only the newest assistant message is inspected. A mismatch, absent trace,
absent manifest, or invalid association does not cause the resolver to scan
backward to an older response.

A quoted lookup uses `quoted_first_paragraph` and omits `response_digest`. It
searches at most 50 recent assistant messages using exact, case-sensitive
normalized first-paragraph equality. Zero matches return `no_record`; one match
is association-validated; multiple exact matches return `ambiguous` rather than
selecting the newest match.

A successful response has this bounded form:

```json
{
  "schema_version": "acquisition-history-resolution.v1",
  "request_id": "lookup_request_1",
  "owner_id": "owner_123",
  "conversation_id": "550e8400-e29b-41d4-a716-446655440000",
  "surface": "web",
  "target_mode": "immediate_previous",
  "resolution_status": "resolved",
  "match_count": 1,
  "reason_code": "immediate_response_resolved",
  "record": {
    "original_request_id": "original_chat_request_1",
    "assistant_message_id": "550e8400-e29b-41d4-a716-446655440001",
    "surface": "web",
    "trace_status": "ok",
    "response_digest": "sha256:0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
    "normalized_first_paragraph": "The exact first response paragraph.",
    "acquisition_manifest": {}
  }
}
```

Resolution validates the message owner, conversation, assistant role, message
request ID, trace request and scope, trace status, attempted manifest,
assistant-message association, exact full-response digest, and normalized first
paragraph. The response projects only the retained
`prompt.evidence_acquisition` manifest. It never returns the complete assistant
response, surrounding trace, profile, retrieval, router, model, fallback, cost,
or unrelated prompt fields.

The manifest privacy boundary accepts the bounded current manifest, including
optional `next_steps`, and remains compatible with older manifests where
`next_steps` is absent. It rejects unexpected top-level fields, excessive size
or nesting, unbounded collections or strings, and nested raw/private payload
keys. It does not reconstruct identifiers removed by privacy suppression.
Targeted, exact-fetch, hybrid, bounded-exhaustive, limited, insufficient,
unknown, deterministic provider-free, and other no-claim responses can resolve
when their stored association is valid. Resolution describes retained history;
Chat Orchestrator remains responsible for user-facing wording and for labeling
any later acquisition as new verification.

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
message, attempted acquisition, ready plan, and matching sufficient status
before storing the link. The calibrated claim remains identified by
`claim_anchor_digest`, which hashes only the normalized `claim_anchor`. The
manifest's `response_digest` independently hashes the exact complete persisted
assistant message. Basic Memory Store requires the normalized first response
paragraph to equal the claim anchor and verifies the manifest digest against the
full message bytes. Later policy-owned qualification paragraphs are therefore
associated with the response without being copied into the claim record. The
field is omitted from responses when it is absent, preserving the legacy
create, get, and list response shapes.

`claim-record.v2` is an additive generic-support contract on the same immutable
table and endpoints. A v2 record explicitly records whether the evaluated claim
was presented to the user and includes one strict bounded `support` object
containing the claim digest,
supporting and counterevidence identities, material exclusions, actual executed
derivation records, material scope limitations, and the Cognitive Runtime
calibration/disposition/qualification result. Its same-request trace must retain
the matching claim digest, runtime session and turn identifiers, and exact
presentation status under `prompt.general_evidence_reasoning`. A presented v2
record may retain the existing association in which the normalized first
assistant-response paragraph equals its exact claim anchor. When bounded display
formatting makes those texts differ, the system-owned reasoning presentation
trace may instead bind the visible first paragraph with its normalized SHA-256
digest. Basic Memory Store independently normalizes and hashes the persisted
assistant response before accepting that association; a client, model, or
provider cannot supply the authoritative binding. The visible digest grants no
claim authority and does not replace the exact claim anchor or claim digest. An
unpresented v2 record retains the shadow association without pretending the
visible assistant message contained it.
When an acquisition manifest is linked to v2, BMS validates the manifest and
message association but does not reuse legacy task-specific sufficiency as the
generic conclusion authority.

For v2, the bounded `support` object and Cognitive Runtime disposition are the
authority record. The older calibration columns are only a non-escalating
compatibility projection: runtime inference, unknown confidence/authority/
freshness, and contextual evidence references with unknown authority and
freshness. BMS rejects v2 callers that attempt to encode direct, trusted, or
confidence-bearing authority through those legacy fields. V1 semantics are
unchanged.

The support object is closed and bounded. Derivation records retain canonical
inputs/results, executor identity and digest, evidence identities, and whether
the input basis was system-established or model-interpreted. That distinction
prevents mechanical execution from being represented as verification of an
interpreted premise. The record contains no source body, provider prompt,
scratchpad, unrestricted reasoning metadata, or chain-of-thought. Existing
`claim-record.v1` request, association, and serialized response behavior is
unchanged.

The manifest link and `validated_evidence_references` serve different purposes.
The link records which acquisition process preceded the answer; the validated
references remain only the evidence actually used to support this specific
claim. Basic Memory Store does not infer that every considered, returned, or
prompt-delivered acquisition item supports the claim, and it does not copy
manifest contents into the claim record. This contract adds no manifest
payload to claim-record responses or public response field. The separate
internal acquisition-history resolver does not change claim support, which
remains a strict subset of acquired evidence.

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
