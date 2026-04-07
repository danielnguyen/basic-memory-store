from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

from models import RetrievalOptions
from services.retrieval import retrieve_ranked_messages

GIT_RULE_KIND = "git_risk_scan"
PORTFOLIO_RULE_KIND = "portfolio_drift_review"
DEFAULT_PORTFOLIO_DRIFT_THRESHOLD = 0.05
DEFAULT_GIT_MIN_SCORE = 0.35


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


def _extract_target_surface(prefs: dict[str, Any]) -> str | None:
    allowed = prefs.get("allowed_surfaces_json") or []
    if not isinstance(allowed, list):
        return None
    for item in allowed:
        if item == "telegram":
            return "telegram"
    return None


def _get_rule_pref(rule_prefs: dict[str, Any], rule: str, key: str) -> Any:
    section = rule_prefs.get(rule)
    if isinstance(section, dict):
        return section.get(key)
    return None


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


async def evaluate_event(
    *,
    pg: Any,
    qdrant: Any,
    settings: Any,
    owner_id: str,
    event_log_id: UUID,
    surface: str | None,
) -> list[dict[str, Any]]:
    event_log = await pg.get_event_ingest_log(event_log_id)
    if event_log is None or event_log["owner_id"] != owner_id:
        return []

    prefs = await pg.get_proactive_prefs(owner_id)
    if not prefs or not prefs.get("enabled"):
        logging.info("proactive_evaluate_skipped", extra={"owner_id": owner_id, "event_log_id": str(event_log_id), "reason": "disabled"})
        return []

    resolved_surface = surface or _extract_target_surface(prefs) or "telegram"
    profile = await pg.resolve_profile(
        owner_id=owner_id,
        surface=resolved_surface,
        requested_profile=None,
        client_id="",
        default_profile_name=getattr(settings, "default_profile_name", "dev"),
    )

    source_type = event_log["source_type"]
    if source_type == "git":
        suggestion = await _evaluate_git_event(
            pg=pg,
            qdrant=qdrant,
            owner_id=owner_id,
            event_log=event_log,
            prefs=prefs,
            conversation_id=UUID(event_log["conversation_id"]) if event_log.get("conversation_id") else None,
            settings=settings,
        )
        return [suggestion] if suggestion else []
    if source_type == "portfolio":
        suggestion = await _evaluate_portfolio_event(
            pg=pg,
            owner_id=owner_id,
            event_log=event_log,
            prefs=prefs,
            profile=profile,
        )
        return [suggestion] if suggestion else []

    logging.info("proactive_evaluate_no_rule", extra={"owner_id": owner_id, "event_log_id": str(event_log_id), "source_type": source_type})
    return []


async def _evaluate_git_event(
    *,
    pg: Any,
    qdrant: Any,
    owner_id: str,
    event_log: dict[str, Any],
    prefs: dict[str, Any],
    conversation_id: UUID | None,
    settings: Any,
) -> dict[str, Any] | None:
    payload = event_log.get("payload_json") or {}
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
    if not ranked:
        logging.info("proactive_git_no_match", extra={"owner_id": owner_id, "event_log_id": event_log["event_log_id"], "query": query})
        return None

    matched_snippet, score_details = ranked[0]
    threshold = _coerce_float(_get_rule_pref(prefs.get("rule_prefs_json") or {}, "git", "min_score")) or DEFAULT_GIT_MIN_SCORE
    if score_details["final_score"] < threshold:
        logging.info("proactive_git_below_threshold", extra={"owner_id": owner_id, "event_log_id": event_log["event_log_id"], "score": score_details["final_score"], "threshold": threshold})
        return None

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
            "query": query,
            "matched_message_id": matched_snippet["message_id"],
            "score_details": score_details,
        },
        evidence_json={
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
                "content": matched_snippet["content"],
            },
        },
        target_surface=_extract_target_surface(prefs),
    )
    logging.info("proactive_git_suggestion_created", extra={"owner_id": owner_id, "event_log_id": event_log["event_log_id"], "suggestion_id": suggestion["suggestion_id"]})
    return suggestion


async def _evaluate_portfolio_event(
    *,
    pg: Any,
    owner_id: str,
    event_log: dict[str, Any],
    prefs: dict[str, Any],
    profile: dict[str, Any],
) -> dict[str, Any] | None:
    payload = event_log.get("payload_json") or {}
    drift = _extract_portfolio_drift(payload)
    if drift is None:
        logging.info("proactive_portfolio_no_drift", extra={"owner_id": owner_id, "event_log_id": event_log["event_log_id"]})
        return None

    threshold = _resolve_portfolio_threshold(prefs=prefs, profile=profile)
    if drift <= threshold:
        logging.info("proactive_portfolio_below_threshold", extra={"owner_id": owner_id, "event_log_id": event_log["event_log_id"], "drift": drift, "threshold": threshold})
        return None

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
            "observed_drift": drift,
            "threshold": threshold,
        },
        evidence_json={
            "source_event_log_id": event_log["event_log_id"],
            "source_event_id": event_log["source_event_id"],
            "event_type": event_log["event_type"],
            "account": payload.get("account"),
            "symbol": payload.get("symbol"),
            "summary": payload.get("summary"),
            "observed_drift": drift,
            "threshold": threshold,
        },
        target_surface=_extract_target_surface(prefs),
    )
    logging.info("proactive_portfolio_suggestion_created", extra={"owner_id": owner_id, "event_log_id": event_log["event_log_id"], "suggestion_id": suggestion["suggestion_id"]})
    return suggestion
