from __future__ import annotations

import logging
import math
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from models import (
    ArtifactRef,
    DerivedProvenance,
    ObservedMetadata,
    RetrievalBundle,
    RetrievalMessageItem,
    RetrievalOptions,
    RetrievalPolicyMetadata,
    RetrievalSourceRef,
)
from services.memory_lifecycle import effective_freshness_state
from services.derived_contract import derived_text_contract_view


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

    return RetrievalPolicyMetadata(memory_domains=domains, sensitivity=sensitivity)


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
) -> RetrievalBundle:
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
    )
    ranked_semantic = message_results["ranked_semantic"]

    artifact_k = retrieval_artifact_k(settings) if include_artifacts else 0
    artifact_status = "not_requested" if artifact_k == 0 else "ok"
    artifact_omission_reasons: list[str] = []
    if artifact_k > 0:
        try:
            artifact_hits = await qdrant.search_artifact_chunks(
                owner_id=owner_id,
                query=query,
                k=artifact_k,
                min_score=opts.min_score,
                client_id=(client_id if opts.scope == "client" else None),
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

    recent_snips = await pg.get_recent_message_items(
        conversation_id=conversation_id,
        limit=getattr(settings, "recent_turns", 10),
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
            policy_metadata=_policy_metadata(s.get("metadata")),
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
            policy_metadata=_policy_metadata(s.get("metadata")),
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
                policy_metadata=_policy_metadata(s.get("derivation_params")),
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

    allowed_domain_set = set(_normalize_domain_values(allowed_memory_domains or []))
    blocked_domain_set = set(_normalize_domain_values(blocked_memory_domains or []))
    domain_filter_debug = {
        "tagged_records_evaluated": 0,
        "tagged_records_filtered": 0,
        "untagged_records_not_domain_enforced": 0,
    }
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
            **domain_filter_debug,
        },
    )
