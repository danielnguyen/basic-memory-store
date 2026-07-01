from __future__ import annotations

import logging
import math
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from pydantic import ValidationError

from models import (
    ArtifactRef,
    DerivedProvenance,
    ObservedMetadata,
    RetrievalBundle,
    RetrievalContainmentPolicy,
    RetrievalMessageItem,
    RetrievalOptions,
    RetrievalPolicyMetadata,
    RetrievalRecordPolicyMetadata,
    RetrievalSourceRef,
)
from services.memory_lifecycle import effective_freshness_state
from services.derived_contract import derived_text_contract_view
from services.retrieval_debug import compare_bundle_summaries, summarize_bundle


SOURCE_AVAILABLE = "available"
SOURCE_MISSING = "missing"
SOURCE_MALFORMED = "malformed"
SOURCE_UNAVAILABLE = "unavailable"
SOURCE_OWNER_MISMATCH = "owner_mismatch"

LIFECYCLE_RESTRICTED_STATES = {
    "parked",
    "stale",
    "superseded",
    "forgotten_or_demoted",
    "unknown_freshness",
}
SENSITIVITY_ORDER = ("low", "medium", "high", "restricted")


def cap_snippet(text: str, max_chars: int) -> str:
    cleaned = text.strip()
    if len(cleaned) <= max_chars:
        return cleaned
    return cleaned[: max_chars - 12].rstrip() + "...(trunc)"


def retrieval_artifact_k(settings: Any) -> int:
    return int(getattr(settings, "retrieval_artifact_k", 3))


def retrieval_artifact_max_snippet_chars(settings: Any) -> int:
    return int(getattr(settings, "retrieval_artifact_max_snippet_chars", 500))


def _time_window_cutoff(time_window: str) -> datetime | None:
    now = datetime.now(UTC)
    if time_window == "7d":
        return now - timedelta(days=7)
    if time_window == "30d":
        return now - timedelta(days=30)
    if time_window == "90d":
        return now - timedelta(days=90)
    return None


def _half_life_days(settings: Any, retrieval_mode: str) -> int:
    if retrieval_mode == "recent":
        return int(getattr(settings, "retrieval_recent_half_life_days", 14))
    if retrieval_mode == "historical":
        return int(getattr(settings, "retrieval_historical_half_life_days", 365))
    return int(getattr(settings, "retrieval_balanced_half_life_days", 45))


def _safe_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace(" ", "T"))
    except ValueError:
        return None


def _in_time_window(created_at: str | None, time_window: str) -> bool:
    cutoff = _time_window_cutoff(time_window)
    if cutoff is None:
        return True
    created_dt = _safe_dt(created_at)
    if created_dt is None:
        return True
    return created_dt >= cutoff


def _message_missing_score(settings: Any, item: dict[str, object]) -> float:
    metadata = item.get("metadata") if isinstance(item, dict) else None
    if not isinstance(metadata, dict):
        return 0.0
    score = 0.0
    if metadata.get("artifact_expected") and not metadata.get("artifact_ids"):
        score += 0.08
    if metadata.get("dangling_reference"):
        score += 0.05
    return min(score, float(getattr(settings, "retrieval_missing_penalty_cap", 0.15)))


def _artifact_missing_score(settings: Any, item: dict[str, object]) -> float:
    derivation_params = item.get("derivation_params") if isinstance(item, dict) else None
    if not isinstance(derivation_params, dict):
        return 0.0
    score = 0.0
    if not item.get("file_path"):
        score += 0.08
    if derivation_params.get("linked_entities_missing"):
        score += 0.05
    return min(score, float(getattr(settings, "retrieval_missing_penalty_cap", 0.15)))


def _score_item(
    *,
    settings: Any,
    semantic_score: float | None,
    created_at: str | None,
    retrieval_mode: str,
    is_same_conversation: bool,
    is_pinned: bool,
    missing_score: float,
) -> dict[str, float]:
    base_score = float(semantic_score or 0.0)
    recency_adjustment = 0.0
    created_dt = _safe_dt(created_at)
    if created_dt is not None:
        age_days = max(0.0, (datetime.now(UTC) - created_dt).total_seconds() / 86400.0)
        boost = math.exp(-(age_days / max(1, _half_life_days(settings, retrieval_mode))))
        if retrieval_mode == "recent":
            recency_adjustment = 0.2 * boost
        elif retrieval_mode == "historical":
            recency_adjustment = 0.05 * boost
        else:
            recency_adjustment = 0.12 * boost

    conversation_boost = (
        float(getattr(settings, "retrieval_conversation_boost", 0.08))
        if is_same_conversation
        else 0.0
    )
    pinned_bias = float(getattr(settings, "retrieval_pinned_bias", 0.12)) if is_pinned else 0.0
    final_score = base_score + recency_adjustment + conversation_boost + pinned_bias - missing_score
    return {
        "semantic_score": round(base_score, 6),
        "recency_adjustment": round(recency_adjustment, 6),
        "conversation_boost": round(conversation_boost, 6),
        "pinned_bias": round(pinned_bias, 6),
        "missing_score": round(missing_score, 6),
        "final_score": round(final_score, 6),
    }


def _dedupe_artifact_refs(refs: list[ArtifactRef]) -> list[ArtifactRef]:
    best_by_key: dict[tuple[str | None, str, str], ArtifactRef] = {}
    order: list[tuple[str | None, str, str]] = []
    for ref in refs:
        key = (ref.repo_name, ref.file_path, ref.snippet)
        existing = best_by_key.get(key)
        if existing is None:
            best_by_key[key] = ref
            order.append(key)
            continue
        existing_score = existing.relevance_score if existing.relevance_score is not None else float("-inf")
        candidate_score = ref.relevance_score if ref.relevance_score is not None else float("-inf")
        if candidate_score > existing_score:
            best_by_key[key] = ref
    return [best_by_key[key] for key in order]


def _safe_uuid(raw_id: str, *, context: str) -> UUID | None:
    try:
        return UUID(raw_id)
    except (TypeError, ValueError):
        logging.warning("Skipping non-UUID retrieval hit id in %s: %r", context, raw_id)
        return None


def _normalize_domain_values(raw_value: Any) -> list[str]:
    values: list[str] = []
    if isinstance(raw_value, str):
        values = [raw_value]
    elif isinstance(raw_value, list):
        values = [item for item in raw_value if isinstance(item, str)]

    normalized: list[str] = []
    seen: set[str] = set()
    for value in values:
        cleaned = value.strip().lower().replace("-", "_").replace(" ", "_")
        if not cleaned or cleaned in seen:
            continue
        seen.add(cleaned)
        normalized.append(cleaned)
    return normalized


