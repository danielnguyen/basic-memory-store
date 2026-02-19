# Telemetry

This repository exposes lightweight Prometheus metrics for operational visibility.

## Current probes

- `memory_skipped_qdrant_ids_total{kind="<value>"}`  
  Counts non-UUID Qdrant hit IDs skipped by API retrieval paths.
  - `kind="semantic"`: tiered semantic retrieval (`/v1/conversations/{id}/retrieve`)
  - `kind="retrieval"`: chat retrieval path (`/v1/chat`, `/v1/orchestrate/chat`)
  - `kind="retrieve"`: direct retrieval endpoint (`/v1/retrieve`)

## Endpoint

- `GET /metrics`  
  Returns Prometheus exposition format (`text/plain`).

## Future probes (suggested)

- Request totals and latency histograms per endpoint.
- LLM call latency and error counters by model/provider.
- Retrieval hit-count and fallback counters.
- Artifact lifecycle counters (`init`, `complete`, `failed`).
