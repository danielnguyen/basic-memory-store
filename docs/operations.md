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