def _policy_metadata(raw_metadata: Any) -> RetrievalPolicyMetadata:
    metadata = raw_metadata if isinstance(raw_metadata, dict) else {}
    domains: list[str] = []
    for key in ("memory_domains", "memory_domain", "domains", "domain"):
        domains = _normalize_domain_values(metadata.get(key))
        if domains:
            break

    sensitivity = metadata.get("sensitivity")
    if isinstance(sensitivity, str):
        sensitivity = sensitivity.strip().lower().replace("-", "_").replace(" ", "_") or None
    else:
        sensitivity = None

    content_class = metadata.get("content_class")
    if isinstance(content_class, str):
        content_class = content_class.strip().lower().replace("-", "_").replace(" ", "_") or None
    else:
        content_class = None
    return RetrievalPolicyMetadata(
        memory_domains=domains,
        sensitivity=sensitivity,
        content_class=content_class,
        entity_ids=[item.strip() for item in metadata.get("entity_ids", []) if isinstance(item, str) and item.strip()]
        if isinstance(metadata.get("entity_ids"), list)
        else [],
        relationship_ids=[
            item.strip() for item in metadata.get("relationship_ids", []) if isinstance(item, str) and item.strip()
        ]
        if isinstance(metadata.get("relationship_ids"), list)
        else [],
        relationship_scopes=_normalize_domain_values(metadata.get("relationship_scopes")),
    )


def _policy_metadata_from_validated(metadata: RetrievalRecordPolicyMetadata) -> RetrievalPolicyMetadata:
    return RetrievalPolicyMetadata(
        memory_domains=list(metadata.memory_domains),
        sensitivity=metadata.sensitivity,
        content_class=metadata.content_class,
        entity_ids=list(metadata.entity_ids),
        relationship_ids=list(metadata.relationship_ids),
        relationship_scopes=list(metadata.relationship_scopes),
    )


def _sensitivity_values_through(maximum: str) -> list[str]:
    if maximum not in SENSITIVITY_ORDER:
        return []
    return [value for value in SENSITIVITY_ORDER[: SENSITIVITY_ORDER.index(maximum) + 1] if value != "restricted"]


def _containment_filter(policy: RetrievalContainmentPolicy | None, *, artifact: bool = False) -> dict[str, Any] | None:
    if policy is None:
        return None
    allowed_domains = _normalize_domain_values(policy.allowed_memory_domains)
    blocked_domains = _normalize_domain_values(policy.blocked_memory_domains)
    content_classes: list[str] = []
    allowed_sensitivities = ["low", "medium", "high"]
    if artifact:
        artifact_policy = policy.artifact_access_policy
        allowed_domains = sorted(set(allowed_domains) & set(_normalize_domain_values(artifact_policy.allowed_domains)))
        content_classes = sorted(
            set(artifact_policy.allowed_content_classes)
            & set(artifact_policy.surface_content_capabilities)
        )
        allowed_sensitivities = _sensitivity_values_through(artifact_policy.maximum_sensitivity)
    projection = policy.relationship_scope_projection
    relationship_scope = {
        "applied": bool(projection and projection.applied),
        "relationship_ids": list(projection.relationship_ids if projection else []),
        "entity_ids": list(projection.entity_ids if projection else []),
        "relationship_scopes": _normalize_domain_values(projection.relationship_scopes if projection else []),
    }
    return {
        "mode": "mandatory",
        "allowed_domains": allowed_domains,
        "blocked_domains": blocked_domains,
        "allowed_sensitivities": allowed_sensitivities,
        "content_classes": content_classes,
        "relationship_scope": relationship_scope,
        "artifact_policy_empty": artifact and (not allowed_domains or not content_classes),
    }


def _mandatory_debug_state(policy: RetrievalContainmentPolicy | None) -> dict[str, Any]:
    return {
        "enforcement_mode": "mandatory" if policy is not None else "legacy",
        "policy_supplied": policy is not None,
        "pre_limit_policy_filter_applied": policy is not None,
        "post_fetch_validation_count": 0,
        "retained_count": 0,
        "omitted_counts_by_reason": {},
        "relationship_narrowing_applied": bool(
            policy and policy.relationship_scope_projection and policy.relationship_scope_projection.applied
        ),
        "artifact_search_skipped_reason": None,
    }


def _add_policy_omission(debug_state: dict[str, Any], reason: str) -> None:
    omitted = debug_state["omitted_counts_by_reason"]
    omitted[reason] = int(omitted.get(reason, 0)) + 1


def _mandatory_policy_metadata(raw: Any) -> tuple[RetrievalPolicyMetadata | None, str | None]:
    if not isinstance(raw, dict):
        return None, "missing_policy_metadata"
    try:
        validated = RetrievalRecordPolicyMetadata.model_validate(raw)
    except ValidationError:
        return None, "malformed_policy_metadata"
    parsed = _policy_metadata_from_validated(validated)
    if not parsed.memory_domains:
        return None, "missing_policy_metadata"
    return parsed, None


def _record_allowed_by_filter(
    metadata: RetrievalPolicyMetadata,
    policy_filter: dict[str, Any],
    *,
    artifact: bool,
) -> tuple[bool, str | None]:
    domains = set(metadata.memory_domains)
    allowed_domains = set(policy_filter.get("allowed_domains") or [])
    blocked_domains = set(policy_filter.get("blocked_domains") or [])
    if not allowed_domains or not (domains & allowed_domains):
        return False, "outside_allowed_domain"
    if blocked_domains and domains & blocked_domains:
        return False, "blocked_domain"
    if metadata.sensitivity == "restricted":
        return False, "restricted_sensitivity"
    if metadata.sensitivity not in set(policy_filter.get("allowed_sensitivities") or []):
        return False, "sensitivity_ceiling_exceeded"
    if artifact:
        if metadata.content_class not in set(policy_filter.get("content_classes") or []):
            return False, "content_class_not_allowed"
    relationship = policy_filter.get("relationship_scope") or {}
    if relationship.get("applied"):
        relationship_match = bool(set(metadata.relationship_ids) & set(relationship.get("relationship_ids") or []))
        entity_match = bool(set(metadata.entity_ids) & set(relationship.get("entity_ids") or []))
        if not relationship_match and not entity_match:
            return False, "relationship_scope_mismatch"
        selected_scopes = set(relationship.get("relationship_scopes") or [])
        if selected_scopes and metadata.relationship_scopes and not (set(metadata.relationship_scopes) & selected_scopes):
            return False, "relationship_scope_mismatch"
    return True, None


def _mandatory_record_allowed(
    raw_metadata: Any,
    policy_filter: dict[str, Any],
    *,
    artifact: bool = False,
) -> tuple[RetrievalPolicyMetadata | None, str | None]:
    parsed, reason = _mandatory_policy_metadata(raw_metadata)
    if reason is not None or parsed is None:
        return None, reason
    allowed, reason = _record_allowed_by_filter(parsed, policy_filter, artifact=artifact)
    if not allowed:
        return parsed, reason
    return parsed, None


def containment_policy_filter(
    policy: RetrievalContainmentPolicy | None,
    *,
    artifact: bool = False,
) -> dict[str, Any] | None:
    return _containment_filter(policy, artifact=artifact)


