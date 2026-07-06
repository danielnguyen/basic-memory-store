from __future__ import annotations

from hashlib import sha256
import re
from typing import Any

from services.episodes import episode_key, normalize_json_map, normalize_source_refs, source_ref_hash


MAX_DECISIONS = 12
MAX_REASON_LEN = 80
MAX_EXCERPT_LEN = 240


INCIDENT_RULES: tuple[dict[str, Any], ...] = (
    {
        "episode_type": "project_milestone_completed",
        "title": "Project milestone completed",
        "keywords": ("completed", "landed", "merged", "shipped", "ready", "milestone"),
        "evidence_terms": ("pr", "phase", "wave", "cluster", "milestone", "merged", "completed"),
        "significance": "Preserves a completed project milestone for later continuity.",
    },
    {
        "episode_type": "planning_decision_reversed",
        "title": "Planning decision reversed",
        "keywords": ("reverse", "reversed", "instead", "switch", "changed plan", "no longer"),
        "evidence_terms": ("plan", "decision", "approach", "strategy", "scope"),
        "significance": "Records a planning reversal that may affect future work.",
    },
    {
        "episode_type": "repeated_issue_pattern",
        "title": "Repeated issue pattern identified",
        "keywords": ("again", "recurring", "repeated", "pattern", "keeps happening"),
        "evidence_terms": ("issue", "failure", "bug", "problem", "mistake"),
        "significance": "Captures a recurring pattern so later work can avoid repeating it.",
    },
    {
        "episode_type": "correction_future_relevance",
        "title": "Correction with future relevance",
        "keywords": ("correction", "corrected", "actually", "remember", "next time"),
        "evidence_terms": ("wrong", "instead", "should", "must", "use", "avoid"),
        "significance": "Preserves a correction likely to matter in future decisions.",
    },
    {
        "episode_type": "successful_mitigation",
        "title": "Successful mitigation",
        "keywords": ("mitigated", "fixed", "resolved", "recovered", "workaround", "unblocked"),
        "evidence_terms": ("failure", "incident", "bug", "blocked", "error", "risk"),
        "significance": "Records a mitigation that may be useful when similar issues recur.",
    },
    {
        "episode_type": "failure_worth_remembering",
        "title": "Failure worth remembering",
        "keywords": ("failed", "failure", "regression", "blocked", "broke", "outage"),
        "evidence_terms": ("because", "root cause", "lesson", "risk", "worth remembering"),
        "significance": "Records a failure with enough context to inform later work.",
    },
    {
        "episode_type": "recurring_workflow_established",
        "title": "Recurring workflow established",
        "keywords": ("workflow", "process", "always", "from now on", "every time", "repeatable"),
        "evidence_terms": ("run", "check", "review", "sync", "validate", "preflight"),
        "significance": "Captures a repeatable workflow that supports future execution.",
    },
    {
        "episode_type": "useful_lesson_extracted",
        "title": "Useful lesson extracted",
        "keywords": ("lesson", "learned", "takeaway", "avoid", "remember to", "next time"),
        "evidence_terms": ("because", "so that", "future", "repeat", "risk", "prevents"),
        "significance": "Keeps a useful lesson available for later retrieval.",
    },
)


LOW_VALUE_PATTERNS = (
    r"\bthanks?\b",
    r"\bthank you\b",
    r"\bnice\b",
    r"\bcool\b",
    r"\bok(?:ay)?\b",
    r"\bsounds good\b",
)


def _bounded_reason(reason: str) -> str:
    return reason.strip().replace("\n", " ")[:MAX_REASON_LEN]


def _compact_text(value: Any, *, limit: int = 400) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    return text[:limit]


def _source_text(item: dict[str, Any]) -> str:
    for key in ("content", "text", "summary", "title", "description", "event_text"):
        if item.get(key):
            return _compact_text(item[key], limit=1200)
    return ""


