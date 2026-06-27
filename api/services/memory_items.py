from __future__ import annotations

from hashlib import sha256
import json
from typing import Any

from services.memory_lifecycle import effective_freshness_state
from services.derived_contract import normalize_contract_source_refs
from services.derivation_versions import MEMORY_ITEM_DERIVATION_VERSION

DEFAULT_DERIVATION_VERSION = MEMORY_ITEM_DERIVATION_VERSION


def _compact_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def normalize_source_refs(source_refs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return normalize_contract_source_refs(source_refs)


def source_ref_hash(source_refs: list[dict[str, Any]]) -> str:
    return sha256(_compact_json(normalize_source_refs(source_refs)).encode("utf-8")).hexdigest()


def normalize_scores(scores: dict[str, Any] | None) -> dict[str, Any]:
    if not scores:
        return {}
    return {str(k): v for k, v in sorted(scores.items(), key=lambda item: str(item[0]))}


def memory_item_changed(existing: dict[str, Any], incoming: dict[str, Any]) -> bool:
    comparable_fields = (
        "memory_type",
        "summary",
        "source_refs_json",
        "scores_json",
        "promotion_state",
        "expires_at",
        "confidence",
        "explanation_json",
        "generation_trace_id",
    )
    for field in comparable_fields:
        if existing.get(field) != incoming.get(field):
            return True
    return False


def shape_memory_item(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "memory_id": row["memory_id"],
        "owner_id": row["owner_id"],
        "memory_type": row["memory_type"],
        "summary": row["summary"],
        "source_refs": row.get("source_refs_json") or [],
        "source_ref_hash": row["source_ref_hash"],
        "scores": row.get("scores_json") or {},
        "promotion_state": row["promotion_state"],
        "status": row["status"],
        "freshness_state": effective_freshness_state(row),
        "supersedes_memory_id": row.get("supersedes_memory_id"),
        "superseded_by_memory_id": row.get("superseded_by_memory_id"),
        "last_reinforced_at": row.get("last_reinforced_at"),
        "expires_at": row.get("expires_at"),
        "derivation_version": row.get("derivation_version") or DEFAULT_DERIVATION_VERSION,
        "confidence": row.get("confidence"),
        "explanation": row.get("explanation_json") or {},
        "generation_trace_id": row.get("generation_trace_id"),
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def shape_memory_event(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "event_id": row["event_id"],
        "memory_id": row["memory_id"],
        "owner_id": row["owner_id"],
        "event_type": row["event_type"],
        "reason": row.get("reason_json") or {},
        "created_at": row["created_at"],
    }