def mandatory_policy_allows(
    raw_metadata: Any,
    policy_filter: dict[str, Any],
    *,
    artifact: bool = False,
) -> tuple[RetrievalPolicyMetadata | None, str | None]:
    return _mandatory_record_allowed(raw_metadata, policy_filter, artifact=artifact)


def _normalize_freshness_state(memory_item: dict[str, Any] | None) -> str:
    return effective_freshness_state(memory_item)


def _freshness_metadata(
    *,
    memory_item: dict[str, Any] | None,
    source_kind: str,
) -> dict[str, Any]:
    if not isinstance(memory_item, dict):
        return {
            "memory_id": None,
            "freshness_state": "unknown_freshness",
            "durable_status": None,
            "last_verified_at": None,
            "source_kind": source_kind,
            "confidence": None,
            "supersedes": None,
            "superseded_by": None,
        }

    last_verified_at = memory_item.get("last_reinforced_at") or memory_item.get("updated_at")
    confidence = memory_item.get("confidence")
    if not isinstance(confidence, (int, float)):
        confidence = None

    return {
        "memory_id": memory_item.get("memory_id"),
        "freshness_state": _normalize_freshness_state(memory_item),
        "durable_status": memory_item.get("status"),
        "last_verified_at": str(last_verified_at) if last_verified_at else None,
        "source_kind": source_kind,
        "confidence": float(confidence) if confidence is not None else None,
        "supersedes": memory_item.get("supersedes_memory_id"),
        "superseded_by": memory_item.get("superseded_by_memory_id"),
    }


def _truth_counts() -> dict[str, Any]:
    return {
        "canonical_result_count": 0,
        "derived_result_count": 0,
        "derivative_source_checks_attempted": 0,
        "source_available_count": 0,
        "source_missing_count": 0,
        "source_malformed_count": 0,
        "source_unavailable_count": 0,
        "source_owner_mismatch_count": 0,
        "derived_degraded_count": 0,
        "lifecycle_restricted_derived_count": 0,
        "derivative_omissions_by_reason": {},
        "canonical_fallback_reasons": [],
    }


def _add_omission(debug_state: dict[str, Any], reason: str) -> None:
    omissions = debug_state["derivative_omissions_by_reason"]
    omissions[reason] = int(omissions.get(reason, 0)) + 1


def _source_count_key(availability: str) -> str:
    if availability == SOURCE_AVAILABLE:
        return "source_available_count"
    if availability == SOURCE_MISSING:
        return "source_missing_count"
    if availability == SOURCE_MALFORMED:
        return "source_malformed_count"
    if availability == SOURCE_UNAVAILABLE:
        return "source_unavailable_count"
    if availability == SOURCE_OWNER_MISMATCH:
        return "source_owner_mismatch_count"
    return "source_unavailable_count"


def _bounded_source_check(ref: dict[str, Any], availability: str, reason: str | None = None) -> dict[str, Any]:
    check = {
        "ref_type": str(ref.get("ref_type") or "")[:64],
        "ref_id": str(ref.get("ref_id") or "")[:160],
        "support_kind": str(ref.get("support_kind") or "")[:64],
        "availability": availability,
    }
    if reason:
        check["reason"] = reason[:160]
    return check


def _safe_source_uuid(ref: dict[str, Any]) -> UUID | None:
    raw = str(ref.get("ref_id") or "").strip()
    try:
        return UUID(raw)
    except (TypeError, ValueError):
        return None


async def _check_source_ref(pg: Any, *, owner_id: str, ref: dict[str, Any]) -> dict[str, Any]:
    ref_type = str(ref.get("ref_type") or "").strip()
    ref_id = str(ref.get("ref_id") or "").strip()
    support_kind = str(ref.get("support_kind") or "").strip()
    if not ref_type or not ref_id or not support_kind:
        return _bounded_source_check(ref, SOURCE_MALFORMED, "malformed_source_ref")

    source_id = _safe_source_uuid(ref)
    if source_id is None:
        return _bounded_source_check(ref, SOURCE_MALFORMED, "source_ref_id_not_uuid")

    try:
        if ref_type == "artifact":
            row = await pg.get_artifact(source_id)
            if row is None:
                return _bounded_source_check(ref, SOURCE_MISSING, "source_not_found")
            if row.get("owner_id") != owner_id:
                return _bounded_source_check(ref, SOURCE_OWNER_MISMATCH, "source_owner_mismatch")
            return _bounded_source_check(ref, SOURCE_AVAILABLE)

        if ref_type == "message":
            if hasattr(pg, "get_message_owner"):
                message_owner = await pg.get_message_owner(source_id)
                if message_owner is None:
                    return _bounded_source_check(ref, SOURCE_MISSING, "source_not_found")
                if message_owner != owner_id:
                    return _bounded_source_check(ref, SOURCE_OWNER_MISMATCH, "source_owner_mismatch")
                return _bounded_source_check(ref, SOURCE_AVAILABLE)
            rows = await pg.get_message_snippets_by_ids([source_id])
            return _bounded_source_check(
                ref,
                SOURCE_AVAILABLE if rows else SOURCE_MISSING,
                None if rows else "source_not_found",
            )

        if ref_type == "event_log":
            row = await pg.get_event_ingest_log(source_id)
            if row is None:
                return _bounded_source_check(ref, SOURCE_MISSING, "source_not_found")
            if row.get("owner_id") != owner_id:
                return _bounded_source_check(ref, SOURCE_OWNER_MISMATCH, "source_owner_mismatch")
            return _bounded_source_check(ref, SOURCE_AVAILABLE)

        if ref_type == "memory_item":
            row = await pg.get_memory_debug(source_id, owner_id)
            return _bounded_source_check(
                ref,
                SOURCE_AVAILABLE if row else SOURCE_MISSING,
                None if row else "source_not_found",
            )

        if ref_type == "derived_text":
            row = await pg.get_derived_text_for_owner(derived_text_id=source_id, owner_id=owner_id)
            return _bounded_source_check(
                ref,
                SOURCE_AVAILABLE if row else SOURCE_MISSING,
                None if row else "source_not_found",
            )
    except Exception:
        logging.warning("Derivative source traversal unavailable")
        return _bounded_source_check(ref, SOURCE_UNAVAILABLE, "source_lookup_unavailable")

    return _bounded_source_check(ref, SOURCE_MALFORMED, "unsupported_source_ref_type")


