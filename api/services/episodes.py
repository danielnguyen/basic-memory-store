from __future__ import annotations

from hashlib import sha256
import json
from typing import Any

from services.derivation_versions import EPISODE_DERIVATION_VERSION
from services.memory_items import normalize_source_refs, source_ref_hash


DEFAULT_DERIVATION_VERSION = EPISODE_DERIVATION_VERSION


def _compact_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def normalize_json_map(value: dict[str, Any] | None) -> dict[str, Any]:
    if not value:
        return {}
    return json.loads(_compact_json(value))


def normalize_json_list(value: list[Any] | None) -> list[Any]:
    if not value:
        return []
    return json.loads(_compact_json(value))


def episode_key(
    *,
    episode_type: str,
    source_ref_hash_value: str,
    trigger_json: dict[str, Any] | None,
    time_window_json: dict[str, Any] | None,
) -> str:
    material = {
        "episode_type": str(episode_type).strip(),
        "source_ref_hash": source_ref_hash_value,
        "trigger_json": normalize_json_map(trigger_json),
        "time_window_json": normalize_json_map(time_window_json),
    }
    return sha256(_compact_json(material).encode("utf-8")).hexdigest()


def episode_changed(existing: dict[str, Any], incoming: dict[str, Any]) -> bool:
    comparable_fields = (
        "title",
        "summary",
        "outcome",
        "significance",
        "unresolved_json",
        "callback_candidates_json",
        "participants_json",
        "confidence",
        "explanation_json",
        "generation_trace_id",
    )
    for field in comparable_fields:
        if existing.get(field) != incoming.get(field):
            return True
    return False


def shape_episode(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "episode_id": row["episode_id"],
        "owner_id": row["owner_id"],
        "title": row["title"],
        "summary": row["summary"],
        "episode_type": row["episode_type"],
        "trigger": row.get("trigger_json") or {},
        "outcome": row.get("outcome"),
        "significance": row.get("significance"),
        "unresolved": row.get("unresolved_json") or {},
        "source_refs": row.get("source_refs_json") or [],
        "source_ref_hash": row["source_ref_hash"],
        "episode_key": row["episode_key"],
        "callback_candidates": row.get("callback_candidates_json") or [],
        "time_window": row.get("time_window_json") or {},
        "participants": row.get("participants_json") or [],
        "status": row["status"],
        "derivation_version": row.get("derivation_version") or DEFAULT_DERIVATION_VERSION,
        "confidence": row.get("confidence"),
        "explanation": row.get("explanation_json") or {},
        "generation_trace_id": row.get("generation_trace_id"),
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def shape_episode_link(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "link_id": row["link_id"],
        "episode_id": row["episode_id"],
        "owner_id": row["owner_id"],
        "ref_type": row["ref_type"],
        "ref_id": row["ref_id"],
        "relationship": row["relationship"],
        "created_at": row["created_at"],
    }


def shape_episode_event(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "event_id": row["event_id"],
        "episode_id": row["episode_id"],
        "owner_id": row["owner_id"],
        "event_type": row["event_type"],
        "reason": row.get("reason_json") or {},
        "created_at": row["created_at"],
    }


__all__ = [
    "DEFAULT_DERIVATION_VERSION",
    "episode_changed",
    "episode_key",
    "normalize_json_list",
    "normalize_json_map",
    "normalize_source_refs",
    "shape_episode",
    "shape_episode_event",
    "shape_episode_link",
    "source_ref_hash",
]
