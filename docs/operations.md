# Operations

This guide covers current local startup, managed schema changes, artifact
storage, and recovery behavior. Refer to
[`api/.env.example`](../api/.env.example) for configuration keys and defaults.

## Local and Compose configuration

Local host execution reads `api/.env`. The repository Compose deployment reads
the root `.env` and supplies service-to-service hostnames on its network. Do
not copy host-loopback URLs into a Compose deployment without checking which
process must reach them.

The development stack uses these host defaults:

| Service | Default location |
| --- | --- |
| Basic Memory Store API | `http://127.0.0.1:4321` |
| PostgreSQL | `127.0.0.1:15432` |
| Qdrant HTTP | `http://127.0.0.1:16333` |
| Qdrant gRPC | `127.0.0.1:16334` |
| LiteLLM | `http://127.0.0.1:4000` |
| MinIO API | `http://127.0.0.1:16335` |
| MinIO console | `http://127.0.0.1:16336` |

## Cloud answer model configuration

[`litellm/config.yaml`](../litellm/config.yaml) retains the public answer routes:

| Route | Provider model |
| --- | --- |
| `chat_voice_openai` (primary) | `openai/gpt-5.6-terra` |
| `chat_deep_openai` (fallback) | `openai/gpt-5.6-sol` |

The consumer's primary/fallback selection stays unchanged; this file only maps
aliases to models. Local routes, embeddings, credential references, privacy
controls, token/context bounds and the separately pinned history classifier
must stay unchanged.

### Compatibility and owner deployment