async def _validate_source_refs(
    pg: Any,
    *,
    owner_id: str,
    source_refs: list[dict[str, Any]],
    debug_state: dict[str, Any],
) -> tuple[str, list[dict[str, Any]], str | None]:
    checks: list[dict[str, Any]] = []
    if not source_refs:
        debug_state["source_malformed_count"] += 1
        _add_omission(debug_state, "missing_derivative_source_refs")
        return SOURCE_MALFORMED, checks, "missing_derivative_source_refs"

    for ref in source_refs:
        debug_state["derivative_source_checks_attempted"] += 1
        check = await _check_source_ref(pg, owner_id=owner_id, ref=ref)
        checks.append(check)
        debug_state[_source_count_key(check["availability"])] += 1

    failing = next((check for check in checks if check["availability"] != SOURCE_AVAILABLE), None)
    if failing is not None:
        reason_by_availability = {
            SOURCE_MALFORMED: "malformed_derivative_source_ref",
            SOURCE_MISSING: "missing_derivative_source_record",
            SOURCE_UNAVAILABLE: "derivative_source_lookup_unavailable",
            SOURCE_OWNER_MISMATCH: "cross_owner_derivative_source_ref",
        }
        reason = reason_by_availability.get(failing["availability"], "derivative_source_unavailable")
        _add_omission(debug_state, reason)
        return str(failing["availability"]), checks, reason

    return SOURCE_AVAILABLE, checks, None


def _qualification_reasons(*, freshness_state: str, durable_status: str | None) -> list[str]:
    reasons: list[str] = []
    if freshness_state in LIFECYCLE_RESTRICTED_STATES:
        reasons.append(f"effective_{freshness_state}")
    if durable_status in {
        "contradicted",
        "invalidated",
        "retracted",
        "expired",
        "forgotten_or_demoted",
        "rebuilding",
        "superseded",
    }:
        reasons.append(f"durable_{durable_status}")
    return list(dict.fromkeys(reasons))


def _state_counts_for_items(items: list[Any]) -> dict[str, int]:
    counts = {
        "stale": 0,
        "contradicted": 0,
        "superseded": 0,
        "retracted": 0,
        "unsupported_validation_state": 0,
    }
    unsupported_statuses = {"invalidated", "expired", "forgotten_or_demoted", "rebuilding"}
    for item in items:
        freshness_state = str(getattr(item, "freshness_state", "") or "")
        durable_status = str(getattr(item, "durable_status", "") or "")
        if freshness_state == "stale" or durable_status == "stale":
            counts["stale"] += 1
        if durable_status == "contradicted":
            counts["contradicted"] += 1
        if freshness_state == "superseded" or durable_status == "superseded":
            counts["superseded"] += 1
        if durable_status == "retracted":
            counts["retracted"] += 1
        if durable_status in unsupported_statuses:
            counts["unsupported_validation_state"] += 1
    return counts


def _merge_state_counts(*states: dict[str, int]) -> dict[str, int]:
    merged = {
        "stale": 0,
        "contradicted": 0,
        "superseded": 0,
        "retracted": 0,
        "unsupported_validation_state": 0,
    }
    for state in states:
        for key in merged:
            merged[key] += int(state.get(key, 0))
    return merged


def _domain_filter_state(
    *,
    policy_metadata: RetrievalPolicyMetadata,
    allowed_domains: set[str],
    blocked_domains: set[str],
) -> tuple[bool, bool]:
    if not policy_metadata.memory_domains:
        return False, True

    domains = set(policy_metadata.memory_domains)
    if blocked_domains and domains & blocked_domains:
        return True, False
    if allowed_domains and not (domains & allowed_domains):
        return True, False
    return True, True


def _filter_message_items(
    *,
    items: list[RetrievalMessageItem],
    allowed_domains: set[str],
    blocked_domains: set[str],
    debug_state: dict[str, int],
) -> list[RetrievalMessageItem]:
    if not allowed_domains and not blocked_domains:
        return items

    kept: list[RetrievalMessageItem] = []
    for item in items:
        tagged, allowed = _domain_filter_state(
            policy_metadata=item.policy_metadata,
            allowed_domains=allowed_domains,
            blocked_domains=blocked_domains,
        )
        if tagged:
            debug_state["tagged_records_evaluated"] += 1
        else:
            debug_state["untagged_records_not_domain_enforced"] += 1
        if not allowed:
            debug_state["tagged_records_filtered"] += 1
            continue
        kept.append(item)
    return kept


def _filter_artifact_refs(
    *,
    items: list[ArtifactRef],
    allowed_domains: set[str],
    blocked_domains: set[str],
    debug_state: dict[str, int],
) -> list[ArtifactRef]:
    if not allowed_domains and not blocked_domains:
        return items

    kept: list[ArtifactRef] = []
    for item in items:
        tagged, allowed = _domain_filter_state(
            policy_metadata=item.policy_metadata,
            allowed_domains=allowed_domains,
            blocked_domains=blocked_domains,
        )
        if tagged:
            debug_state["tagged_records_evaluated"] += 1
        else:
            debug_state["untagged_records_not_domain_enforced"] += 1
        if not allowed:
            debug_state["tagged_records_filtered"] += 1
            continue
        kept.append(item)
    return kept


async def retrieve_ranked_messages(
    *,
    pg: Any,
    qdrant: Any,
    settings: Any,
    owner_id: str,
    query: str,
    opts: RetrievalOptions,
    conversation_id: UUID | None,
    client_id: str | None,
    exclude_message_ids: list[str] | None = None,
    context: str = "retrieval",
    policy_filter: dict[str, Any] | None = None,
    policy_debug: dict[str, Any] | None = None,
) -> dict[str, Any]:
    conversation_filter: str | None = None
    client_filter: str | None = None
    if opts.scope == "conversation":
        conversation_filter = str(conversation_id) if conversation_id is not None else None
    elif opts.scope == "client":
        client_filter = client_id

    vector_status = "ok"
    fallback_to_raw_reasons: list[str] = []
    try:
        semantic_hits = await qdrant.search(
            owner_id=owner_id,
            query=query,
            k=opts.k,
            min_score=opts.min_score,
            conversation_id=conversation_filter,
            client_id=client_filter,
            exclude_message_ids=exclude_message_ids,
            policy_filter=policy_filter,
        )
    except Exception:
        logging.warning("Vector retrieval unavailable; continuing with canonical recent messages")
        semantic_hits = []
        vector_status = "unavailable"
        fallback_to_raw_reasons.append("vector_unavailable")
    semantic_ids: list[UUID] = []
    semantic_score_by_id: dict[str, float] = {}
    invalid_hit_ids = 0
    for hit in semantic_hits:
        message_id = _safe_uuid(getattr(hit, "message_id", None), context=context)
        if message_id is None:
            invalid_hit_ids += 1
            continue
        semantic_ids.append(message_id)
        semantic_score_by_id[str(message_id)] = float(getattr(hit, "score", 0.0) or 0.0)
    if invalid_hit_ids:
        fallback_to_raw_reasons.append("malformed_vector_result")
    semantic_snips = await pg.get_message_snippets_by_ids(semantic_ids)
    missing_source_count = max(0, len(semantic_ids) - len(semantic_snips))
    if missing_source_count:
        fallback_to_raw_reasons.append("missing_canonical_source")

    ranked_semantic: list[tuple[dict[str, Any], dict[str, float]]] = []
    for snippet in semantic_snips:
        if not _in_time_window(snippet.get("created_at"), opts.time_window):
            continue
        if policy_filter is not None:
            if policy_debug is not None:
                policy_debug["post_fetch_validation_count"] += 1
            _, rejection_reason = _mandatory_record_allowed(
                snippet.get("policy_metadata"),
                policy_filter,
            )
            if rejection_reason is not None:
                if policy_debug is not None:
                    _add_policy_omission(policy_debug, rejection_reason)
                continue
        score_details = _score_item(
            settings=settings,
            semantic_score=semantic_score_by_id.get(snippet["message_id"]),
            created_at=snippet.get("created_at"),
            retrieval_mode=opts.retrieval_mode,
            is_same_conversation=(
                conversation_id is not None and snippet.get("conversation_id") == str(conversation_id)
            ),
            is_pinned=False,
            missing_score=_message_missing_score(settings, snippet),
        )
        ranked_semantic.append((snippet, score_details))
    ranked_semantic.sort(key=lambda item: item[1]["final_score"], reverse=True)
    ranked_semantic = ranked_semantic[: opts.k]

    return {
        "semantic_hits": semantic_hits,
        "semantic_snips": semantic_snips,
        "ranked_semantic": ranked_semantic,
        "retrieval_debug": {
            "time_window": opts.time_window,
            "retrieval_mode": opts.retrieval_mode,
            "semantic_candidates": len(semantic_snips),
            "semantic_ranked": len(ranked_semantic),
            "vector_status": vector_status,
            "semantic_invalid_hit_ids": invalid_hit_ids,
            "semantic_missing_source_count": missing_source_count,
            "fallback_to_raw_reasons": fallback_to_raw_reasons,
            "mandatory_policy_filter_applied": policy_filter is not None,
        },
    }


