# Truth-Qualified Retrieval Contract

The V2 retrieval bundle preserves existing response fields and adds bounded metadata so consumers can distinguish canonical evidence from derived augmentation.

## Evidence Roles

- `canonical`: persisted message evidence returned through recent or semantic message retrieval.
- `derived`: additive artifact-derived retrieval context. Derived items do not replace canonical message records.

## Source Availability

Derived items include `source_availability` and bounded `source_checks` when source traversal succeeds. The supported states are:

- `available`
- `missing`
- `malformed`
- `unavailable`
- `owner_mismatch`
- `not_applicable`

Canonical message items use `not_applicable` because their own `source_ref` is the canonical record identity.

## Lifecycle Qualification

Retrieval items keep durable lifecycle status separate from effective freshness:

- `durable_status` reflects the stored memory lifecycle status when a related memory item exists.
- `freshness_state` is the effective freshness projection used by consumers.
- `qualification_reasons` records bounded current-use cautions such as `effective_stale`, `effective_unknown_freshness`, or `durable_rebuilding`.

Confidence, correction links, and supersession links remain in their existing additive fields when present. Missing confidence remains absent.

## Diagnostics

`retrieval_debug.truth_qualification` summarizes canonical counts, derived counts, derivative source checks, source availability counts, lifecycle-restricted derived counts, omission reasons, and canonical fallback reasons. Diagnostics are structural only and must not include raw message, artifact, derivative, prompt, trace, or event content.

## Conservative Behavior

Malformed, missing-source, cross-owner, and source-lookup-unavailable derived augmentation is omitted from `artifact_refs` with bounded diagnostics. Canonical recent evidence remains available when vector search, artifact retrieval, or derivative source traversal fails.
