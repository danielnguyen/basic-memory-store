from __future__ import annotations

import logging
import re
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from models import RetrievalOptions
from services.retrieval import retrieve_ranked_messages

GIT_RULE_KIND = "git_risk_scan"
PORTFOLIO_RULE_KIND = "portfolio_drift_review"
PROACTIVE_DERIVATION_VERSION = "proactive-rules-v1"
DEFAULT_PORTFOLIO_DRIFT_THRESHOLD = 0.05
DEFAULT_GIT_MIN_SCORE = 0.35
DEFAULT_COOLDOWN_HOURS = 24.0
DEFAULT_NEGATIVE_FEEDBACK_SUPPRESSION_DAYS = 7.0


def _coerce_float(value: Any) -> float | None:
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.strip())
        except ValueError:
            return None
    return None


def _first_string(payload: dict[str, Any], *keys: str) -> str | None:
    for key in keys:
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _build_git_query(payload: dict[str, Any], event_type: str) -> str:
    parts: list[str] = []
    for key in ("summary", "title", "repo", "branch", "symbol", "account"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            parts.append(value.strip())
    files = payload.get("files")
    if isinstance(files, list):
        for item in files[:3]:
            if isinstance(item, str) and item.strip():
                parts.append(item.strip())
    if not parts:
        parts.append(event_type.replace("_", " "))
    return " ".join(parts)


def _extract_allowed_surfaces(prefs: dict[str, Any]) -> list[str]:
    allowed = prefs.get("allowed_surfaces_json") or []
    if not isinstance(allowed, list):
        return []
    return [item for item in allowed if isinstance(item, str) and item.strip()]


def _resolve_delivery_surface(surface: str | None, prefs: dict[str, Any]) -> str | None:
    allowed = _extract_allowed_surfaces(prefs)
    if surface and surface in allowed:
        return surface
    for item in allowed:
        if item == "telegram":
            return "telegram"
    return None


def _extract_target_surface(prefs: dict[str, Any]) -> str | None:
    return _resolve_delivery_surface(None, prefs)


def _get_rule_pref(rule_prefs: dict[str, Any], rule: str, key: str) -> Any:
    section = rule_prefs.get(rule)
    if isinstance(section, dict):
        return section.get(key)
    return None


def _rule_float_pref(prefs: dict[str, Any], rule: str, key: str, fallback: float) -> float:
    rule_prefs = prefs.get("rule_prefs_json") or {}
    value = _coerce_float(_get_rule_pref(rule_prefs, rule, key))
    if value is not None:
        return value
    value = _coerce_float(_get_rule_pref(rule_prefs, "initiative", key))
    if value is not None:
        return value
    return fallback


def _cooldown_hours(prefs: dict[str, Any], rule: str) -> float:
    return _rule_float_pref(prefs, rule, "cooldown_hours", DEFAULT_COOLDOWN_HOURS)


def _negative_suppression_days(prefs: dict[str, Any], rule: str) -> float:
    return _rule_float_pref(
        prefs,
        rule,
        "negative_feedback_suppression_days",
        DEFAULT_NEGATIVE_FEEDBACK_SUPPRESSION_DAYS,
    )


def _profile_threshold(profile: dict[str, Any]) -> float | None:
    for section_name in ("tool_policy", "routing_policy", "response_style", "retrieval_policy", "safety_policy"):
        section = profile.get(section_name)
        if not isinstance(section, dict):
            continue
        proactive = section.get("proactive")
        if isinstance(proactive, dict):
            value = _coerce_float(proactive.get("portfolio_drift_threshold"))
            if value is not None:
                return value
        value = _coerce_float(section.get("portfolio_drift_threshold"))
        if value is not None:
            return value
    return None


def _resolve_portfolio_threshold(*, prefs: dict[str, Any], profile: dict[str, Any]) -> float:
    pref_value = _coerce_float(_get_rule_pref(prefs.get("rule_prefs_json") or {}, "portfolio", "drift_threshold"))
    if pref_value is not None:
        return pref_value
    profile_value = _profile_threshold(profile)
    if profile_value is not None:
        return profile_value
    return DEFAULT_PORTFOLIO_DRIFT_THRESHOLD


def _extract_portfolio_drift(payload: dict[str, Any]) -> float | None:
    for key in ("allocation_drift_pct", "drift_pct", "allocation_drift", "drift"):
        value = _coerce_float(payload.get(key))
        if value is not None:
            return value
    return None


def _normalize_part(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    value = re.sub(r"\s+", " ", value.strip().lower())
    return value or None


def _normalized_git_subject(payload: dict[str, Any]) -> str:
    parts = [
        _normalize_part(payload.get("repo")),
        _normalize_part(payload.get("branch")),
        _normalize_part(payload.get("title")) or _normalize_part(payload.get("summary")),
    ]
    return "|".join(part for part in parts if part) or "git:unknown"


def _normalized_portfolio_subject(payload: dict[str, Any], kind: str) -> str:
    parts = [
        _normalize_part(payload.get("account")),
        _normalize_part(payload.get("symbol")),
        kind,
    ]
    return "|".join(part for part in parts if part) or f"portfolio:{kind}"


def _cooldown_key(
    *,
    owner_id: str,
    kind: str,
    delivery_surface: str | None,
    source_type: str,
    normalized_subject: str,
) -> str:
    surface = delivery_surface or "none"
    return f"{owner_id}|{kind}|{surface}|{source_type}|{normalized_subject}"


def _base_reason(
    *,
    event_log: dict[str, Any],
    trigger_source: str,
    rule: str | None,
    delivery_surface: str | None,
    normalized_subject: str | None,
    cooldown_identity_key: str | None,
) -> dict[str, Any]:
    return {
        "trigger_source": trigger_source,
        "trigger_ref": {
            "source_event_log_id": event_log.get("event_log_id"),
            "source_event_id": event_log.get("source_event_id"),
            "source_type": event_log.get("source_type"),
            "event_type": event_log.get("event_type"),
        },
        "rule": rule,
        "surface_selection": {
            "delivery_surface": delivery_surface,
        },
        "normalized_subject": normalized_subject,
        "cooldown_identity_key": cooldown_identity_key,
    }


async def evaluate_event(
    *,
    pg: Any,
    qdrant: Any,
    settings: Any,
    request_id: str,
    owner_id: str,
    event_log_id: UUID,
    surface: str | None,
) -> dict[str, Any]:
    event_log = await pg.get_event_ingest_log(event_log_id)
    if event_log is None or event_log["owner_id"] != owner_id:
        return {
            "request_id": request_id,
            "owner_id": owner_id,
            "event_log_id": str(event_log_id),
            "initiative_event": None,
            "decisions": [],
            "suggestions": [],
            "created_count": 0,
        }

    initiative_event = await pg.create_initiative_event(
        owner_id=owner_id,
        request_id=request_id,
        source_event_log_id=event_log_id,
        trigger_type=event_log["source_type"],
        trigger_ref_json={
            "source_event_log_id": event_log["event_log_id"],
            "source_event_id": event_log.get("source_event_id"),
            "source_type": event_log.get("source_type"),
            "event_type": event_log.get("event_type"),
        },
        payload_json=event_log.get("payload_json") or {},
    )
    existing_decisions = await pg.list_initiative_decisions(UUID(initiative_event["initiative_event_id"]))
    if existing_decisions:
        suggestions = []
        for decision in existing_decisions:
            proactive_suggestion_id = decision.get("proactive_suggestion_id")
            if proactive_suggestion_id:
                suggestion = await pg.get_proactive_suggestion(UUID(proactive_suggestion_id))
                if suggestion is not None:
                    suggestions.append(suggestion)
        logging.info(
            "initiative_evaluate_replayed",
            extra={
                "owner_id": owner_id,
                "event_log_id": str(event_log_id),
                "request_id": request_id,
                "decision_count": len(existing_decisions),
            },
        )
        return _evaluation_response(
            request_id,
            owner_id,
            event_log_id,
            initiative_event,
            existing_decisions,
            suggestions,
        )

    prefs = await pg.get_proactive_prefs(owner_id)
    if not prefs or not prefs.get("enabled"):
        decision = await _record_decision(
            pg=pg,
            initiative_event=initiative_event,
            owner_id=owner_id,
            event_log=event_log,
            kind=None,
            decision_status="suppressed",
            score=None,
            delivery_surface=None,
            suppression_reason="prefs_disabled",
            normalized_subject=None,
            cooldown_identity_key=None,
            reason_extra={"policy": {"enabled": False}},
        )
        logging.info("initiative_evaluate_suppressed", extra={"owner_id": owner_id, "event_log_id": str(event_log_id), "reason": "prefs_disabled"})
        return _evaluation_response(request_id, owner_id, event_log_id, initiative_event, [decision], [])

    delivery_surface = _resolve_delivery_surface(surface, prefs)
    if delivery_surface is None:
        decision = await _record_decision(
            pg=pg,
            initiative_event=initiative_event,
            owner_id=owner_id,
            event_log=event_log,
            kind=None,
            decision_status="suppressed",
            score=None,
            delivery_surface=None,
            suppression_reason="no_allowed_surface",
            normalized_subject=None,
            cooldown_identity_key=None,
            reason_extra={
                "policy": {
                    "requested_surface": surface,
                    "allowed_surfaces": _extract_allowed_surfaces(prefs),
                }
            },
        )
        return _evaluation_response(request_id, owner_id, event_log_id, initiative_event, [decision], [])

    profile = await pg.resolve_profile(
        owner_id=owner_id,
        surface=delivery_surface,
        requested_profile=None,
        client_id="",
        default_profile_name=getattr(settings, "default_profile_name", "dev"),
    )

    source_type = event_log["source_type"]
    if source_type == "git":
        decision, suggestion = await _evaluate_git_event(
            pg=pg,
            qdrant=qdrant,
            owner_id=owner_id,
            event_log=event_log,
            initiative_event=initiative_event,
            prefs=prefs,
            delivery_surface=delivery_surface,
            conversation_id=UUID(event_log["conversation_id"]) if event_log.get("conversation_id") else None,
            settings=settings,
            generation_trace_id=request_id,
        )
        return _evaluation_response(request_id, owner_id, event_log_id, initiative_event, [decision], [suggestion] if suggestion else [])

    if source_type == "portfolio":
        decision, suggestion = await _evaluate_portfolio_event(
            pg=pg,
            owner_id=owner_id,
            event_log=event_log,
            initiative_event=initiative_event,
            prefs=prefs,
            profile=profile,
            delivery_surface=delivery_surface,
            generation_trace_id=request_id,
        )
        return _evaluation_response(request_id, owner_id, event_log_id, initiative_event, [decision], [suggestion] if suggestion else [])

    decision = await _record_decision(
        pg=pg,
        initiative_event=initiative_event,
        owner_id=owner_id,
        event_log=event_log,
        kind=None,
        decision_status="no_op",
        score=None,
        delivery_surface=delivery_surface,
        suppression_reason="no_matching_rule",
        normalized_subject=None,
        cooldown_identity_key=None,
        reason_extra={"policy": {"known_rules": ["git", "portfolio"]}},
    )
    logging.info("initiative_evaluate_no_rule", extra={"owner_id": owner_id, "event_log_id": str(event_log_id), "source_type": source_type})
    return _evaluation_response(request_id, owner_id, event_log_id, initiative_event, [decision], [])


def _evaluation_response(
    request_id: str,
    owner_id: str,
    event_log_id: UUID,
    initiative_event: dict[str, Any] | None,
    decisions: list[dict[str, Any]],
    suggestions: list[dict[str, Any]],
) -> dict[str, Any]:
    return {
        "request_id": request_id,
        "owner_id": owner_id,
        "event_log_id": str(event_log_id),
        "initiative_event": initiative_event,
        "decisions": decisions,
        "suggestions": suggestions,
        "created_count": len(suggestions),
    }


async def _record_decision(
    *,
    pg: Any,
    initiative_event: dict[str, Any],
    owner_id: str,
    event_log: dict[str, Any],
    kind: str | None,
    decision_status: str,
    score: float | None,
    delivery_surface: str | None,
    suppression_reason: str | None,
    normalized_subject: str | None,
    cooldown_identity_key: str | None,
    reason_extra: dict[str, Any] | None = None,
    proactive_suggestion_id: str | None = None,
    cooldown_until: str | None = None,
) -> dict[str, Any]:
    reason = _base_reason(
        event_log=event_log,
        trigger_source=event_log["source_type"],
        rule=kind,
        delivery_surface=delivery_surface,
        normalized_subject=normalized_subject,
        cooldown_identity_key=cooldown_identity_key,
    )
    reason.update(reason_extra or {})
    if suppression_reason:
        reason["suppression"] = {"reason": suppression_reason, "cooldown_until": cooldown_until}
    if proactive_suggestion_id:
        reason["delivery_outcome"] = {"proactive_suggestion_id": proactive_suggestion_id, "delivery_status": "not_attempted"}
    return await pg.create_initiative_decision(
        initiative_event_id=UUID(initiative_event["initiative_event_id"]),
        owner_id=owner_id,
        proactive_suggestion_id=UUID(proactive_suggestion_id) if proactive_suggestion_id else None,
        decision_status=decision_status,
        score=score,
        reason_json=reason,
        delivery_surface=delivery_surface,
        delivery_status="not_attempted",
        suppression_reason=suppression_reason,
        cooldown_identity_key=cooldown_identity_key,
        normalized_subject=normalized_subject,
        cooldown_until=cooldown_until,
    )


async def _policy_suppression(
    *,
    pg: Any,
    prefs: dict[str, Any],
    owner_id: str,
    kind: str,
    cooldown_identity_key: str,
) -> tuple[str | None, str | None, dict[str, Any]]:
    cooldown_hours = _cooldown_hours(prefs, kind)
    prior = await pg.get_recent_initiative_cooldown(
        owner_id=owner_id,
        cooldown_identity_key=cooldown_identity_key,
        cooldown_hours=cooldown_hours,
    )
    if prior is not None:
        return (
            "cooldown_active",
            prior.get("cooldown_until"),
            {"policy": {"cooldown_hours": cooldown_hours, "prior_decision_id": prior["decision_id"]}},
        )

    lookback_days = _negative_suppression_days(prefs, kind)
    feedback = await pg.get_recent_negative_initiative_feedback(
        owner_id=owner_id,
        cooldown_identity_key=cooldown_identity_key,
        lookback_days=lookback_days,
    )
    if feedback is not None:
        until = (datetime.now(UTC) + timedelta(days=lookback_days)).isoformat()
        return (
            "negative_feedback_suppression",
            until,
            {"feedback_signals": {"recent_negative_feedback": feedback, "lookback_days": lookback_days}},
        )
    return None, None, {}


async def _evaluate_git_event(
    *,
    pg: Any,
    qdrant: Any,
    owner_id: str,
    event_log: dict[str, Any],
    initiative_event: dict[str, Any],
    prefs: dict[str, Any],
    delivery_surface: str,
    conversation_id: UUID | None,
    settings: Any,
    generation_trace_id: str,
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    payload = event_log.get("payload_json") or {}
    normalized_subject = _normalized_git_subject(payload)
    cooldown_identity_key = _cooldown_key(
        owner_id=owner_id,
        kind=GIT_RULE_KIND,
        delivery_surface=delivery_surface,
        source_type="git",
        normalized_subject=normalized_subject,
    )
    query = _build_git_query(payload, event_log["event_type"])
    opts = RetrievalOptions(k=3, min_score=0.25, scope="owner", time_window="90d", retrieval_mode="recent")
    message_results = await retrieve_ranked_messages(
        pg=pg,
        qdrant=qdrant,
        settings=settings,
        owner_id=owner_id,
        query=query,
        opts=opts,
        conversation_id=conversation_id,
        client_id=None,
        exclude_message_ids=[event_log["message_id"]] if event_log.get("message_id") else None,
        context="proactive_git",
    )
    ranked = message_results["ranked_semantic"]
    threshold = _coerce_float(_get_rule_pref(prefs.get("rule_prefs_json") or {}, "git", "min_score")) or DEFAULT_GIT_MIN_SCORE
    if not ranked:
        decision = await _record_decision(
            pg=pg,
            initiative_event=initiative_event,
            owner_id=owner_id,
            event_log=event_log,
            kind=GIT_RULE_KIND,
            decision_status="suppressed",
            score=0.0,
            delivery_surface=delivery_surface,
            suppression_reason="below_threshold",
            normalized_subject=normalized_subject,
            cooldown_identity_key=cooldown_identity_key,
            reason_extra={"scoring": {"query": query, "score": 0.0, "threshold": threshold, "rationale": "No related memory matched the git event."}},
        )
        return decision, None

    matched_snippet, score_details = ranked[0]
    score = float(score_details["final_score"])
    if score < threshold:
        decision = await _record_decision(
            pg=pg,
            initiative_event=initiative_event,
            owner_id=owner_id,
            event_log=event_log,
            kind=GIT_RULE_KIND,
            decision_status="suppressed",
            score=score,
            delivery_surface=delivery_surface,
            suppression_reason="below_threshold",
            normalized_subject=normalized_subject,
            cooldown_identity_key=cooldown_identity_key,
            reason_extra={"scoring": {"query": query, "score": score, "threshold": threshold, "score_details": score_details}},
        )
        return decision, None

    suppression_reason, cooldown_until, extra = await _policy_suppression(
        pg=pg,
        prefs=prefs,
        owner_id=owner_id,
        kind=GIT_RULE_KIND,
        cooldown_identity_key=cooldown_identity_key,
    )
    if suppression_reason:
        decision = await _record_decision(
            pg=pg,
            initiative_event=initiative_event,
            owner_id=owner_id,
            event_log=event_log,
            kind=GIT_RULE_KIND,
            decision_status="suppressed",
            score=score,
            delivery_surface=delivery_surface,
            suppression_reason=suppression_reason,
            normalized_subject=normalized_subject,
            cooldown_identity_key=cooldown_identity_key,
            cooldown_until=cooldown_until,
            reason_extra={"scoring": {"query": query, "score": score, "threshold": threshold, "score_details": score_details}, **extra},
        )
        return decision, None

    topic = _first_string(payload, "title", "summary", "repo") or "this topic"
    suggestion, _ = await pg.create_proactive_suggestion(
        owner_id=owner_id,
        source_event_log_id=UUID(event_log["event_log_id"]),
        source_type="git",
        kind=GIT_RULE_KIND,
        title="Related git change may need a risk scan",
        body=f"You discussed {topic} recently; this new git event touches it. Want a quick risk scan?",
        explanation_json={
            "rule": GIT_RULE_KIND,
            "because": "A recent git event matched prior discussion in time-aware retrieval.",
            "derivation_version": PROACTIVE_DERIVATION_VERSION,
            "generation_trace_id": generation_trace_id,
            "query": query,
            "matched_message_id": matched_snippet["message_id"],
            "score_details": score_details,
            "initiative": {
                "normalized_subject": normalized_subject,
                "cooldown_identity_key": cooldown_identity_key,
                "delivery_surface": delivery_surface,
            },
        },
        evidence_json={
            "source_refs": [
                {
                    "ref_type": "event_log",
                    "ref_id": event_log["event_log_id"],
                    "support_kind": "direct",
                },
                {
                    "ref_type": "message",
                    "ref_id": matched_snippet["message_id"],
                    "support_kind": "corroborating",
                },
            ],
            "source_event_log_id": event_log["event_log_id"],
            "source_event_id": event_log["source_event_id"],
            "event_type": event_log["event_type"],
            "payload_summary": payload.get("summary"),
            "payload_title": payload.get("title"),
            "repo": payload.get("repo"),
            "branch": payload.get("branch"),
            "matched_message": {
                "message_id": matched_snippet["message_id"],
                "conversation_id": matched_snippet["conversation_id"],
                "created_at": matched_snippet["created_at"],
            },
        },
        target_surface=delivery_surface,
    )
    cooldown_until = (datetime.now(UTC) + timedelta(hours=_cooldown_hours(prefs, GIT_RULE_KIND))).isoformat()
    decision = await _record_decision(
        pg=pg,
        initiative_event=initiative_event,
        owner_id=owner_id,
        event_log=event_log,
        kind=GIT_RULE_KIND,
        decision_status="created",
        score=score,
        delivery_surface=delivery_surface,
        suppression_reason=None,
        normalized_subject=normalized_subject,
        cooldown_identity_key=cooldown_identity_key,
        proactive_suggestion_id=suggestion["suggestion_id"],
        cooldown_until=cooldown_until,
        reason_extra={"scoring": {"query": query, "score": score, "threshold": threshold, "score_details": score_details}},
    )
    logging.info("initiative_git_suggestion_created", extra={"owner_id": owner_id, "event_log_id": event_log["event_log_id"], "suggestion_id": suggestion["suggestion_id"], "decision_id": decision["decision_id"]})
    return decision, suggestion


async def _evaluate_portfolio_event(
    *,
    pg: Any,
    owner_id: str,
    event_log: dict[str, Any],
    initiative_event: dict[str, Any],
    prefs: dict[str, Any],
    profile: dict[str, Any],
    delivery_surface: str,
    generation_trace_id: str,
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    payload = event_log.get("payload_json") or {}
    normalized_subject = _normalized_portfolio_subject(payload, PORTFOLIO_RULE_KIND)
    cooldown_identity_key = _cooldown_key(
        owner_id=owner_id,
        kind=PORTFOLIO_RULE_KIND,
        delivery_surface=delivery_surface,
        source_type="portfolio",
        normalized_subject=normalized_subject,
    )
    drift = _extract_portfolio_drift(payload)
    threshold = _resolve_portfolio_threshold(prefs=prefs, profile=profile)
    if drift is None or drift <= threshold:
        decision = await _record_decision(
            pg=pg,
            initiative_event=initiative_event,
            owner_id=owner_id,
            event_log=event_log,
            kind=PORTFOLIO_RULE_KIND,
            decision_status="suppressed",
            score=drift,
            delivery_surface=delivery_surface,
            suppression_reason="below_threshold",
            normalized_subject=normalized_subject,
            cooldown_identity_key=cooldown_identity_key,
            reason_extra={"scoring": {"observed_drift": drift, "threshold": threshold, "rationale": "Portfolio drift did not exceed threshold."}},
        )
        return decision, None

    suppression_reason, cooldown_until, extra = await _policy_suppression(
        pg=pg,
        prefs=prefs,
        owner_id=owner_id,
        kind=PORTFOLIO_RULE_KIND,
        cooldown_identity_key=cooldown_identity_key,
    )
    if suppression_reason:
        decision = await _record_decision(
            pg=pg,
            initiative_event=initiative_event,
            owner_id=owner_id,
            event_log=event_log,
            kind=PORTFOLIO_RULE_KIND,
            decision_status="suppressed",
            score=drift,
            delivery_surface=delivery_surface,
            suppression_reason=suppression_reason,
            normalized_subject=normalized_subject,
            cooldown_identity_key=cooldown_identity_key,
            cooldown_until=cooldown_until,
            reason_extra={"scoring": {"observed_drift": drift, "threshold": threshold}, **extra},
        )
        return decision, None

    account = _first_string(payload, "account") or "portfolio"
    suggestion, _ = await pg.create_proactive_suggestion(
        owner_id=owner_id,
        source_event_log_id=UUID(event_log["event_log_id"]),
        source_type="portfolio",
        kind=PORTFOLIO_RULE_KIND,
        title="Portfolio allocation drift crossed threshold",
        body=f"{account.capitalize()} allocation drifted beyond your threshold. Review the portfolio?",
        explanation_json={
            "rule": PORTFOLIO_RULE_KIND,
            "because": "A portfolio event reported allocation drift above the configured threshold.",
            "derivation_version": PROACTIVE_DERIVATION_VERSION,
            "generation_trace_id": generation_trace_id,
            "observed_drift": drift,
            "threshold": threshold,
            "initiative": {
                "normalized_subject": normalized_subject,
                "cooldown_identity_key": cooldown_identity_key,
                "delivery_surface": delivery_surface,
            },
        },
        evidence_json={
            "source_refs": [
                {
                    "ref_type": "event_log",
                    "ref_id": event_log["event_log_id"],
                    "support_kind": "direct",
                }
            ],
            "source_event_log_id": event_log["event_log_id"],
            "source_event_id": event_log["source_event_id"],
            "event_type": event_log["event_type"],
            "account": payload.get("account"),
            "symbol": payload.get("symbol"),
            "summary": payload.get("summary"),
            "observed_drift": drift,
            "threshold": threshold,
        },
        target_surface=delivery_surface,
    )
    cooldown_until = (datetime.now(UTC) + timedelta(hours=_cooldown_hours(prefs, PORTFOLIO_RULE_KIND))).isoformat()
    decision = await _record_decision(
        pg=pg,
        initiative_event=initiative_event,
        owner_id=owner_id,
        event_log=event_log,
        kind=PORTFOLIO_RULE_KIND,
        decision_status="created",
        score=drift,
        delivery_surface=delivery_surface,
        suppression_reason=None,
        normalized_subject=normalized_subject,
        cooldown_identity_key=cooldown_identity_key,
        proactive_suggestion_id=suggestion["suggestion_id"],
        cooldown_until=cooldown_until,
        reason_extra={"scoring": {"observed_drift": drift, "threshold": threshold}},
    )
    logging.info("initiative_portfolio_suggestion_created", extra={"owner_id": owner_id, "event_log_id": event_log["event_log_id"], "suggestion_id": suggestion["suggestion_id"], "decision_id": decision["decision_id"]})
    return decision, suggestion
