from __future__ import annotations

import json
from typing import Any


VALID_CANDIDATE_TYPES = ("memory_item", "episode", "message", "artifact", "event", "derived_text")
VALID_DECISIONS = ("mention", "suppress", "implicit_only")
VALID_MENTION_STRATEGIES = ("none", "implicit", "light_callback", "explicit_callback")


def _compact_json(value: Any) -> Any:
    return json.loads(json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True))


def normalize_source_refs(source_refs: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    if not source_refs:
        return []
    normalized: list[dict[str, Any]] = []
    for ref in source_refs:
        item = {
            "ref_type": str(ref["ref_type"]).strip(),
            "ref_id": str(ref["ref_id"]).strip(),
            "support_kind": str(ref.get("support_kind") or "direct").strip(),
        }
        metadata = ref.get("metadata")
        if isinstance(metadata, dict) and metadata:
            item["metadata"] = _compact_json(metadata)
        normalized.append(item)
    return sorted(
        normalized,
        key=lambda item: (
            item["ref_type"],
            item["ref_id"],
            item["support_kind"],
            json.dumps(item, sort_keys=True, separators=(",", ":"), ensure_ascii=True),
        ),
    )


def normalize_metadata(metadata: dict[str, Any] | None) -> dict[str, Any]:
    if not metadata:
        return {}
    return _compact_json(metadata)


def normalize_score(value: float | None) -> float:
    if value is None:
        return 0.0
    return max(0.0, min(1.0, float(value)))


def candidate_ref(candidate: dict[str, Any]) -> dict[str, Any]:
    ref = {
        "candidate_id": str(candidate["candidate_id"]),
        "candidate_type": str(candidate["candidate_type"]),
    }
    if candidate.get("title") is not None:
        ref["title"] = str(candidate["title"])
    if candidate.get("summary") is not None:
        ref["summary"] = str(candidate["summary"])
    metadata = normalize_metadata(candidate.get("metadata"))
    if metadata:
        ref["metadata"] = metadata
    return ref


def select_recall_decision(*, context: dict[str, Any], candidate: dict[str, Any]) -> dict[str, Any]:
    relevance = normalize_score(candidate.get("relevance_score"))
    salience = normalize_score(candidate.get("salience_score"))
    recency = normalize_score(candidate.get("recency_score"))
    mentionability_score = round((0.60 * relevance) + (0.25 * salience) + (0.15 * recency), 4)

    sensitivity = str(context.get("sensitivity") or "low").strip().lower()
    urgency = str(context.get("urgency") or "medium").strip().lower()
    surface = str(context.get("surface") or "").strip().lower()
    metadata = normalize_metadata(candidate.get("metadata"))
    blocked_surfaces = metadata.get("blocked_surfaces") if isinstance(metadata.get("blocked_surfaces"), list) else []

    decision = "implicit_only"
    mention_strategy = "implicit"
    rule_id = "implicit_default"

    if relevance < 0.35:
        decision = "suppress"
        mention_strategy = "none"
        rule_id = "below_relevance_threshold"
    elif surface and surface in {str(item).strip().lower() for item in blocked_surfaces}:
        decision = "suppress"
        mention_strategy = "none"
        rule_id = "surface_blocked"
    elif sensitivity == "high":
        if metadata.get("allow_sensitive_mention") is True:
            decision = "implicit_only"
            mention_strategy = "implicit"
            rule_id = "high_sensitivity_implicit_cap"
        else:
            decision = "suppress"
            mention_strategy = "none"
            rule_id = "high_sensitivity_suppression"
    elif sensitivity == "medium" or (urgency == "high" and relevance < 0.90):
        decision = "implicit_only"
        mention_strategy = "implicit"
        rule_id = "sensitive_context_implicit_only" if sensitivity == "medium" else "urgent_context_implicit_only"
    elif mentionability_score >= 0.82 and urgency != "high" and metadata.get("explicit_callback_allowed") is True:
        decision = "mention"
        mention_strategy = "explicit_callback"
        rule_id = "explicit_callback_allowed"
    elif mentionability_score >= 0.60:
        decision = "mention"
        mention_strategy = "light_callback"
        rule_id = "light_callback_allowed"

    prompt_eligible = decision == "mention" and mention_strategy in {"light_callback", "explicit_callback"}
    return {
        "candidate_id": str(candidate["candidate_id"]),
        "candidate_type": str(candidate["candidate_type"]),
        "candidate_ref_json": candidate_ref(candidate),
        "source_refs_json": normalize_source_refs(candidate.get("source_refs")),
        "scene_id": context.get("scene_id"),
        "surface": context.get("surface"),
        "urgency": context.get("urgency"),
        "sensitivity": context.get("sensitivity"),
        "relevance_score": relevance,
        "salience_score": salience,
        "recency_score": recency,
        "mentionability_score": mentionability_score,
        "decision": decision,
        "mention_strategy": mention_strategy,
        "prompt_eligible": prompt_eligible,
        "reason_json": {
            "rule_id": rule_id,
            "score_formula": "0.60*relevance + 0.25*salience + 0.15*recency",
            "thresholds": {
                "minimum_relevance": 0.35,
                "light_callback": 0.60,
                "explicit_callback": 0.82,
            },
        },
    }


def shape_recall_decision(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": row["id"],
        "request_id": row["request_id"],
        "owner_id": row["owner_id"],
        "candidate_id": row["candidate_id"],
        "candidate_type": row["candidate_type"],
        "candidate_ref": row.get("candidate_ref_json") or {},
        "source_refs": row.get("source_refs_json") or [],
        "context": {
            "scene_id": row.get("scene_id"),
            "surface": row.get("surface"),
            "urgency": row.get("urgency"),
            "sensitivity": row.get("sensitivity"),
        },
        "relevance_score": row.get("relevance_score"),
        "salience_score": row.get("salience_score"),
        "recency_score": row.get("recency_score"),
        "mentionability_score": row["mentionability_score"],
        "decision": row["decision"],
        "mention_strategy": row["mention_strategy"],
        "prompt_eligible": row["prompt_eligible"],
        "reason": row.get("reason_json") or {},
        "created_at": row["created_at"],
    }
