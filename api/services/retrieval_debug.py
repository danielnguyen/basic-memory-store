from __future__ import annotations

from typing import Any

from models import RetrievalBundle


def _source_ref(item: Any) -> dict[str, str] | None:
    value = item.get("source_ref") if isinstance(item, dict) else getattr(item, "source_ref", None)
    if value is None:
        return None
    if hasattr(value, "model_dump"):
        value = value.model_dump()
    if not isinstance(value, dict):
        return None
    ref_type = value.get("ref_type")
    ref_id = value.get("ref_id")
    if ref_type is None or ref_id is None:
        return None
    return {"ref_type": str(ref_type), "ref_id": str(ref_id)}


def _safe_item_summary(item: Any, *, id_key: str, score_key: str) -> dict[str, Any]:
    value = item if isinstance(item, dict) else item.model_dump()
    summary = {
        "id": str(value[id_key]),
        "owner_id": value.get("owner_id"),
        "evidence_role": value.get("evidence_role"),
        "source_ref": _source_ref(value),
        "source_availability": value.get("source_availability"),
        "source_checks": value.get("source_checks") or [],
        "score": value.get(score_key),
        "score_details": value.get("score_details") or {},
        "freshness_state": value.get("freshness_state"),
        "durable_status": value.get("durable_status"),
        "source_kind": value.get("source_kind"),
        "confidence": value.get("confidence"),
        "supersedes": value.get("supersedes"),
        "superseded_by": value.get("superseded_by"),
        "qualification_reasons": value.get("qualification_reasons") or [],
    }
    return {key: item_value for key, item_value in summary.items() if item_value is not None}


def summarize_bundle(bundle: RetrievalBundle) -> dict[str, Any]:
    recent = [_safe_item_summary(item, id_key="message_id", score_key="score") for item in bundle.recent]
    semantic = [_safe_item_summary(item, id_key="message_id", score_key="score") for item in bundle.semantic]
    artifacts = [
        _safe_item_summary(item, id_key="artifact_id", score_key="relevance_score")
        for item in bundle.artifact_refs
    ]
    source_refs = [
        item["source_ref"]
        for item in [*recent, *semantic, *artifacts]
        if item["source_ref"] is not None
    ]
    return {
        "recent_ids": [item["id"] for item in recent],
        "semantic_ids": [item["id"] for item in semantic],
        "artifact_ids": [item["id"] for item in artifacts],
        "recent": recent,
        "semantic": semantic,
        "artifacts": artifacts,
        "semantic_count": len(bundle.semantic),
        "artifact_count": len(bundle.artifact_refs),
        "source_refs": source_refs,
        "token_estimate_total": bundle.token_estimate_total,
        "retrieval_debug": bundle.retrieval_debug,
    }


def compare_bundle_summaries(raw: dict[str, Any], augmented: dict[str, Any]) -> dict[str, Any]:
    raw_semantic = raw["semantic_ids"]
    augmented_semantic = augmented["semantic_ids"]
    raw_artifacts = raw["artifact_ids"]
    augmented_artifacts = augmented["artifact_ids"]
    rank_deltas = []
    for item_id in raw_semantic:
        if item_id in augmented_semantic:
            raw_rank = raw_semantic.index(item_id)
            augmented_rank = augmented_semantic.index(item_id)
            if raw_rank != augmented_rank:
                rank_deltas.append(
                    {
                        "id": item_id,
                        "result_type": "message",
                        "raw_rank": raw_rank,
                        "augmented_rank": augmented_rank,
                        "delta": augmented_rank - raw_rank,
                        "reason_codes": ["ranking_adjustment_applied"],
                    }
                )
    return {
        "contract_version": "raw-retrieval-debug.v1",
        "same_semantic_order": raw_semantic == augmented_semantic,
        "raw_only_semantic_ids": [item for item in raw_semantic if item not in augmented_semantic],
        "augmented_only_semantic_ids": [item for item in augmented_semantic if item not in raw_semantic],
        "raw_order": raw_semantic,
        "augmented_order": augmented_semantic,
        "added": [
            {"id": item, "result_type": "message", "reason_codes": ["augmented_inclusion"]}
            for item in augmented_semantic
            if item not in raw_semantic
        ] + [
            {"id": item, "result_type": "artifact", "reason_codes": ["derivative_augmentation_used"]}
            for item in augmented_artifacts
            if item not in raw_artifacts
        ],
        "removed": [
            {"id": item, "result_type": "message", "reason_codes": ["raw_only_inclusion"]}
            for item in raw_semantic
            if item not in augmented_semantic
        ],
        "moved": rank_deltas,
        "rank_deltas": rank_deltas,
        "artifact_delta": augmented["artifact_count"] - raw["artifact_count"],
        "token_delta": (augmented.get("token_estimate_total") or 0) - (raw.get("token_estimate_total") or 0),
    }
