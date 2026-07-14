# Validation

Run validation from the repository root. The suites below use repository-local
or disposable dependencies and must not point at production databases, object
stores, credentials, or paid external model services.

## Core suites

| Command | Purpose |
| --- | --- |
| `make test` | Runs the containerized unit and API suite with fake dependencies. |
| `make test-postgres` | Runs PostgreSQL integration coverage against a disposable PostgreSQL container. |
| `make provenance-test` | Checks durable source and identity provenance against disposable PostgreSQL. |
| `make replay-test` | Checks deterministic replay behavior in the test image. |
| `make raw-retrieval-test` | Exercises bounded raw retrieval behavior with test doubles. |
| `make raw-retrieval-smoke` | Exercises raw retrieval through the containerized test harness. |
| `make derivation-replay-test` | Checks deterministic artifact-derivation replay behavior. |
| `make derivation-version-test` | Checks derivation version handling and compatibility. |
| `make lifecycle-smoke` | Exercises memory lifecycle behavior against disposable PostgreSQL. |
| `make artifact-storage-test` | Runs artifact storage contract coverage in the test image. |
| `make artifact-storage-smoke` | Exercises artifact storage with disposable Compose services. |
| `make dev-test` | Runs the development suite in the host-local `api/.venv`. |
| `make process-naming-check` | Checks added source text for reserved internal naming. |

## Runtime needs

The following commands build or run Docker images and do not use the host
Python environment for the test process:

```bash
make test
make test-postgres
make provenance-test
make replay-test
make raw-retrieval-test
make raw-retrieval-smoke
make derivation-replay-test
make derivation-version-test
make lifecycle-smoke
make artifact-storage-test
make artifact-storage-smoke
```

`make test-postgres`, `make provenance-test`, and `make lifecycle-smoke` create
disposable PostgreSQL containers. `make artifact-storage-smoke` uses the
disposable services in `docker-compose.artifact-smoke.yml`. The other commands
in this group run in the repository test image with fake or bounded local
dependencies.

These commands use host-local tooling:

```bash
make dev-test
make process-naming-check
```

`make dev-test` requires the Python 3.12 environment created by
`make dev-setup`. The naming check runs the repository script with the host
shell and Python tooling.

## Isolation and cleanup

Validation configuration must use disposable local services. Do not supply
production connection strings, object-store locations, API keys, or model
credentials. The suites do not need paid or external model calls merely to
validate this repository.

The disposable-service scripts and Compose smoke suite normally clean up on
exit. After interrupting the artifact storage smoke suite, remove its services
and volumes before retrying:

```bash
docker compose -f docker-compose.artifact-smoke.yml down -v --remove-orphans
```

For another interrupted disposable-container suite, remove the containers and
networks created by that run before retrying. Confirm no validation process is
still using them first.

Validation output must not expose credentials, raw object bodies, unrestricted
message content, prompts, or exception details. Safety-sensitive outcomes
should remain bounded and owner-scoped in both successful and degraded runs.
