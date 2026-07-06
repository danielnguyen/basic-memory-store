from __future__ import annotations

import re
from typing import Any


PROMOTION_DECISIONS = ("promote", "update", "suppress", "defer")
PROMOTION_MEMORY_TYPES = ("short_horizon", "core", "procedural", "episodic", "dormant")
SUPPRESSION_REASONS = ("trivial", "low_value", "unsupported", "insufficient_evidence")


def _clamp(value: Any) -> float:
    try:
        return max(0.0, min(1.0, float(value)))
    except (TypeError, ValueError):
        return 0.0


def _flag(payload: dict[str, Any], *names: str) -> bool:
    for name in names:
        value = payload.get(name)
        if value is True:
            return True
        if isinstance(value, str) and value.strip().lower() in {"true", "yes", "1"}:
            return True
    return False


def _contains(text: str, patterns: tuple[str, ...]) -> bool:
    return any(re.search(pattern, text, flags=re.IGNORECASE) for pattern in patterns)


def _score_from_payload(payload: dict[str, Any], score_key: str, fallback: float) -> float:
    scores = payload.get("scores")
    if isinstance(scores, dict) and score_key in scores:
        return _clamp(scores.get(score_key))
    return fallback


def _source_refs(payload: dict[str, Any]) -> list[dict[str, Any]]:
    refs = payload.get("source_refs")
    if not isinstance(refs, list):
        return []
    return [ref for ref in refs if isinstance(ref, dict)]