def _source_ref(item: dict[str, Any], fallback_index: int) -> dict[str, Any] | None:
    raw = item.get("source_ref")
    if isinstance(raw, dict):
        ref_type = str(raw.get("ref_type") or "").strip()
        ref_id = str(raw.get("ref_id") or "").strip()
        if ref_type and ref_id:
            return {
                "ref_type": ref_type[:64],
                "ref_id": ref_id[:160],
                "support_kind": str(raw.get("support_kind") or "direct")[:64],
                "metadata": raw.get("metadata") if isinstance(raw.get("metadata"), dict) else {},
            }
    ref_id = item.get("message_id") or item.get("event_id") or item.get("id")
    if ref_id:
        ref_type = "message" if item.get("role") or item.get("message_id") else "event"
        return {"ref_type": ref_type, "ref_id": str(ref_id)[:160], "support_kind": "direct", "metadata": {}}
    if item.get("allow_generated_source_ref") is True:
        return {"ref_type": "input_item", "ref_id": f"item-{fallback_index}", "support_kind": "direct", "metadata": {}}
    return None


def _is_low_value(text: str) -> bool:
    lowered = text.lower()
    if len(lowered) > 80:
        return False
    return any(re.search(pattern, lowered) for pattern in LOW_VALUE_PATTERNS)


def _rule_matches(rule: dict[str, Any], text: str) -> bool:
    lowered = text.lower()
    return any(term in lowered for term in rule["keywords"]) and any(term in lowered for term in rule["evidence_terms"])


def _decision_id(material: dict[str, Any]) -> str:
    compact = repr(sorted(material.items())).encode("utf-8")
    return sha256(compact).hexdigest()[:24]


def _summary_for(rule: dict[str, Any], text: str) -> str:
    first_sentence = re.split(r"(?<=[.!?])\s+", text.strip(), maxsplit=1)[0]
    if not first_sentence:
        first_sentence = rule["title"]
    return _compact_text(first_sentence, limit=320)


