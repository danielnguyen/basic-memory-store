from __future__ import annotations

from typing import Any


DURABLE_MEMORY_STATUSES = (
    "active",
    "parked",
    "stale",
    "contradicted",
    "corrected",
    "invalidated",
    "superseded",
    "expired",
    "retracted",
    "forgotten_or_demoted",
    "rebuilding",
)

DEMOTED_PROMOTION_STATES = {"demoted", "decayed"}


def effective_freshness_state(memory_item: dict[str, Any] | None) -> str:
    if not isinstance(memory_item, dict):
        return "unknown_freshness"

    status = str(memory_item.get("status") or "").strip().lower()
    promotion_state = str(memory_item.get("promotion_state") or "").strip().lower()
    superseded_by = memory_item.get("superseded_by_memory_id")

    if promotion_state in DEMOTED_PROMOTION_STATES:
        return "forgotten_or_demoted"
    if status == "contradicted":
        if memory_item.get("supersedes_memory_id") or superseded_by:
            return "corrected"
        return "forgotten_or_demoted"
    if superseded_by or status == "superseded":
        return "superseded"
    if status in {"active", "parked", "stale", "corrected"}:
        return status
    if status in {"invalidated", "retracted", "forgotten_or_demoted"}:
        return "forgotten_or_demoted"
    if status == "expired":
        return "stale"
    return "unknown_freshness"


def bounded_transition_reason(
    *,
    code: str,
    metadata: dict[str, Any],
    request_id: str,
    previous_status: str,
    new_status: str,
    related_memory_id: str | None,
) -> dict[str, Any]:
    bounded_metadata: dict[str, Any] = {}
    for key, value in list(metadata.items())[:8]:
        safe_key = str(key).strip()[:64]
        if not safe_key:
            continue
        if isinstance(value, bool) or value is None or isinstance(value, (int, float)):
            bounded_metadata[safe_key] = value
        elif isinstance(value, str):
            bounded_metadata[safe_key] = value[:160]

    reason: dict[str, Any] = {
        "request_id": request_id,
        "reason_code": code,
        "previous_status": previous_status,
        "new_status": new_status,
    }
    if bounded_metadata:
        reason["reason_metadata"] = bounded_metadata
    if related_memory_id:
        reason["related_memory_id"] = related_memory_id
    return reason
