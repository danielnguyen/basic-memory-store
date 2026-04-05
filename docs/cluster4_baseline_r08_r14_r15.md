# Cluster 4 Baseline (R08, R14, R15)

This note captures a manual runtime baseline for future comparison and regression testing of the current Cluster 4 slice.

## Scope

- R08: Time-Aware Retrieval / Recency Decay / Historical Mode
- R14: Memory Hygiene / Drift Monitoring
- R15: Personal Knowledge Graph Layer

R15 is currently schema-only in this baseline.

## R08 Baseline

Representative recency adjustments observed for the same three seeded messages (`newest`, `middle`, `oldest`) under `time_window=all`:

- `retrieval_mode=recent`
  - `newest`: `0.173368`
  - `middle`: `0.047928`
  - `oldest`: `0.000038`
- `retrieval_mode=balanced`
  - `newest`: `0.114782`
  - `middle`: `0.076941`
  - `oldest`: `0.008338`
- `retrieval_mode=historical`
  - `newest`: `0.049727`
  - `middle`: `0.047334`
  - `oldest`: `0.035990`

Observed time-window behavior for the same seeded messages:

- `time_window=7d`, `retrieval_mode=balanced`
  - surviving semantic results: `newest` only
  - `semantic_ranked=1`
- `time_window=30d`, `retrieval_mode=balanced`
  - surviving semantic results: `newest`, `middle`
  - excluded semantic result: `oldest`
  - `semantic_ranked=2`

## R14 Baseline

Observed idempotency result for `POST /v1/hygiene/scan` after seeding duplicate and contradictory pinned memories:

- first scan: `flags_created > 0`
- second identical scan: `flags_created = 0`
- resulting open flags remained queryable through `GET /v1/hygiene/flags` without duplicate accumulation on the rerun

## R15 Baseline

Current baseline only confirms schema presence for:

- `memory_entities`
- `memory_edges`

No graph retrieval expansion behavior is part of this baseline.