def extract_episode_decisions(payload: dict[str, Any]) -> list[dict[str, Any]]:
    owner_id = _compact_text(payload.get("owner_id"), limit=160)
    request_id = _compact_text(payload.get("request_id"), limit=160)
    conversation_id = _compact_text(payload.get("conversation_id"), limit=160) or None
    scene = normalize_json_map(payload.get("scene") if isinstance(payload.get("scene"), dict) else {})
    source_items = payload.get("source_items")
    if source_items is None:
        source_items = [*(payload.get("messages") or []), *(payload.get("events") or [])]
    if not owner_id or not request_id:
        return [
            {
                "decision_id": _decision_id({"request_id": request_id, "reason": "missing_owner_or_request"}),
                "decision": "reject",
                "episode_type": "unknown",
                "reasons": ["missing_owner_or_request"],
            }
        ]
    if not isinstance(source_items, list) or not source_items:
        return [
            {
                "decision_id": _decision_id({"owner_id": owner_id, "request_id": request_id, "reason": "missing_evidence"}),
                "decision": "defer",
                "episode_type": "unknown",
                "reasons": ["missing_evidence"],
            }
        ]

    decisions: list[dict[str, Any]] = []
    seen_keys: set[str] = set()
    for index, raw_item in enumerate(source_items[:MAX_DECISIONS]):
        if not isinstance(raw_item, dict):
            continue
        if raw_item.get("owner_id") not in (None, owner_id):
            decisions.append(
                {
                    "decision_id": _decision_id({"owner_id": owner_id, "index": index, "reason": "cross_owner_source"}),
                    "decision": "reject",
                    "episode_type": "unknown",
                    "reasons": ["cross_owner_source"],
                }
            )
            continue
        text = _source_text(raw_item)
        ref = _source_ref(raw_item, index)
        if raw_item.get("unsupported") is True or raw_item.get("evidence_supported") is False:
            decisions.append(
                {
                    "decision_id": _decision_id({"owner_id": owner_id, "index": index, "reason": "unsupported_claim"}),
                    "decision": "reject",
                    "episode_type": "unknown",
                    "reasons": ["unsupported_claim"],
                    "source_refs": [ref] if ref else [],
                }
            )
            continue
        if not text or ref is None:
            decisions.append(
                {
                    "decision_id": _decision_id({"owner_id": owner_id, "index": index, "reason": "missing_evidence"}),
                    "decision": "defer",
                    "episode_type": "unknown",
                    "reasons": ["missing_evidence"],
                    "source_refs": [ref] if ref else [],
                }
            )
            continue
        if _is_low_value(text):
            decisions.append(
                {
                    "decision_id": _decision_id({"owner_id": owner_id, "ref": ref, "reason": "low_value_generic_chat"}),
                    "decision": "reject",
                    "episode_type": "low_value_generic_chat",
                    "reasons": ["low_value_generic_chat"],
                    "source_refs": [ref],
                }
            )
            continue

        for rule in INCIDENT_RULES:
            if not _rule_matches(rule, text):
                continue
            refs = normalize_source_refs([ref])
            source_hash = source_ref_hash(refs)
            trigger = {"kind": "deterministic_extraction", "incident_type": rule["episode_type"]}
            time_window = normalize_json_map(raw_item.get("time_window") if isinstance(raw_item.get("time_window"), dict) else {})
            key = episode_key(
                episode_type=rule["episode_type"],
                source_ref_hash_value=source_hash,
                trigger_json=trigger,
                time_window_json=time_window,
            )
            if key in seen_keys:
                continue
            seen_keys.add(key)
            summary = _summary_for(rule, text)
            decisions.append(
                {
                    "decision_id": _decision_id({"owner_id": owner_id, "episode_key": key}),
                    "decision": "accept",
                    "episode_key": key,
                    "title": rule["title"],
                    "summary": summary,
                    "episode_type": rule["episode_type"],
                    "trigger": trigger,
                    "outcome": _compact_text(raw_item.get("outcome") or summary, limit=1000),
                    "significance": rule["significance"],
                    "unresolved": normalize_json_map(raw_item.get("unresolved") if isinstance(raw_item.get("unresolved"), dict) else {}),
                    "evidence": [{"source_ref": refs[0], "excerpt": _compact_text(text, limit=MAX_EXCERPT_LEN)}],
                    "source_refs": refs,
                    "time_window": time_window,
                    "participants": raw_item.get("participants") if isinstance(raw_item.get("participants"), list) else [],
                    "entities": raw_item.get("entities") if isinstance(raw_item.get("entities"), list) else [],
                    "callback_candidates": [
                        {
                            "strategy": "light_callback",
                            "reason": "incident_may_support_future_continuity",
                        }
                    ],
                    "confidence": 0.82,
                    "scene": scene,
                    "conversation_id": conversation_id,
                    "reasons": ["matched_meaningful_incident", rule["episode_type"]],
                }
            )
            break

    if not decisions:
        return [
            {
                "decision_id": _decision_id({"owner_id": owner_id, "request_id": request_id, "reason": "no_meaningful_incident"}),
                "decision": "reject",
                "episode_type": "unknown",
                "reasons": ["no_meaningful_incident"],
            }
        ]
    return decisions[:MAX_DECISIONS]


def normalize_episode_candidate(candidate: dict[str, Any]) -> dict[str, Any]:
    episode = dict(candidate)
    metadata = episode.get("metadata") if isinstance(episode.get("metadata"), dict) else {}
    explanation = episode.get("explanation") if isinstance(episode.get("explanation"), dict) else {}
    return {
        "episode_id": str(episode.get("episode_id") or episode.get("candidate_id") or episode.get("episode_key") or ""),
        "title": _compact_text(episode.get("title"), limit=400),
        "summary": _compact_text(episode.get("summary"), limit=600),
        "episode_type": _compact_text(episode.get("episode_type"), limit=80),
        "source_refs": episode.get("source_refs") if isinstance(episode.get("source_refs"), list) else [],
        "confidence": episode.get("confidence"),
        "significance": _compact_text(episode.get("significance"), limit=600),
        "unresolved": episode.get("unresolved") if isinstance(episode.get("unresolved"), dict) else {},
        "time_window": episode.get("time_window") if isinstance(episode.get("time_window"), dict) else {},
        "created_at": episode.get("created_at"),
        "updated_at": episode.get("updated_at"),
        "metadata": metadata,
        "explanation": explanation,
        "relevance_score": episode.get("relevance_score", metadata.get("relevance_score")),
        "continuity_value": episode.get("continuity_value", metadata.get("continuity_value")),
        "recency_score": episode.get("recency_score", metadata.get("recency_score")),
        "awkwardness_score": episode.get("awkwardness_score", metadata.get("awkwardness_score")),
        "scene": episode.get("scene") if isinstance(episode.get("scene"), dict) else metadata.get("scene", {}),
    }


