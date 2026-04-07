from __future__ import annotations

import logging
import math
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from models import ArtifactRef, ObservedMetadata, RetrievalBundle, RetrievalMessageItem, RetrievalOptions


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

    conversation_boost = float(getattr(settings, "retrieval_conversation_boost", 0.08)) if is_same_conversation else 0.0
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

    semantic_hits = await qdrant.search(
        owner_id=owner_id,
        query=query,
        k=opts.k,
        min_score=opts.min_score,
        conversation_id=conversation_filter,
        client_id=client_filter,
        exclude_message_ids=exclude_message_ids,
    )
    semantic_ids: list[UUID] = []
    semantic_score_by_id: dict[str, float] = {}
    for hit in semantic_hits:
        message_id = _safe_uuid(getattr(hit, "message_id", None), context=context)
        if message_id is None:
            continue
        semantic_ids.append(message_id)
        semantic_score_by_id[str(message_id)] = float(getattr(hit, "score", 0.0) or 0.0)
    semantic_snips = await pg.get_message_snippets_by_ids(semantic_ids)

    ranked_semantic: list[tuple[dict[str, Any], dict[str, float]]] = []
    for snippet in semantic_snips:
        if not _in_time_window(snippet.get("created_at"), opts.time_window):
            continue
        score_details = _score_item(
            settings=settings,
            semantic_score=semantic_score_by_id.get(snippet["message_id"]),
            created_at=snippet.get("created_at"),
            retrieval_mode=opts.retrieval_mode,
            is_same_conversation=(conversation_id is not None and snippet.get("conversation_id") == str(conversation_id)),
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

    artifact_k = retrieval_artifact_k(settings)
    artifact_hits = await qdrant.search_artifact_chunks(
        owner_id=owner_id,
        query=query,
        k=artifact_k,
        min_score=opts.min_score,
        client_id=(client_id if opts.scope == "client" else None),
    ) if artifact_k > 0 else []

    artifact_ids: list[UUID] = []
    artifact_score_by_id: dict[str, float] = {}
    for hit in artifact_hits:
        derived_text_id = _safe_uuid(getattr(hit, "derived_text_id", None), context="retrieve_bundle_artifacts")
        if derived_text_id is None:
            continue
        artifact_ids.append(derived_text_id)
        artifact_score_by_id[str(derived_text_id)] = float(getattr(hit, "score", 0.0) or 0.0)

    artifact_snips = await pg.get_derived_text_snippets_by_ids(artifact_ids)
    ranked_artifacts: list[tuple[dict[str, Any], dict[str, float]]] = []
    for snippet in artifact_snips:
        if not _in_time_window(snippet.get("created_at"), opts.time_window):
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
        ranked_artifacts.append((snippet, score_details))
    ranked_artifacts.sort(key=lambda item: item[1]["final_score"], reverse=True)
    ranked_artifacts = ranked_artifacts[:artifact_k]

    recent_snips = await pg.get_recent_message_items(conversation_id=conversation_id, limit=getattr(settings, "recent_turns", 10))
    all_content = "".join(
        [s["content"] for s in recent_snips]
        + [s["content"] for s, _ in ranked_semantic]
        + [s["text"] for s, _ in ranked_artifacts]
    )
    has_code_like_content = any(tok in all_content for tok in ("```", "def ", "class ", "import ", "{", "};"))
    token_estimate_total = max(1, len(all_content) // 4) if all_content else None

    artifact_refs = _dedupe_artifact_refs(
        [
            ArtifactRef(
                artifact_id=s["artifact_id"],
                file_path=s["file_path"],
                snippet=cap_snippet(s["text"], retrieval_artifact_max_snippet_chars(settings)),
                relevance_score=score_details["final_score"],
                repo_name=s.get("repo_name"),
                score_details=score_details,
            )
            for s, score_details in ranked_artifacts
        ]
    )

    return RetrievalBundle(
        recent=[
            RetrievalMessageItem(
                message_id=s["message_id"],
                conversation_id=s["conversation_id"],
                role=s["role"],
                content=s["content"],
                created_at=s["created_at"],
                score=None,
            )
            for s in recent_snips
        ],
        semantic=[
            RetrievalMessageItem(
                message_id=s["message_id"],
                conversation_id=s["conversation_id"],
                role=s["role"],
                content=s["content"],
                created_at=s["created_at"],
                score=score_details["final_score"],
                score_details=score_details,
            )
            for s, score_details in ranked_semantic
        ],
        artifact_refs=artifact_refs,
        token_estimate_total=token_estimate_total,
        observed_metadata=ObservedMetadata(
            mime_types=["text/plain"] if ranked_artifacts else [],
            has_artifacts=bool(ranked_artifacts),
            has_code_like_content=has_code_like_content,
            estimated_chars=len(all_content),
        ),
        retrieval_debug={
            **message_results["retrieval_debug"],
            "artifact_candidates": len(artifact_snips),
            "artifact_ranked": len(ranked_artifacts),
            "graph_expansion_applied": False,
            "pinned_handling": "pinned memories are not part of the v2 ranked bundle; they remain available via the unchanged tiered retrieval path",
            "missing_score_note": "project heuristic; not an explicit spec term",
        },
    )
