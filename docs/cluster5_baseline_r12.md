# Cluster 5 Baseline (R12)

This note captures a real runtime baseline for the narrow Cluster 5 MVP event ingest substrate.

## Scope

- `POST /v1/events/ingest`
- additive `event_ingest_log`
- event memories persisted as ordinary `messages`
- explicit `memory_entities` upsert only when caller provides entities

## Baseline

Observed on April 6, 2026 against the current checkout served locally on `http://127.0.0.1:4322` with the dev Postgres database at `127.0.0.1:15432`.

- Git ingest persistence:
  - request: `source_type=git`, `source_event_id=cluster5-git-real-20260406b`
  - `event_ingest_log.event_log_id`: `a0f98bd0-fd88-465a-bdb8-988921f7da08`
  - `conversation_id`: `121adcd7-f9b0-49f4-a648-98701b9a8311`
  - `message_id`: `3c3b4afe-86b7-4d69-b0cf-53464bbff3c8`
  - linked message persisted with `role=tool` and `client_id=event-stream:git`

- Calendar ingest persistence:
  - request: `source_type=calendar`, `source_event_id=cluster5-cal-real-20260406b`
  - `event_ingest_log.event_log_id`: `1d5565f7-f363-4d87-8f1f-e758be3291f1`
  - `conversation_id`: `9f1f074e-aa61-4741-bfd2-be196912f1bb`
  - `message_id`: `7fcdca71-ae91-452f-8f4a-98205e630590`
  - linked message persisted with `role=tool`, `client_id=event-stream:calendar`, and metadata `event_time=2026-04-07T13:00:00+00:00`

- Finance to portfolio normalization:
  - request: `source_type=finance`, `source_event_id=cluster5-fin-real-20260406b`
  - stored `event_ingest_log.source_type`: `portfolio`
  - `event_ingest_log.event_log_id`: `d33792e2-c151-4800-ace4-b9e8a294bce9`
  - `conversation_id`: `4858b0e5-4a5c-4ccf-8908-27fd9aee8689`
  - `message_id`: `de6c8314-6f13-4ac1-a848-0d779e833a0e`
  - linked message persisted with `role=tool`, `client_id=event-stream:portfolio`, and metadata `source_type_original=finance`

- Retrieval path proof:
  - query: `NVDA taxable account`
  - `/v1/retrieve` returned message `de6c8314-6f13-4ac1-a848-0d779e833a0e`
  - observed score: `0.60858417`
  - returned content matched the ingested portfolio event message

## Caveat

Event memories are persisted as ordinary messages and retrieval currently has no event-specific ranking.