def _score(value: Any, default: float) -> float:
    try:
        return max(0.0, min(1.0, float(value)))
    except (TypeError, ValueError):
        return default


def evaluate_episode_callback(*, context: dict[str, Any], candidate: dict[str, Any]) -> dict[str, Any]:
    episode = normalize_episode_candidate(candidate)
    context_scene = str(context.get("scene_id") or context.get("scene") or "").strip()
    episode_scene = ""
    if isinstance(episode.get("scene"), dict):
        episode_scene = str(episode["scene"].get("scene_id") or episode["scene"].get("scene") or "").strip()

    relevance = _score(episode.get("relevance_score"), 0.5)
    confidence = _score(episode.get("confidence"), 0.5)
    continuity = _score(episode.get("continuity_value"), 0.5)
    recency = _score(episode.get("recency_score"), 0.6)
    awkwardness = _score(episode.get("awkwardness_score"), 0.1)
    unresolved = bool(episode.get("unresolved"))
    if unresolved:
        continuity = max(continuity, 0.68)

    reasons: list[str] = []
    decision = "include"
    strategy = "light_callback"
    if confidence < 0.45:
        decision = "suppress"
        reasons.append("low_confidence")
    if relevance < 0.35:
        decision = "suppress"
        reasons.append("low_current_relevance")
    elif relevance < 0.5 and decision == "include":
        decision = "defer"
        reasons.append("weak_current_relevance")
    if recency < 0.2 and not unresolved:
        decision = "defer" if decision == "include" else decision
        reasons.append("stale_episode")
    if context_scene and episode_scene and context_scene != episode_scene and relevance < 0.75:
        decision = "suppress"
        reasons.append("scene_inappropriate")
    if awkwardness >= 0.65:
        decision = "suppress"
        reasons.append("awkward_or_tangential")
    if continuity < 0.35 and decision == "include":
        decision = "defer"
        reasons.append("low_continuity_value")

    if decision != "include":
        strategy = "none"
    elif unresolved and relevance >= 0.55:
        strategy = "explicit_callback"
        reasons.append("unresolved_follow_up_relevant")
    else:
        reasons.append("relevant_continuity_supported")

    callback_score = round((0.36 * relevance) + (0.24 * confidence) + (0.24 * continuity) + (0.16 * recency) - (0.22 * awkwardness), 4)
    return {
        "episode_id": episode["episode_id"],
        "decision": decision,
        "callback_strategy": strategy,
        "callback_score": max(0.0, min(1.0, callback_score)),
        "prompt_eligible": decision == "include",
        "reasons": [_bounded_reason(reason) for reason in dict.fromkeys(reasons)],
        "signals": {
            "current_relevance": relevance,
            "evidence_confidence": confidence,
            "continuity_value": continuity,
            "recency_score": recency,
            "awkwardness_risk": awkwardness,
            "unresolved_follow_up": unresolved,
            "scene_match": not (context_scene and episode_scene) or context_scene == episode_scene,
        },
        "episode": {
            "episode_id": episode["episode_id"],
            "title": episode["title"],
            "summary": episode["summary"],
            "episode_type": episode["episode_type"],
            "source_refs": episode["source_refs"][:10],
        },
    }


def select_episode_callbacks(*, context: dict[str, Any], candidates: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
    evaluated = [evaluate_episode_callback(context=context, candidate=candidate) for candidate in candidates[:100]]
    evaluated.sort(key=lambda item: (item["decision"] != "include", -item["callback_score"], item["episode_id"]))
    return evaluated[: max(1, min(50, limit))]


__all__ = [
    "evaluate_episode_callback",
    "extract_episode_decisions",
    "normalize_episode_candidate",
    "select_episode_callbacks",
]