def evaluate_promotion_candidate(candidate: dict[str, Any]) -> dict[str, Any]:
    text = " ".join(
        str(candidate.get(key) or "")
        for key in ("summary", "title", "claim", "candidate_text", "instruction")
    ).strip()
    lowered = text.lower()
    metadata = candidate.get("metadata") if isinstance(candidate.get("metadata"), dict) else {}
    evidence = candidate.get("evidence") if isinstance(candidate.get("evidence"), dict) else {}
    refs = _source_refs(candidate)

    occurrence_count = int(candidate.get("occurrence_count") or candidate.get("recurrence_count") or 0)
    recurrence = 1.0 if occurrence_count >= 3 else 0.65 if occurrence_count == 2 else 0.0
    if _flag(candidate, "recurring", "repeated") or _flag(metadata, "recurring", "repeated"):
        recurrence = max(recurrence, 0.75)

    future_usefulness = 0.0
    if _flag(candidate, "future_useful", "future_usefulness", "utility") or _flag(metadata, "future_useful"):
        future_usefulness = 0.85
    elif _contains(lowered, (r"\balways\b", r"\bprefer\b", r"\bnext time\b", r"\bworkflow\b", r"\buseful later\b")):
        future_usefulness = 0.65

    project_relevance = 0.0
    if candidate.get("project_id") or candidate.get("project") or metadata.get("project_id"):
        project_relevance = 0.85
    elif _contains(lowered, (r"\brepo\b", r"\bproject\b", r"\bimplementation\b", r"\bdeployment\b")):
        project_relevance = 0.55

    identity_relevance = 0.0
    if _flag(candidate, "identity_relevant") or _flag(metadata, "identity_relevant"):
        identity_relevance = 0.85
    elif _contains(lowered, (r"\bi prefer\b", r"\bmy preference\b", r"\bcall me\b", r"\bmy name\b")):
        identity_relevance = 0.7

    procedural_value = 0.0
    if _flag(candidate, "procedural", "workflow") or _flag(metadata, "procedural"):
        procedural_value = 0.9
    elif _contains(lowered, (r"\bprocedure\b", r"\bworkflow\b", r"\bstep\b", r"\bwhen .* then\b", r"\brun\b")):
        procedural_value = 0.65

    explicit_instruction = 0.0
    if _flag(candidate, "explicit_instruction", "remember") or _flag(metadata, "explicit_instruction"):
        explicit_instruction = 1.0
    elif _contains(lowered, (r"\bremember this\b", r"\bplease remember\b", r"\bstore this\b")):
        explicit_instruction = 1.0

    correction_significance = 0.0
    if candidate.get("supersedes_memory_id") or _flag(candidate, "correction", "corrected_fact"):
        correction_significance = 0.95
    elif _contains(lowered, (r"\bcorrection\b", r"\bcorrected\b", r"\binstead\b", r"\bnot .* anymore\b")):
        correction_significance = 0.75

    unsupported = _flag(candidate, "unsupported") or evidence.get("supported") is False
    insufficient_evidence = not refs and not evidence and not explicit_instruction
    trivial = len(text) < 28 and not any(
        (recurrence, future_usefulness, project_relevance, identity_relevance, procedural_value, explicit_instruction, correction_significance)
    )

    factors = {
        "recurrence": _score_from_payload(candidate, "recurrence_score", recurrence),
        "utility": _score_from_payload(candidate, "utility_score", future_usefulness),
        "future_usefulness": _score_from_payload(candidate, "future_usefulness_score", future_usefulness),
        "project_relevance": _score_from_payload(candidate, "project_relevance_score", project_relevance),
        "identity_relevance": _score_from_payload(candidate, "identity_relevance_score", identity_relevance),
        "procedural_value": _score_from_payload(candidate, "procedural_score", procedural_value),
        "explicit_user_instruction": _score_from_payload(candidate, "explicit_instruction_score", explicit_instruction),
        "correction_significance": _score_from_payload(candidate, "correction_significance_score", correction_significance),
    }
    total = round(
        (0.14 * factors["recurrence"])
        + (0.18 * factors["utility"])
        + (0.18 * factors["future_usefulness"])
        + (0.11 * factors["project_relevance"])
        + (0.10 * factors["identity_relevance"])
        + (0.13 * factors["procedural_value"])
        + (0.10 * factors["explicit_user_instruction"])
        + (0.06 * factors["correction_significance"]),
        4,
    )

    suppression_reasons: list[str] = []
    defer_reasons: list[str] = []
    if unsupported:
        suppression_reasons.append("unsupported")
    if trivial:
        suppression_reasons.append("trivial")
    if insufficient_evidence:
        defer_reasons.append("insufficient_evidence")
    strong_signal = any(
        score >= threshold
        for score, threshold in (
            (factors["recurrence"], 0.75),
            (factors["procedural_value"], 0.60),
            (factors["explicit_user_instruction"], 0.90),
            (factors["correction_significance"], 0.75),
        )
    )
    if total < 0.30 and not suppression_reasons and not strong_signal:
        suppression_reasons.append("low_value")

    decision = "defer"
    if suppression_reasons:
        decision = "suppress"
    elif (
        factors["explicit_user_instruction"] >= 0.9
        or total >= 0.58
        or factors["correction_significance"] >= 0.75
        or (factors["recurrence"] >= 0.75 and factors["future_usefulness"] >= 0.60)
        or factors["procedural_value"] >= 0.60
    ):
        decision = "update" if candidate.get("existing_memory_id") or candidate.get("supersedes_memory_id") else "promote"

    target_memory_type = "short_horizon"
    if decision == "suppress":
        target_memory_type = "dormant"
    elif factors["procedural_value"] >= 0.60:
        target_memory_type = "procedural"
    elif factors["identity_relevance"] >= 0.60 or factors["explicit_user_instruction"] >= 0.90 or factors["project_relevance"] >= 0.60:
        target_memory_type = "core"
    elif factors["correction_significance"] >= 0.60:
        target_memory_type = "core"

    reasons = {
        "score_formula": "weighted R20 promotion factors",
        "signals": {
            "source_ref_count": len(refs),
            "occurrence_count": occurrence_count,
            "unsupported": unsupported,
            "trivial": trivial,
        },
    }
    if suppression_reasons:
        reasons["suppression_reasons"] = suppression_reasons
    if defer_reasons and decision == "defer":
        reasons["defer_reasons"] = defer_reasons

    return {
        "decision": decision,
        "target_memory_type": target_memory_type,
        "factor_scores": factors,
        "promotion_score": total,
        "suppression_reasons": suppression_reasons,
        "defer_reasons": defer_reasons if decision == "defer" else [],
        "reasons": reasons,
    }