async def build_retrieval_bundle(
    *,
    pg: Any,
    qdrant: Any,
    settings: Any,
    owner_id: str,
    conversation_id: UUID,
    client_id: str | None,
    query: str,
    opts: RetrievalOptions,
    include_artifacts: bool = True,
    allowed_memory_domains: list[str] | None = None,
    blocked_memory_domains: list[str] | None = None,
    containment_policy: RetrievalContainmentPolicy | None = None,
) -> RetrievalBundle:
    policy_debug = _mandatory_debug_state(containment_policy)
    message_policy_filter = _containment_filter(containment_policy, artifact=False)
    artifact_policy_filter = _containment_filter(containment_policy, artifact=True)
    message_results = await retrieve_ranked_messages(
        pg=pg,
        qdrant=qdrant,
        settings=settings,
        owner_id=owner_id,
        query=query,
        opts=opts,
        conversation_id=conversation_id,
        client_id=client_id,
        context="retrieve_bundle",
        policy_filter=message_policy_filter,
        policy_debug=policy_debug,
    )
    ranked_semantic = message_results["ranked_semantic"]

    artifact_k = retrieval_artifact_k(settings) if include_artifacts else 0
    artifact_status = "not_requested" if artifact_k == 0 else "ok"
    artifact_omission_reasons: list[str] = []
    if artifact_k > 0 and artifact_policy_filter is not None and artifact_policy_filter.get("artifact_policy_empty"):
        artifact_hits = []
        artifact_status = "not_requested"
        artifact_omission_reasons.append("artifact_policy_empty")
        policy_debug["artifact_search_skipped_reason"] = "artifact_policy_empty"
        _add_policy_omission(policy_debug, "artifact_policy_empty")
    elif artifact_k > 0:
        try:
            artifact_hits = await qdrant.search_artifact_chunks(
                owner_id=owner_id,
                query=query,
                k=min(max(artifact_k * 20, artifact_k), 100) if artifact_policy_filter is not None else artifact_k,
                min_score=opts.min_score,
                client_id=(client_id if opts.scope == "client" else None),
                conversation_id=(conversation_id if opts.scope == "conversation" else None),
                policy_filter=artifact_policy_filter,
            )
        except Exception:
            logging.warning("Artifact retrieval unavailable; continuing without artifact snippets")
            artifact_hits = []
            artifact_status = "unavailable"
            artifact_omission_reasons.append("artifact_retrieval_unavailable")
    else:
        artifact_hits = []

    artifact_ids: list[UUID] = []
    artifact_score_by_id: dict[str, float] = {}
    artifact_invalid_hit_ids = 0
    for hit in artifact_hits:
        derived_text_id = _safe_uuid(
            getattr(hit, "derived_text_id", None),
            context="retrieve_bundle_artifacts",
        )
        if derived_text_id is None:
            artifact_invalid_hit_ids += 1
            continue
        artifact_ids.append(derived_text_id)
        artifact_score_by_id[str(derived_text_id)] = float(getattr(hit, "score", 0.0) or 0.0)
    if artifact_invalid_hit_ids:
        artifact_status = "degraded"
        artifact_omission_reasons.append("malformed_artifact_result")

    artifact_snips = await pg.get_derived_text_snippets_by_ids(artifact_ids)
    artifact_missing_source_count = max(0, len(artifact_ids) - len(artifact_snips))
    if artifact_missing_source_count:
        artifact_status = "degraded"
        artifact_omission_reasons.append("missing_derivative_source")
    truth_debug = _truth_counts()
    truth_debug["canonical_fallback_reasons"] = list(
        message_results["retrieval_debug"]["fallback_to_raw_reasons"]
    )
    ranked_artifacts: list[tuple[dict[str, Any], dict[str, float], dict[str, Any], str, list[dict[str, Any]]]] = []
    malformed_artifact_provenance_count = 0
    cross_owner_artifact_provenance_count = 0
    for snippet in artifact_snips:
        if not _in_time_window(snippet.get("created_at"), opts.time_window):
            continue
        if artifact_policy_filter is not None:
            policy_debug["post_fetch_validation_count"] += 1
            _, rejection_reason = _mandatory_record_allowed(
                snippet.get("policy_metadata"),
                artifact_policy_filter,
                artifact=True,
            )
            if rejection_reason is not None:
                _add_policy_omission(policy_debug, rejection_reason)
                artifact_status = "degraded"
                artifact_omission_reasons.append(rejection_reason)
                continue
        if snippet.get("owner_id") != owner_id:
            cross_owner_artifact_provenance_count += 1
            artifact_status = "degraded"
            artifact_omission_reasons.append("cross_owner_derivative_provenance")
            truth_debug["source_owner_mismatch_count"] += 1
            _add_omission(truth_debug, "cross_owner_derivative_provenance")
            continue
        try:
            provenance = derived_text_contract_view(snippet)
        except ValueError:
            malformed_artifact_provenance_count += 1
            artifact_status = "degraded"
            artifact_omission_reasons.append("malformed_derivative_provenance")
            _add_omission(truth_debug, "malformed_derivative_provenance")
            truth_debug["source_malformed_count"] += 1
            continue
        source_availability, source_checks, source_failure_reason = await _validate_source_refs(
            pg,
            owner_id=owner_id,
            source_refs=provenance["source_refs"],
            debug_state=truth_debug,
        )
        if source_failure_reason is not None:
            artifact_status = "degraded"
            artifact_omission_reasons.append(source_failure_reason)
            continue
        score_details = _score_item(
            settings=settings,
            semantic_score=artifact_score_by_id.get(snippet["derived_text_id"]),
            created_at=snippet.get("created_at"),
            retrieval_mode=opts.retrieval_mode,
            is_same_conversation=False,
            is_pinned=False,
            missing_score=_artifact_missing_score(settings, snippet),
        )
        ranked_artifacts.append((snippet, score_details, provenance, source_availability, source_checks))
    ranked_artifacts.sort(key=lambda item: item[1]["final_score"], reverse=True)
    ranked_artifacts = ranked_artifacts[:artifact_k]

    if message_policy_filter is None:
        recent_snips = await pg.get_recent_message_items(
            conversation_id=conversation_id,
            limit=getattr(settings, "recent_turns", 10),
        )
    else:
        recent_snips = await pg.get_recent_message_items(
            conversation_id=conversation_id,
            limit=getattr(settings, "recent_turns", 10),
            policy_filter=message_policy_filter,
        )
    source_refs = [
        {"ref_type": "message", "ref_id": snippet["message_id"]}
        for snippet in recent_snips
    ] + [
        {"ref_type": "message", "ref_id": snippet["message_id"]}
        for snippet, _ in ranked_semantic
    ] + [
        {"ref_type": "derived_text", "ref_id": snippet["derived_text_id"]}
        for snippet, _, _, _, _ in ranked_artifacts
    ]
    memory_items_by_ref = await pg.get_memory_items_for_source_refs(
        owner_id=owner_id,
        source_refs=source_refs,
    )

    recent_items = [
        RetrievalMessageItem(
            message_id=s["message_id"],
            owner_id=owner_id,
            evidence_role="canonical",
            conversation_id=s["conversation_id"],
            role=s["role"],
            content=s["content"],
            created_at=s["created_at"],
            score=None,
            source_ref=RetrievalSourceRef(ref_type="message", ref_id=s["message_id"]),
            source_availability="not_applicable",
            qualification_reasons=["canonical_recent"],
            policy_metadata=_policy_metadata(s.get("policy_metadata") or s.get("metadata")),
            **_freshness_metadata(memory_item=memory_items_by_ref.get(("message", s["message_id"])), source_kind="message"),
        )
        for s in recent_snips
    ]
    semantic_items = [
        RetrievalMessageItem(
            message_id=s["message_id"],
            owner_id=owner_id,
            evidence_role="canonical",
            conversation_id=s["conversation_id"],
            role=s["role"],
            content=s["content"],
            created_at=s["created_at"],
            score=score_details["final_score"],
            score_details=score_details,
            source_ref=RetrievalSourceRef(ref_type="message", ref_id=s["message_id"]),
            source_availability="not_applicable",
            qualification_reasons=["canonical_semantic"],
            policy_metadata=_policy_metadata(s.get("policy_metadata") or s.get("metadata")),
            **_freshness_metadata(memory_item=memory_items_by_ref.get(("message", s["message_id"])), source_kind="message"),
        )
        for s, score_details in ranked_semantic
    ]
    for item in recent_items:
        item.qualification_reasons = list(
            dict.fromkeys([
                *item.qualification_reasons,
                *_qualification_reasons(
                    freshness_state=item.freshness_state,
                    durable_status=item.durable_status,
                ),
            ])
        )
    for item in semantic_items:
        item.qualification_reasons = list(
            dict.fromkeys([
                *item.qualification_reasons,
                *_qualification_reasons(
                    freshness_state=item.freshness_state,
                    durable_status=item.durable_status,
                ),
            ])
        )

    artifact_refs = _dedupe_artifact_refs(
        [
            ArtifactRef(
                artifact_id=s["artifact_id"],
                owner_id=owner_id,
                evidence_role="derived",
                file_path=s["file_path"],
                snippet=cap_snippet(s["text"], retrieval_artifact_max_snippet_chars(settings)),
                relevance_score=score_details["final_score"],
                repo_name=s.get("repo_name"),
                score_details=score_details,
                source_ref=RetrievalSourceRef(ref_type="derived_text", ref_id=s["derived_text_id"]),
                source_availability=source_availability,
                source_checks=source_checks,
                policy_metadata=_policy_metadata(s.get("policy_metadata")),
                provenance=DerivedProvenance(
                    **provenance,
                    retrieval_reason="included_by_artifact_similarity",
                ),
                **_freshness_metadata(
                    memory_item=memory_items_by_ref.get(("derived_text", s["derived_text_id"])),
                    source_kind="derived_text",
                ),
            )
            for s, score_details, provenance, source_availability, source_checks in ranked_artifacts
        ]
    )
    for item in artifact_refs:
        item.qualification_reasons = _qualification_reasons(
            freshness_state=item.freshness_state,
            durable_status=item.durable_status or (item.provenance.status if item.provenance else None),
        )
        if item.qualification_reasons:
            truth_debug["derived_degraded_count"] += 1
            truth_debug["lifecycle_restricted_derived_count"] += 1
    truth_debug["derivative_state_counts"] = _state_counts_for_items(artifact_refs)

    allowed_domain_set = set(_normalize_domain_values(allowed_memory_domains or []))
    blocked_domain_set = set(_normalize_domain_values(blocked_memory_domains or []))
    domain_filter_debug = {
        "tagged_records_evaluated": 0,
        "tagged_records_filtered": 0,
        "untagged_records_not_domain_enforced": 0,
    }
    if containment_policy is None:
        recent_items = _filter_message_items(
            items=recent_items,
            allowed_domains=allowed_domain_set,
            blocked_domains=blocked_domain_set,
            debug_state=domain_filter_debug,
        )
        semantic_items = _filter_message_items(
            items=semantic_items,
            allowed_domains=allowed_domain_set,
            blocked_domains=blocked_domain_set,
            debug_state=domain_filter_debug,
        )
        artifact_refs = _filter_artifact_refs(
            items=artifact_refs,
            allowed_domains=allowed_domain_set,
            blocked_domains=blocked_domain_set,
            debug_state=domain_filter_debug,
        )
    policy_debug["retained_count"] = len(recent_items) + len(semantic_items) + len(artifact_refs)
    truth_debug["canonical_result_count"] = len(recent_items) + len(semantic_items)
    truth_debug["derived_result_count"] = len(artifact_refs)
    all_content = "".join(
        [s.content for s in recent_items]
        + [s.content for s in semantic_items]
        + [s.snippet for s in artifact_refs]
    )
    has_code_like_content = any(tok in all_content for tok in ("```", "def ", "class ", "import ", "{", "};"))
    token_estimate_total = max(1, len(all_content) // 4) if all_content else None

    return RetrievalBundle(
        recent=recent_items,
        semantic=semantic_items,
        artifact_refs=artifact_refs,
        token_estimate_total=token_estimate_total,
        observed_metadata=ObservedMetadata(
            mime_types=["text/plain"] if artifact_refs else [],
            has_artifacts=bool(artifact_refs),
            has_code_like_content=has_code_like_content,
            estimated_chars=len(all_content),
        ),
        retrieval_debug={
            **message_results["retrieval_debug"],
            "retrieval_contract_mode": "augmented" if include_artifacts else "raw",
            "contract_version": "raw-retrieval-debug.v1",
            "artifacts_included": include_artifacts,
            "artifact_candidates": len(artifact_snips),
            "artifact_ranked": len(ranked_artifacts),
            "artifact_status": artifact_status,
            "artifact_invalid_hit_ids": artifact_invalid_hit_ids,
            "artifact_missing_source_count": artifact_missing_source_count,
            "malformed_artifact_provenance_count": malformed_artifact_provenance_count,
            "cross_owner_artifact_provenance_count": cross_owner_artifact_provenance_count,
            "artifact_omission_reasons": artifact_omission_reasons,
            "truth_qualification": {
                **truth_debug,
                "vector_retrieval_status": message_results["retrieval_debug"]["vector_status"],
                "derivative_retrieval_status": artifact_status,
            },
            "graph_expansion_applied": False,
            "pinned_handling": "pinned memories are not part of the v2 ranked bundle; they remain available via the unchanged tiered retrieval path",
            "missing_score_note": "project heuristic; not an explicit spec term",
            "domain_filters_requested": bool(allowed_domain_set or blocked_domain_set),
            "allowed_memory_domains": sorted(allowed_domain_set),
            "blocked_memory_domains": sorted(blocked_domain_set),
            "tagged_domain_enforcement_applied": bool(
                (allowed_domain_set or blocked_domain_set)
                and domain_filter_debug["tagged_records_evaluated"] > 0
            ),
            "domain_enforcement_mode": "tagged_records_only",
            "containment_policy": {
                "enforcement_mode": policy_debug["enforcement_mode"],
                "policy_supplied": policy_debug["policy_supplied"],
                "pre_limit_policy_filter_applied": policy_debug["pre_limit_policy_filter_applied"],
                "post_fetch_validation_count": policy_debug["post_fetch_validation_count"],
                "retained_count": policy_debug["retained_count"],
                "omitted_counts_by_reason": policy_debug["omitted_counts_by_reason"],
                "relationship_narrowing_applied": policy_debug["relationship_narrowing_applied"],
                "artifact_search_skipped_reason": policy_debug["artifact_search_skipped_reason"],
            },
            **domain_filter_debug,
        },
    )


def _diagnostic_reason_codes(debug: dict[str, Any]) -> list[str]:
    codes: list[str] = []
    containment = debug.get("containment_policy") if isinstance(debug.get("containment_policy"), dict) else {}
    if containment.get("enforcement_mode") == "mandatory":
        codes.append("mandatory_containment_applied")
    else:
        codes.append("legacy_retrieval_policy")
    for reason, count in (containment.get("omitted_counts_by_reason") or {}).items():
        if int(count or 0) > 0:
            codes.append(str(reason)[:80])
    truth = debug.get("truth_qualification") if isinstance(debug.get("truth_qualification"), dict) else {}
    if truth.get("canonical_result_count", 0) > 0:
        codes.append("canonical_evidence_used")
    if truth.get("derived_result_count", 0) > 0:
        codes.append("derivative_augmentation_used")
    if truth.get("source_missing_count", 0) > 0:
        codes.append("source_missing_or_unavailable")
    if truth.get("source_unavailable_count", 0) > 0:
        codes.append("source_missing_or_unavailable")
    if truth.get("source_malformed_count", 0) > 0:
        codes.append("provenance_missing_or_invalid")
    if truth.get("source_owner_mismatch_count", 0) > 0:
        codes.append("validation_violation")
    omissions = truth.get("derivative_omissions_by_reason") or {}
    for reason in omissions:
        if reason in {"missing_derivative_source_refs", "malformed_derivative_provenance", "malformed_derivative_source_ref"}:
            codes.append("provenance_missing_or_invalid")
        elif reason == "cross_owner_derivative_source_ref":
            codes.append("validation_violation")
        elif "missing" in reason or "unavailable" in reason:
            codes.append("source_missing_or_unavailable")
    if truth.get("derived_degraded_count", 0) > 0:
        codes.append("derivative_ineligible")
    state_counts = truth.get("derivative_state_counts") if isinstance(truth.get("derivative_state_counts"), dict) else {}
    for state, count in state_counts.items():
        if int(count or 0) > 0:
            codes.append(f"derivative_{state}")
    if truth.get("canonical_fallback_reasons"):
        codes.append("fallback_to_raw")
    if debug.get("vector_status") == "unavailable" or debug.get("artifact_status") == "unavailable":
        codes.append("advanced_dependency_unavailable")
    if debug.get("artifact_status") in {"degraded", "unavailable"}:
        codes.append("validation_violation")
    return list(dict.fromkeys(codes))


def doctrine_diagnostics_for_bundle(
    *,
    request_id: str,
    conversation_id: str,
    owner_id: str,
    mode: str,
    bundle: RetrievalBundle | None = None,
    raw_bundle: RetrievalBundle | None = None,
    augmented_bundle: RetrievalBundle | None = None,
    comparison: dict[str, Any] | None = None,
    status: str = "ok",
    error: str | None = None,
) -> dict[str, Any]:
    selected = augmented_bundle or bundle or raw_bundle
    debug = selected.retrieval_debug if selected is not None else {}
    raw_ids = summarize_bundle(raw_bundle)["semantic_ids"] if raw_bundle is not None else []
    augmented_ids = summarize_bundle(augmented_bundle)["semantic_ids"] if augmented_bundle is not None else []
    selected_state_counts = (
        _state_counts_for_items([*selected.recent, *selected.semantic, *selected.artifact_refs])
        if selected is not None
        else _merge_state_counts()
    )
    diagnostics = {
        "contract_version": "raw-retrieval-debug.v1",
        "request_id": request_id,
        "conversation_id": conversation_id,
        "owner_id": owner_id,
        "mode": mode,
        "status": status,
        "retrieval_path": mode,
        "canonical_used": False,
        "derived_used": False,
        "reason_codes": [],
        "raw_result_ids": raw_ids,
        "augmented_result_ids": augmented_ids,
        "comparison": comparison or {},
        "fallback_to_raw": False,
        "fallback_reasons": [],
        "provenance_summary": {},
        "validation": {},
    }
    if selected is not None:
        truth = debug.get("truth_qualification") if isinstance(debug.get("truth_qualification"), dict) else {}
        diagnostics.update(
            {
                "canonical_used": truth.get("canonical_result_count", 0) > 0,
                "derived_used": truth.get("derived_result_count", 0) > 0,
                "fallback_to_raw": bool(truth.get("canonical_fallback_reasons")),
                "fallback_reasons": list(truth.get("canonical_fallback_reasons") or []),
                "provenance_summary": {
                    "derivative_source_checks_attempted": truth.get("derivative_source_checks_attempted", 0),
                    "source_available_count": truth.get("source_available_count", 0),
                    "source_missing_count": truth.get("source_missing_count", 0),
                    "source_malformed_count": truth.get("source_malformed_count", 0),
                    "source_unavailable_count": truth.get("source_unavailable_count", 0),
                    "source_owner_mismatch_count": truth.get("source_owner_mismatch_count", 0),
                    "derivative_omissions_by_reason": truth.get("derivative_omissions_by_reason", {}),
                },
                "validation": {
                    "vector_retrieval_status": truth.get("vector_retrieval_status", debug.get("vector_status")),
                    "derivative_retrieval_status": truth.get("derivative_retrieval_status", debug.get("artifact_status")),
                    "derived_degraded_count": truth.get("derived_degraded_count", 0),
                    "lifecycle_restricted_derived_count": truth.get("lifecycle_restricted_derived_count", 0),
                    "derivative_state_counts": truth.get("derivative_state_counts", selected_state_counts),
                    "artifact_omission_reasons": list(debug.get("artifact_omission_reasons") or []),
                },
            }
        )
        diagnostics["reason_codes"] = _diagnostic_reason_codes(debug)
    if mode == "compare":
        diagnostics["reason_codes"] = list(
            dict.fromkeys([*diagnostics["reason_codes"], "compare_mode_requested", "compare_mode_completed"])
        )
    if error:
        diagnostics["error"] = error[:160]
        diagnostics["reason_codes"] = list(dict.fromkeys([*diagnostics["reason_codes"], "retrieval_failed"]))
    return diagnostics


async def build_retrieval_response_payload(
    *,
    pg: Any,
    qdrant: Any,
    settings: Any,
    request_id: str,
    owner_id: str,
    conversation_id: UUID,
    client_id: str | None,
    query: str,
    opts: RetrievalOptions,
    mode: str,
    include_artifacts: bool,
    allowed_memory_domains: list[str] | None = None,
    blocked_memory_domains: list[str] | None = None,
    containment_policy: RetrievalContainmentPolicy | None = None,
) -> dict[str, Any]:
    normalized_query = query.strip()
    normalization_applied = normalized_query != query
    if mode == "raw":
        bundle = await build_retrieval_bundle(
            pg=pg,
            qdrant=qdrant,
            settings=settings,
            owner_id=owner_id,
            conversation_id=conversation_id,
            client_id=client_id,
            query=normalized_query,
            opts=opts,
            include_artifacts=False,
            allowed_memory_domains=allowed_memory_domains,
            blocked_memory_domains=blocked_memory_domains,
            containment_policy=containment_policy,
        )
        diagnostics = doctrine_diagnostics_for_bundle(
            request_id=request_id,
            conversation_id=str(conversation_id),
            owner_id=owner_id,
            mode=mode,
            bundle=bundle,
        )
        return {"bundle": bundle, "diagnostics": diagnostics}

    if mode == "compare":
        raw_bundle = await build_retrieval_bundle(
            pg=pg,
            qdrant=qdrant,
            settings=settings,
            owner_id=owner_id,
            conversation_id=conversation_id,
            client_id=client_id,
            query=normalized_query,
            opts=opts,
            include_artifacts=False,
            allowed_memory_domains=allowed_memory_domains,
            blocked_memory_domains=blocked_memory_domains,
            containment_policy=containment_policy,
        )
        raw_summary = summarize_bundle(raw_bundle)
        augmented_bundle: RetrievalBundle | None = None
        augmented_failure_reason: str | None = None
        try:
            augmented_bundle = await build_retrieval_bundle(
                pg=pg,
                qdrant=qdrant,
                settings=settings,
                owner_id=owner_id,
                conversation_id=conversation_id,
                client_id=client_id,
                query=normalized_query,
                opts=opts,
                include_artifacts=include_artifacts,
                allowed_memory_domains=allowed_memory_domains,
                blocked_memory_domains=blocked_memory_domains,
                containment_policy=containment_policy,
            )
            augmented_summary = summarize_bundle(augmented_bundle)
            comparison = compare_bundle_summaries(raw_summary, augmented_summary)
            status = "ok"
        except Exception:
            augmented_failure_reason = "augmented_retrieval_failed"
            augmented_summary = None
            comparison = {
                "contract_version": "raw-retrieval-debug.v1",
                "same_semantic_order": False,
                "raw_only_semantic_ids": raw_summary["semantic_ids"],
                "augmented_only_semantic_ids": [],
                "raw_order": raw_summary["semantic_ids"],
                "augmented_order": [],
                "added": [],
                "removed": [
                    {"id": item, "result_type": "message", "reason_codes": ["augmented_unavailable"]}
                    for item in raw_summary["semantic_ids"]
                ],
                "moved": [],
                "rank_deltas": [],
                "artifact_delta": 0 - raw_summary["artifact_count"],
                "token_delta": 0 - (raw_summary.get("token_estimate_total") or 0),
            }
            status = "degraded"
        comparison.update(
            {
                "mode": "compare",
                "request_id": request_id,
                "conversation_id": str(conversation_id),
                "owner_id": owner_id,
                "shared_normalized_input": True,
                "normalization_applied": normalization_applied,
                "scope": opts.scope,
            }
        )
        diagnostics = doctrine_diagnostics_for_bundle(
            request_id=request_id,
            conversation_id=str(conversation_id),
            owner_id=owner_id,
            mode=mode,
            raw_bundle=raw_bundle,
            augmented_bundle=augmented_bundle,
            comparison=comparison,
            status=status,
        )
        if augmented_failure_reason:
            diagnostics["fallback_to_raw"] = True
            diagnostics["fallback_reasons"] = [augmented_failure_reason]
            diagnostics["reason_codes"] = list(
                dict.fromkeys([
                    *diagnostics.get("reason_codes", []),
                    "advanced_dependency_unavailable",
                    "augmented_retrieval_failed",
                    "fallback_to_raw",
                    "compare_mode_degraded",
                ])
            )
            diagnostics["validation"]["derivative_retrieval_status"] = "failed"
            diagnostics["validation"]["augmented_failure_reason"] = augmented_failure_reason
        return {
            "bundle": augmented_bundle or raw_bundle,
            "raw_bundle": raw_bundle,
            "augmented_bundle": augmented_bundle,
            "comparison": comparison,
            "diagnostics": diagnostics,
        }

    bundle = await build_retrieval_bundle(
        pg=pg,
        qdrant=qdrant,
        settings=settings,
        owner_id=owner_id,
        conversation_id=conversation_id,
        client_id=client_id,
        query=normalized_query,
        opts=opts,
        include_artifacts=include_artifacts,
        allowed_memory_domains=allowed_memory_domains,
        blocked_memory_domains=blocked_memory_domains,
        containment_policy=containment_policy,
    )
    diagnostics = doctrine_diagnostics_for_bundle(
        request_id=request_id,
        conversation_id=str(conversation_id),
        owner_id=owner_id,
        mode=mode,
        bundle=bundle,
    )
    return {"bundle": bundle, "diagnostics": diagnostics}