The [Terra](https://developers.openai.com/api/docs/models/gpt-5.6-terra) and
[Sol](https://developers.openai.com/api/docs/models/gpt-5.6-sol) documentation
lists Chat Completions, function calling and structured outputs. Verify the
actual installed LiteLLM version and its model-specific translations before
deployment: Compose specifies the mutable `main-stable` image tag, not a proven
installed version. Documentation and offline checks cannot establish account
access, installed proxy support or live answer quality.

BMS direct-chat routes have been retired; conversation requests enter Chat
Orchestrator. Its requests can carry `max_completion_tokens`, `reasoning_effort`,
`tools` and `response_format`; verify those fields survive the proxy boundary.

[GPT-5.6 migration guidance](https://developers.openai.com/api/docs/guides/upgrading-to-gpt-5p6-sol)
restricts sampling parameters when reasoning is active. In particular, an
override such as `temperature=0.2` with default reasoning is rejected by the
locally checked LiteLLM 1.105.5 adapter for both targets. Before deployment,
check whether an existing caller sends such an override. If it does, stop and
report the exact field and route for a separately reviewed correction; do not
enable broad parameter dropping, silently remove fields, or change reasoning
or token budgets to make the request succeed.

The owner performs deployment and rollback through the existing Portainer
workflow:

1. Inspect the actual `llm-memory` stack's LiteLLM bind mount and installed
   image/version. Repository Compose mounts
   `${_VOLUME_BASIC_MEMORY_STORE}/litellm/config.yaml` at `/app/config.yaml:ro`
   and starts LiteLLM with `--config /app/config.yaml`. Resolve the real source
   location from the deployed stack; do not assume an absolute host path.
2. Preserve a rollback copy of that mounted file and record the current image
   digest/version without exposing credentials. Compare the mounted mappings
   with the reviewed source and resolve unexpected differences before applying.
3. Apply only the two reviewed answer-model mappings to the mounted file, then
   reload/recreate only LiteLLM through Portainer. A source merge or BMS image
   update alone does not install this separately mounted configuration.
4. If acceptance fails, restore the saved file and reload/recreate only
   LiteLLM through the same workflow. If its image was separately changed,
   restore the recorded image too. Restoring the retired mappings restores the
   previous configuration, including its known failure; it does not repair it.

### Owner-run native acceptance

After deployment, send one ordinary question through the existing native
surface. Correlate its request ID with bounded, credential-redacted model and
fallback diagnostics: `chat_voice_openai` must resolve to
`openai/gpt-5.6-terra` and succeed without a retired-model 404 or fallback.
A correct answer with degraded fallback is not primary-route acceptance.

Exercise each answer alias through its existing Chat Completions request shape,
including the fallback alias `chat_deep_openai` → `openai/gpt-5.6-sol` directly
without deliberately breaking production's primary route. Check BMS optional
parameters where used, and CO token/reasoning fields; verify structured output
and tool behavior only where currently used. Record the exact aliases, provider
models, request IDs, proxy version and success/degraded status without prompts
or credentials. Live acceptance remains pending until the owner records these
results; offline fake responses are not equivalent evidence.

## Local startup

The host environment requires Python 3.12. From the repository root:

```bash
cp api/.env.example api/.env
make dev-setup PYTHON_BIN=/path/to/python3.12
make dev-up
make dev-start
```

`make dev-setup` creates `api/.venv` and installs
`api/requirements.txt`. `make dev-up` starts dependencies from
`docker-compose.dev.yml`, waits for PostgreSQL, and upgrades the managed
schema. `make dev-start` checks the schema and starts the API.

Useful lifecycle commands are:

```bash
make dev-logs
make dev-down
make dev-reset
```

`make dev-reset` removes local development volumes and data. Use it only when
discarding that local state is intended.

## Managed schema lifecycle

[`db/baseline.sql`](../db/baseline.sql) is the current full schema snapshot.
Forward migrations live in
[`db/migrations/managed/`](../db/migrations/managed/). Applied migrations are
recorded with checksums in PostgreSQL.

Run schema operations from the repository root:

```bash
cd api
../scripts/dev_python.sh run -m tools.schema_migrations status
../scripts/dev_python.sh run -m tools.schema_migrations check
../scripts/dev_python.sh run -m tools.schema_migrations adopt-baseline
../scripts/dev_python.sh run -m tools.schema_migrations upgrade
```

- `status` reports the current ledger and pending migrations.
- `check` exits unsuccessfully when the schema is not ready for the API.
- `upgrade` installs the baseline into an empty database or applies pending
  managed migrations to a tracked database.
- `adopt-baseline` records an existing compatible, non-empty database before
  managed upgrades begin.

Adopt only after verifying that the existing schema matches the baseline.
Managed migrations are forward-only, run under an advisory lock, and use
transactions. On a failed migration, fix the cause and rerun the upgrade; do
not manually mark the migration applied. Recovery uses a verified backup or a
new corrective migration rather than an automated down migration.

In the repository Compose deployment, PostgreSQL becomes healthy before the
one-shot `memory-db-migrate` service runs. The API starts only after that
service completes successfully and its other dependencies are healthy.

## Object-store access and signing

`OBJECT_STORE_ENDPOINT` is the endpoint the service uses for object-store API
calls. `OBJECT_STORE_PRESIGN_BASE_URL` is the client-visible base used when
creating signed upload and download URLs. They can differ when the service and
the client reach the same store through different network names.

A signed request must preserve the generated HTTP method, scheme, host, port,
path, query string, and signed headers. When
`OBJECT_STORE_INCLUDE_CONTENT_TYPE_IN_PUT_SIGNATURE` is enabled, the upload
must send the exact signed `Content-Type`. Proxies must not rewrite these
components after the signature is created.

## Text artifact derivation

Text derivation accepts supported UTF-8 content up to
`ARTIFACT_TEXT_DERIVATION_MAX_BYTES`. Larger or unsupported objects may be
stored without text derivation. Invalid UTF-8 is rejected, and a derivation
dependency failure leaves the artifact incomplete rather than reporting a
successful derived result.

Keep object reads and derivation within the configured bounds. Raw object
bodies and storage credentials must not be emitted in traces or diagnostics.

## Backup and restore validation

Back up PostgreSQL because it is authoritative for durable records. Back up
the object store as well when durable artifact bytes must be recoverable.
Qdrant can be backed up to shorten recovery, but its index is derivable from
PostgreSQL.

Validate restores in an isolated environment with separate database,
object-store, and Qdrant endpoints. Apply the managed schema check, verify
representative owned records and artifacts, and rebuild or validate the
semantic index before directing traffic to the restored services. Never test a
restore against production endpoints.

## Optional CCP proactive pages via ntfy

The publisher defaults off. Both git and portfolio initiative evaluations use
one HTTP transport after committing the canonical suggestion and decision.
Only the database insert winner publishes; repeated or concurrent evaluation
of the same owner/event/rule does not republish. Disabled preferences, denied
owners/surfaces, suppressed decisions and candidate-only validation do not page.
A requested surface other than telegram cannot publish, even if the legacy
evaluator resolves a fallback surface. No memory
snippet, portfolio data, suggestion title/body or model-provided destination is
sent. Existing suggestion/decision/feedback and delivery APIs remain canonical.

Configure `PROACTIVE_NTFY_ENABLED=true`, `PROACTIVE_NTFY_BASE_URL` (server
origin only), `PROACTIVE_NTFY_TOPIC` (dedicated topic, for example `ccp-pages`),
`PROACTIVE_NTFY_OWNER_ID` (the single authorized owner), and optionally
`PROACTIVE_NTFY_TIMEOUT_S` (default 3 seconds, maximum 10). Use the installed
server already serving Node-RED, with its existing access protections; do not
use a public ntfy server for these identifiers. Do not use `alert`, `alerts` or
`truenas-alerts`. HTTPS is preferred where the existing service supports it;
the installed private-network HTTP service is also supported. This change adds
no credentials, server configuration or receiver. Enablement does not grant
network access or authorize service changes.

### Wire contract

One POST to `<configured origin>/<configured topic>`, content type
`text/plain; charset=utf-8`, carries this JSON as the ntfy **message body**:

```json
{"version":1,"suggestion_id":"00000000-0000-4000-8000-000000000083","owner_id":"synthetic-owner","text":"CCP has a new suggestion for your attention. Open Vega to discuss it."}
```

The example is synthetic, not a valid record to deliver. The receiver must parse
ntfy's outer SSE JSON, then parse its `message` as this object. Reject unknown
versions, wrong owner, malformed IDs and non-message events. Look up the
canonical suggestion, check owner, pending status, telegram target and current
preferences, and suppress already delivered or dismissed records. Route the
safe text through the existing Mavis sender without model transformation. Keep
the suggestion ID through the send and use the existing
`POST /v1/proactive/suggestions/{id}/delivery-attempt` callback with surface
`telegram`; record actual sender success/failure and its external ID. A Telegram
sender receipt does not establish physical G2 receipt.

The publisher requires a successful HTTP response containing a nonempty ntfy
`id`, `event=message` and the configured `topic`, with a maximum 4096-byte
response and a total operation deadline. It does not follow redirects or retry.
The `proactive_ntfy_publish` log records the suggestion ID and transport outcome
`accepted` or `failed`. Neither outcome changes delivery status: it remains
`not_attempted` until the existing receiver callback. Failures preserve canonical
context and do not fail the evaluation request. A timeout or invalid response
may mean ntfy accepted the page; `failed` means acceptance was not confirmed.

### Staged owner-run activation and rollback

1. Deploy the reviewed BMS build with publishing **off** and leave hourly
   suggestion polling active. The separate Node-RED follow-up must first build
   and review the receiver for this contract, including bounded cache reconnect,
   serialized/deduplicated handling by suggestion ID, and canonical status checks.
2. Keep that receiver's sender gated off while testing parsing. Inventory a
   bounded, owner-approved set of pending/not_attempted and failed records with
   existing APIs. Resolve which records are stale, still wanted or uncertain;
   do not delete them or mass-replay them. Preserve unrelated deferred chat.
3. For cutover, pause only proactive polling and drain its in-flight sends and
   callbacks. Keep publishing off during this pause. Record the cutover time and
   chosen reconciliation IDs. Start the reviewed receiver, then enable publishing
   for the configured owner and dedicated topic. Do not run both senders together.
4. From deployed CCP, ingest one harmless synthetic portfolio event using the
   existing event-ingest API and a unique source event ID (drift above the enabled
   rule threshold). Evaluate its returned event-log ID once through
   `/v1/initiative/evaluate`, with an owner-approved test owner/preferences and
   telegram surface matching configuration. No private holdings or memory are
   needed. Observe its persisted suggestion/decision, ntfy acceptance, actual
   Mavis message and callback, then have the owner confirm native G2 receipt and
   activate Vega for an ordinary conversation about that event.
5. Before retiring polling, repeat the same evaluation and verify no second
   page. Test receiver interruption, reconnect within the server's verified cache
   retention, replay of the same cached message, and sender failure. Require one
   send for a known delivered suggestion and truthful failure callbacks. Reconcile
   records created during the cutover pause individually. Do not infer receipt
   from HTTP acceptance or claim reconnect guarantees without these live tests.
6. Roll back by disabling BMS publishing first, stopping/draining the receiver,
   reconciling any uncertain sends, and only then resuming proactive polling.
   Check its pending/not_attempted filter before resumption; failed records need
   explicit bounded handling. Preserve all canonical context and feedback.

This is best-effort paging, not durable exactly-once delivery. A crash after
suggestion insertion but before decision persistence or POST loses the automatic
page; subsequent evaluation does not retry that insertion. A crash after POST
or after Telegram send but before its callback can leave acceptance/delivery
uncertain. Existing records support bounded owner-led reconciliation, and ntfy
cache can support receiver recovery only within configured retention. Automatic
recovery beyond that window is not provided. The receiver must serialize live
and replay sends; canonical status checks alone cannot eliminate the
send-before-callback crash window. Ambiguous records require inspection, not
blind retry. There is no new durable queue, retry worker, schema or endpoint.
