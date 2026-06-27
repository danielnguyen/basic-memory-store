from __future__ import annotations

from hashlib import sha256
import json
from pathlib import Path
from typing import Any
from uuid import UUID

from services.chunking import chunk_text
from services.derived_contract import CONTRACT_ADAPTERS, normalize_contract_source_refs
from services.derivation_versions import EPISODE_DERIVATION_VERSION, MEMORY_ITEM_DERIVATION_VERSION
from services.proactive import PROACTIVE_DERIVATION_VERSION, reevaluate_proactive_candidate


DERIVED_CLASSES = ("derived_text", "proactive_suggestion", "memory_item", "episode")
INVALIDATION_REASONS = {
    "source_changed",
    "source_missing",
    "source_access_lost",
    "derivation_version_changed",
    "explicit_retraction",
    "existing_lifecycle_conflict",
}
TERMINAL_RESULTS = {"identical", "replaced", "unsupported", "failed"}
MEMORY_RECIPE_KIND = "structured-memory-promotion-v1"
EPISODE_RECIPE_KIND = "structured-episode-construction-v1"
DERIVED_TEXT_DERIVATION_VERSION = "file-chunk-v1"
DERIVED_TEXT_CHUNKING_ALGORITHM_VERSION = "fixed-overlap-text-v1"
SUPPORTED_TARGET_VERSIONS = {
    "derived_text": {DERIVED_TEXT_DERIVATION_VERSION},
    "proactive_suggestion": {PROACTIVE_DERIVATION_VERSION},
    "memory_item": {MEMORY_ITEM_DERIVATION_VERSION},
    "episode": {EPISODE_DERIVATION_VERSION},
}


def compact_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def structural_hash(value: Any) -> str:
    return sha256(compact_json(value).encode("utf-8")).hexdigest()


def bounded_scalars(value: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    out: dict[str, Any] = {}
    for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))[:8]:
        safe_key = str(key).strip()[:64]
        if not safe_key:
            continue
        if isinstance(item, str):
            out[safe_key] = item[:160]
        elif isinstance(item, (int, float, bool)) or item is None:
            out[safe_key] = item
    return out


def lifecycle_event(*, request_id: str, event_type: str, reason_code: str, metadata: dict[str, Any] | None = None) -> dict[str, Any]:
    event = {
        "event_type": event_type,
        "request_id": request_id,
        "reason_code": reason_code,
    }
    safe = bounded_scalars(metadata)
    if safe:
        event["metadata"] = safe
    return event


def replay_event(
    *,
    request_id: str,
    event_type: str,
    reason_code: str,
    result: str | None = None,
    replacement_id: str | None = None,
    failure_reason: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    event = lifecycle_event(request_id=request_id, event_type=event_type, reason_code=reason_code, metadata=metadata)
    if result is not None:
        event["result"] = result
    if replacement_id is not None:
        event["replacement_id"] = replacement_id
    if failure_reason is not None:
        event["failure_reason"] = failure_reason
    return event


def lifecycle_events_for(derived_class: str, row: dict[str, Any]) -> list[dict[str, Any]]:
    if derived_class == "derived_text":
        params = row.get("derivation_params") if isinstance(row.get("derivation_params"), dict) else {}
        lifecycle = params.get("lifecycle") if isinstance(params.get("lifecycle"), dict) else {}
        return [event for event in lifecycle.get("events") or [] if isinstance(event, dict)]
    if derived_class == "proactive_suggestion":
        evidence = row.get("evidence_json") if isinstance(row.get("evidence_json"), dict) else {}
        lifecycle = evidence.get("lifecycle") if isinstance(evidence.get("lifecycle"), dict) else {}
        return [event for event in lifecycle.get("events") or [] if isinstance(event, dict)]
    return [event for event in row.get("_events") or [] if isinstance(event, dict)]


def normalized_terminal_reason(event: dict[str, Any]) -> dict[str, Any]:
    reason = event.get("reason_json") if isinstance(event.get("reason_json"), dict) else event
    normalized = dict(reason)
    metadata = normalized.get("reason_metadata")
    if not isinstance(metadata, dict):
        metadata = normalized.get("metadata")
    if isinstance(metadata, dict):
        if normalized.get("terminal_result") is None and metadata.get("terminal_result") is not None:
            normalized["terminal_result"] = metadata.get("terminal_result")
        if normalized.get("failure_reason") is None and metadata.get("failure_reason") is not None:
            normalized["failure_reason"] = metadata.get("failure_reason")
        if normalized.get("replacement_id") is None and metadata.get("replacement_id") is not None:
            normalized["replacement_id"] = metadata.get("replacement_id")
    if normalized.get("event_type") is None and event.get("event_type") is not None:
        normalized["event_type"] = event.get("event_type")
    return normalized


def terminal_for_request(derived_class: str, row: dict[str, Any], request_id: str) -> dict[str, Any] | None:
    for event in reversed(lifecycle_events_for(derived_class, row)):
        reason = normalized_terminal_reason(event)
        if reason.get("request_id") != request_id:
            continue
        result = reason.get("result") or reason.get("terminal_result")
        if result in TERMINAL_RESULTS:
            return reason
        if reason.get("event_type") in {"rebuild_terminal", "terminal"}:
            return reason
    return None


def current_version_for(derived_class: str, row: dict[str, Any], inspection: dict[str, Any]) -> str:
    if derived_class == "memory_item":
        recipe = (row.get("explanation_json") or {}).get("derivation_recipe") or {}
        return str(recipe.get("derivation_version") or row.get("derivation_version") or inspection["contract"]["derivation_version"])
    if derived_class == "episode":
        recipe = (row.get("explanation_json") or {}).get("derivation_recipe") or {}
        return str(recipe.get("derivation_version") or row.get("derivation_version") or inspection["contract"]["derivation_version"])
    return str(inspection["contract"]["derivation_version"])


def validate_replay_versions(
    *,
    derived_class: str,
    current_version: str,
    requested_version: str | None,
    expected_current_version: str | None,
) -> tuple[str | None, dict[str, Any] | None]:
    if expected_current_version and expected_current_version != current_version:
        return None, {
            "result": "failed",
            "failure_reason": "expected_current_derivation_version_mismatch",
            "current_derivation_version": current_version,
            "expected_current_derivation_version": expected_current_version,
        }
    supported = SUPPORTED_TARGET_VERSIONS[derived_class]
    target = requested_version or current_version
    if target not in supported:
        return None, {
            "result": "unsupported",
            "failure_reason": "unsupported_derivation_version",
            "requested_derivation_version": target,
            "supported_derivation_versions": sorted(supported),
        }
    return target, None


def append_lifecycle_event(metadata: dict[str, Any], event: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    lifecycle = metadata.get("lifecycle") if isinstance(metadata.get("lifecycle"), dict) else {}
    events = lifecycle.get("events") if isinstance(lifecycle.get("events"), list) else []
    signature = (event.get("event_type"), event.get("request_id"), event.get("reason_code"))
    for existing in events:
        if not isinstance(existing, dict):
            continue
        if (existing.get("event_type"), existing.get("request_id"), existing.get("reason_code")) == signature:
            return metadata, False
    updated_events = [*events, event]
    event_type = event["event_type"]
    result = event.get("result") or (event.get("metadata") or {}).get("result")
    status = lifecycle.get("status") or metadata.get("status")
    if event_type in {"invalidated", "rebuilding", "superseded"}:
        if status not in {"superseded", "invalidated"} or event_type == "superseded":
            status = event_type
    if event_type == "rebuild_terminal":
        if result == "identical":
            status = "active"
        elif result == "replaced":
            status = "superseded"
        elif result in {"unsupported", "failed"}:
            status = status if status in {"superseded", "invalidated"} else "invalidated"
    updated_lifecycle = {
        **lifecycle,
        "status": status,
        "invalidated_reason": event["reason_code"] if event_type == "invalidated" else lifecycle.get("invalidated_reason"),
        "last_request_id": event["request_id"],
        "terminal_result": result or lifecycle.get("terminal_result"),
        "replacement_id": event.get("replacement_id") or (event.get("metadata") or {}).get("replacement_id") or lifecycle.get("replacement_id"),
        "failure_reason": event.get("failure_reason") or (event.get("metadata") or {}).get("failure_reason") or lifecycle.get("failure_reason"),
        "events": updated_events,
    }
    updated = {**metadata, "lifecycle": updated_lifecycle}
    if status:
        updated["status"] = status
    if updated_lifecycle.get("replacement_id"):
        updated["replacement_id"] = updated_lifecycle["replacement_id"]
    return updated, True


def build_structural_snapshot(*, derived_class: str, row: dict[str, Any], contract: dict[str, Any]) -> dict[str, Any]:
    base = {
        "derived_class": derived_class,
        "derived_id": contract["derived_id"],
        "owner_id": contract["owner_id"],
        "derivation_type": contract["derivation_type"],
        "derivation_version": contract["derivation_version"],
        "source_refs": contract["source_refs"],
        "source_ref_hash": structural_hash(contract["source_refs"]),
        "status": contract["status"],
    }
    if derived_class == "derived_text":
        params = row.get("derivation_params") if isinstance(row.get("derivation_params"), dict) else {}
        base.update({
            "stable_object_key": f"{row.get('artifact_id')}:{params.get('chunk_index')}",
            "ordering": {"chunk_index": params.get("chunk_index")},
            "normalized_output_hash": structural_hash({
                "kind": row.get("kind"),
                "text_hash": structural_hash(str(row.get("text") or "")),
                "char_start": params.get("char_start"),
                "char_end": params.get("char_end"),
            }),
        })
    elif derived_class == "proactive_suggestion":
        explanation = row.get("explanation_json") if isinstance(row.get("explanation_json"), dict) else {}
        evidence = row.get("evidence_json") if isinstance(row.get("evidence_json"), dict) else {}
        base.update({
            "stable_object_key": f"{row.get('source_event_log_id')}:{row.get('kind')}",
            "normalized_output_hash": structural_hash({
                "kind": row.get("kind"),
                "title_hash": structural_hash(str(row.get("title") or "")),
                "body_hash": structural_hash(str(row.get("body") or "")),
                "rule": explanation.get("rule"),
                "source_event_log_id": evidence.get("source_event_log_id"),
                "rule_inputs": {
                    "threshold": explanation.get("threshold"),
                    "observed_drift": explanation.get("observed_drift"),
                    "query": explanation.get("query"),
                    "matched_message_id": explanation.get("matched_message_id"),
                },
            }),
        })
    elif derived_class == "memory_item":
        base.update({
            "stable_object_key": row.get("source_ref_hash"),
            "lifecycle_result": row.get("freshness_state") or row.get("status"),
            "replacement_identity": row.get("superseded_by_memory_id"),
            "normalized_output_hash": structural_hash({
                "memory_type": row.get("memory_type"),
                "summary_hash": structural_hash(str(row.get("summary") or "")),
                "scores": row.get("scores_json") or {},
                "promotion_state": row.get("promotion_state"),
            }),
        })
    else:
        base.update({
            "stable_object_key": row.get("episode_key"),
            "lifecycle_result": row.get("status"),
            "normalized_output_hash": structural_hash({
                "episode_type": row.get("episode_type"),
                "title_hash": structural_hash(str(row.get("title") or "")),
                "summary_hash": structural_hash(str(row.get("summary") or "")),
                "outcome_hash": structural_hash(str(row.get("outcome") or "")),
                "significance_hash": structural_hash(str(row.get("significance") or "")),
                "unresolved": row.get("unresolved_json") or {},
                "callback_count": len(row.get("callback_candidates_json") or []),
                "participant_count": len(row.get("participants_json") or []),
            }),
        })
    return base


def classify_rebuildability(derived_class: str, row: dict[str, Any]) -> dict[str, str]:
    if derived_class == "derived_text":
        params = row.get("derivation_params") if isinstance(row.get("derivation_params"), dict) else {}
        required = {
            "chunking_algorithm_version",
            "chunk_size",
            "chunk_overlap",
            "chunk_index",
            "char_start",
            "char_end",
        }
        if (
            row.get("artifact_id")
            and params.get("derivation_version") == DERIVED_TEXT_DERIVATION_VERSION
            and params.get("chunking_algorithm_version") == DERIVED_TEXT_CHUNKING_ALGORITHM_VERSION
            and required <= set(params)
        ):
            return {"classification": "rebuildable", "reason": "artifact-backed deterministic chunk derivation"}
        return {"classification": "not_rebuildable", "reason": "missing deterministic artifact chunk recipe"}
    if derived_class == "proactive_suggestion":
        return {"classification": "replay_only", "reason": "proactive rules are deterministic but replay must not repeat delivery effects"}
    if derived_class == "memory_item":
        return {"classification": "not_rebuildable", "reason": "caller_authored_no_deterministic_recipe"}
    if derived_class == "episode":
        return {"classification": "not_rebuildable", "reason": "caller_authored_no_deterministic_recipe"}
    return {"classification": "not_rebuildable", "reason": "unsupported derived class"}


async def load_derived_row(pg: Any, *, derived_class: str, derived_id: UUID, owner_id: str) -> dict[str, Any] | None:
    if derived_class == "derived_text":
        return await pg.get_derived_text_for_owner(derived_id, owner_id)
    if derived_class == "proactive_suggestion":
        row = await pg.get_proactive_suggestion(derived_id)
        return row if row is not None and row.get("owner_id") == owner_id else None
    if derived_class == "memory_item":
        debug = await pg.get_memory_debug(derived_id, owner_id)
        if debug is None:
            return None
        row = debug["memory"]
        row["_events"] = debug.get("events") or []
        return row
    if derived_class == "episode":
        debug = await pg.get_episode_debug(derived_id, owner_id)
        if debug is None:
            return None
        row = debug["episode"]
        row["_events"] = debug.get("events") or []
        row["_links"] = debug.get("links") or []
        return row
    return None


def inspect_row(*, derived_class: str, row: dict[str, Any]) -> dict[str, Any]:
    adapter = CONTRACT_ADAPTERS[derived_class]
    contract = adapter(row)
    classification = classify_rebuildability(derived_class, row)
    metadata = {}
    if derived_class == "derived_text":
        metadata = row.get("derivation_params") if isinstance(row.get("derivation_params"), dict) else {}
    elif derived_class == "proactive_suggestion":
        metadata = row.get("evidence_json") if isinstance(row.get("evidence_json"), dict) else {}
    lifecycle = metadata.get("lifecycle") if isinstance(metadata.get("lifecycle"), dict) else {}
    return {
        "derived_class": derived_class,
        "derived_id": contract["derived_id"],
        "owner_id": contract["owner_id"],
        "contract": contract,
        "rebuildability": classification["classification"],
        "rebuildability_reason": classification["reason"],
        "lifecycle_status": lifecycle.get("status") or contract.get("effective_status") or contract.get("status"),
        "invalidation": {
            "reason": lifecycle.get("invalidated_reason"),
            "request_id": lifecycle.get("last_request_id"),
        },
        "source_summary": {
            "source_ref_count": len(contract["source_refs"]),
            "source_ref_hash": structural_hash(contract["source_refs"]),
            "ref_types": sorted({item["ref_type"] for item in contract["source_refs"]}),
        },
        "events": row.get("_events") or lifecycle.get("events") or [],
        "structural_snapshot": build_structural_snapshot(derived_class=derived_class, row=row, contract=contract),
    }


async def invalidate_derived(
    pg: Any,
    *,
    derived_class: str,
    derived_id: UUID,
    owner_id: str,
    request_id: str,
    reason_code: str,
    metadata: dict[str, Any] | None,
) -> dict[str, Any] | None:
    if reason_code not in INVALIDATION_REASONS:
        raise ValueError("unsupported invalidation reason")
    row = await load_derived_row(pg, derived_class=derived_class, derived_id=derived_id, owner_id=owner_id)
    if row is None:
        return None
    if derived_class == "memory_item":
        result = await pg.transition_memory_item(
            memory_id=derived_id,
            owner_id=owner_id,
            new_status="invalidated",
            reason_code=reason_code,
            reason_metadata=metadata or {},
            request_id=request_id,
            related_memory_id=None,
        )
        if result is None:
            return None
        return {"changed": result["changed"], "inspection": inspect_row(derived_class=derived_class, row=result["memory"])}
    event = lifecycle_event(request_id=request_id, event_type="invalidated", reason_code=reason_code, metadata=metadata)
    if derived_class == "episode":
        result = await pg.transition_episode_status(
            episode_id=derived_id,
            owner_id=owner_id,
            new_status="invalidated",
            request_id=request_id,
            reason_json=event,
        )
        if result is None:
            return None
        return {"changed": result["changed"], "inspection": inspect_row(derived_class=derived_class, row=result["episode"])}
    if derived_class == "derived_text":
        params = row.get("derivation_params") if isinstance(row.get("derivation_params"), dict) else {}
        updated, changed = append_lifecycle_event(params, event)
        if hasattr(pg, "append_derived_text_lifecycle_event"):
            saved = await pg.append_derived_text_lifecycle_event(derived_text_id=derived_id, owner_id=owner_id, event=event)
        else:
            saved = await pg.update_derived_text_params(derived_text_id=derived_id, owner_id=owner_id, derivation_params=updated)
        return {"changed": changed, "inspection": inspect_row(derived_class=derived_class, row=saved or row)}
    if derived_class == "proactive_suggestion":
        evidence = row.get("evidence_json") if isinstance(row.get("evidence_json"), dict) else {}
        updated, changed = append_lifecycle_event(evidence, event)
        if hasattr(pg, "append_proactive_suggestion_lifecycle_event"):
            saved = await pg.append_proactive_suggestion_lifecycle_event(suggestion_id=derived_id, owner_id=owner_id, event=event)
        else:
            saved = await pg.update_proactive_suggestion_evidence(suggestion_id=derived_id, owner_id=owner_id, evidence_json=updated)
        return {"changed": changed, "inspection": inspect_row(derived_class=derived_class, row=saved or row)}
    return None


async def build_candidate_snapshot(pg: Any, *, derived_class: str, row: dict[str, Any], requested_version: str | None) -> dict[str, Any]:
    if derived_class == "derived_text":
        params = row.get("derivation_params") if isinstance(row.get("derivation_params"), dict) else {}
        if params.get("chunking_algorithm_version") != DERIVED_TEXT_CHUNKING_ALGORITHM_VERSION:
            raise ValueError("unsupported_legacy_chunk_recipe")
        for key in ("chunk_size", "chunk_overlap", "chunk_index", "char_start", "char_end"):
            if key not in params:
                raise ValueError("unsupported_legacy_chunk_recipe")
        artifact = await pg.get_artifact(UUID(row["artifact_id"]))
        if artifact is None or artifact.get("owner_id") != row.get("owner_id"):
            raise KeyError("source_missing")
        uri = str(artifact.get("object_uri") or "")
        if not uri.startswith("file://"):
            raise ValueError("artifact source is not locally replayable")
        path = Path(uri[7:])
        if not path.exists():
            raise KeyError("source_missing")
        text = path.read_text(encoding="utf-8", errors="ignore")
        chunks = chunk_text(text, chunk_size=int(params["chunk_size"]), chunk_overlap=int(params["chunk_overlap"]))
        chunk_index = int(params["chunk_index"])
        if chunk_index >= len(chunks):
            raise KeyError("source_missing")
        chunk = chunks[chunk_index]
        return {
            "derived_class": "derived_text",
            "derivation_type": "chunk",
            "derivation_version": requested_version or params.get("derivation_version") or "file-chunk-v1",
            "source_refs": normalize_contract_source_refs(params.get("source_refs") or [{"ref_type": "artifact", "ref_id": row["artifact_id"], "support_kind": "direct"}]),
            "stable_object_key": f"{row.get('artifact_id')}:{chunk_index}",
            "ordering": {"chunk_index": chunk_index, "chunk_count": len(chunks)},
            "normalized_output_hash": structural_hash({
                "kind": "chunk",
                "text_hash": structural_hash(str(chunk["text"])),
                "char_start": chunk["char_start"],
                "char_end": chunk["char_end"],
            }),
            "candidate": {"text": chunk["text"], "chunk": chunk},
        }
    if derived_class == "proactive_suggestion":
        source_event_log_id = row.get("source_event_log_id")
        if not source_event_log_id:
            raise KeyError("source_missing")
        event_log = await pg.get_event_ingest_log(UUID(source_event_log_id))
        if event_log is None:
            raise KeyError("source_missing")
        try:
            candidate = reevaluate_proactive_candidate(
                owner_id=row["owner_id"],
                event_log=event_log,
                stored_suggestion=row,
                generation_trace_id="replay-validation",
            )
        except PermissionError as exc:
            raise ValueError(str(exc)) from exc
        explanation = candidate["explanation_json"]
        evidence = candidate["evidence_json"]
        return {
            "derived_class": "proactive_suggestion",
            "derivation_version": PROACTIVE_DERIVATION_VERSION,
            "stable_object_key": f"{row.get('source_event_log_id')}:{row.get('kind')}",
            "normalized_output_hash": structural_hash({
                "kind": candidate.get("kind"),
                "title_hash": structural_hash(str(candidate.get("title") or "")),
                "body_hash": structural_hash(str(candidate.get("body") or "")),
                "rule": explanation.get("rule"),
                "source_event_log_id": evidence.get("source_event_log_id"),
                "rule_inputs": {
                    "threshold": explanation.get("threshold"),
                    "observed_drift": explanation.get("observed_drift"),
                    "query": explanation.get("query"),
                    "matched_message_id": explanation.get("matched_message_id"),
                },
            }),
            "candidate": candidate,
        }
    if derived_class == "memory_item":
        raise ValueError("caller_authored_no_deterministic_recipe")
    if derived_class == "episode":
        raise ValueError("caller_authored_no_deterministic_recipe")
    raise ValueError("unsupported_class")


async def append_replay_lifecycle(
    pg: Any,
    *,
    derived_class: str,
    derived_id: UUID,
    owner_id: str,
    event: dict[str, Any],
) -> dict[str, Any] | None:
    if derived_class == "derived_text":
        if hasattr(pg, "append_derived_text_lifecycle_event"):
            return await pg.append_derived_text_lifecycle_event(derived_text_id=derived_id, owner_id=owner_id, event=event)
        row = await load_derived_row(pg, derived_class=derived_class, derived_id=derived_id, owner_id=owner_id)
        if row is None:
            return None
        params = row.get("derivation_params") if isinstance(row.get("derivation_params"), dict) else {}
        updated, _ = append_lifecycle_event(params, event)
        return await pg.update_derived_text_params(derived_text_id=derived_id, owner_id=owner_id, derivation_params=updated)
    if derived_class == "proactive_suggestion":
        if hasattr(pg, "append_proactive_suggestion_lifecycle_event"):
            return await pg.append_proactive_suggestion_lifecycle_event(suggestion_id=derived_id, owner_id=owner_id, event=event)
        row = await load_derived_row(pg, derived_class=derived_class, derived_id=derived_id, owner_id=owner_id)
        if row is None:
            return None
        evidence = row.get("evidence_json") if isinstance(row.get("evidence_json"), dict) else {}
        updated, _ = append_lifecycle_event(evidence, event)
        return await pg.update_proactive_suggestion_evidence(suggestion_id=derived_id, owner_id=owner_id, evidence_json=updated)
    reason_json = {**event, "terminal_result": event.get("result")}
    terminal_result = event.get("result") or (event.get("metadata") or {}).get("result")
    is_terminal_failure = event.get("event_type") == "rebuild_terminal" and terminal_result in {"unsupported", "failed"}
    if derived_class == "memory_item":
        if is_terminal_failure and hasattr(pg, "transition_memory_item"):
            current = await load_derived_row(pg, derived_class=derived_class, derived_id=derived_id, owner_id=owner_id)
            if current is None:
                return None
            if current.get("status") == "active":
                transitioned = await pg.transition_memory_item(
                    memory_id=derived_id,
                    owner_id=owner_id,
                    new_status="invalidated",
                    reason_code=str(event.get("failure_reason") or terminal_result),
                    reason_metadata={
                        "terminal_result": terminal_result,
                        "failure_reason": event.get("failure_reason"),
                    },
                    request_id=str(event.get("request_id")),
                    related_memory_id=None,
                )
                if transitioned is not None:
                    row = transitioned["memory"]
                    debug = await pg.get_memory_debug(derived_id, owner_id)
                    row["_events"] = (debug or {}).get("events") or []
                    return row
        if hasattr(pg, "append_memory_lifecycle_event"):
            debug = await pg.append_memory_lifecycle_event(
                memory_id=derived_id,
                owner_id=owner_id,
                event_type="state_changed",
                reason_json=reason_json,
            )
            if debug is not None:
                row = debug["memory"]
                row["_events"] = debug.get("events") or []
                return row
    if derived_class == "episode":
        if is_terminal_failure and hasattr(pg, "transition_episode_status"):
            current = await load_derived_row(pg, derived_class=derived_class, derived_id=derived_id, owner_id=owner_id)
            if current is None:
                return None
            if current.get("status") == "active":
                transitioned = await pg.transition_episode_status(
                    episode_id=derived_id,
                    owner_id=owner_id,
                    new_status="invalidated",
                    request_id=str(event.get("request_id")),
                    reason_json=reason_json,
                )
                if transitioned is not None:
                    row = transitioned["episode"]
                    debug = await pg.get_episode_debug(derived_id, owner_id)
                    row["_events"] = (debug or {}).get("events") or []
                    row["_links"] = (debug or {}).get("links") or []
                    return row
        if hasattr(pg, "append_episode_lifecycle_event"):
            debug = await pg.append_episode_lifecycle_event(
                episode_id=derived_id,
                owner_id=owner_id,
                event_type="updated",
                reason_json=reason_json,
            )
            if debug is not None:
                row = debug["episode"]
                row["_events"] = debug.get("events") or []
                row["_links"] = debug.get("links") or []
                return row
    return await load_derived_row(pg, derived_class=derived_class, derived_id=derived_id, owner_id=owner_id)


async def finalize_replay_result(
    pg: Any,
    *,
    derived_class: str,
    derived_id: UUID,
    owner_id: str,
    inspection: dict[str, Any],
    request_id: str,
    persist_replacement: bool,
    result: str,
    current: dict[str, Any],
    candidate: dict[str, Any] | None = None,
    replacement_id: str | None = None,
    failure_reason: str | None = None,
) -> dict[str, Any]:
    if persist_replacement:
        terminal = replay_event(
            request_id=request_id,
            event_type="rebuild_terminal",
            reason_code="rebuild_terminal",
            result=result,
            replacement_id=replacement_id,
            failure_reason=failure_reason,
            metadata={
                "result": result,
                "replacement_id": replacement_id,
                "failure_reason": failure_reason,
            },
        )
        saved = await append_replay_lifecycle(pg, derived_class=derived_class, derived_id=derived_id, owner_id=owner_id, event=terminal)
        if saved is not None:
            inspection = inspect_row(derived_class=derived_class, row=saved)
    replay = {
        "request_id": request_id,
        "mode": "persist" if persist_replacement else "validation_only",
        "result": result,
        "current": current,
        "replacement_id": replacement_id,
    }
    if candidate is not None:
        replay["candidate"] = candidate
    if failure_reason:
        replay["failure_reason"] = failure_reason
    return {**inspection, "replay": replay}


async def replay_derived(
    pg: Any,
    *,
    derived_class: str,
    derived_id: UUID,
    owner_id: str,
    request_id: str,
    requested_derivation_version: str | None,
    persist_replacement: bool,
    expected_current_derivation_version: str | None = None,
) -> dict[str, Any] | None:
    row = await load_derived_row(pg, derived_class=derived_class, derived_id=derived_id, owner_id=owner_id)
    if row is None:
        return None
    inspection = inspect_row(derived_class=derived_class, row=row)
    classification = inspection["rebuildability"]
    current = inspection["structural_snapshot"]
    prior_terminal = terminal_for_request(derived_class, row, request_id)
    if prior_terminal is not None:
        return {
            **inspection,
            "replay": {
                "request_id": request_id,
                "mode": "persist" if persist_replacement else "validation_only",
                "result": prior_terminal.get("result") or prior_terminal.get("terminal_result"),
                "failure_reason": prior_terminal.get("failure_reason"),
                "replacement_id": prior_terminal.get("replacement_id"),
                "current": current,
                "idempotent_replay": True,
            },
        }
    target_version, version_error = validate_replay_versions(
        derived_class=derived_class,
        current_version=current_version_for(derived_class, row, inspection),
        requested_version=requested_derivation_version,
        expected_current_version=expected_current_derivation_version,
    )
    if version_error is not None:
        return {
            **inspection,
            "replay": {
                "request_id": request_id,
                "mode": "persist" if persist_replacement else "validation_only",
                "current": current,
                **version_error,
            },
        }
    if persist_replacement:
        start = replay_event(
            request_id=request_id,
            event_type="rebuilding",
            reason_code="rebuild_started",
            metadata={
                "requested_derivation_version": target_version,
                "current_derivation_version": current_version_for(derived_class, row, inspection),
                "source_ref_hash": current.get("source_ref_hash"),
            },
        )
        await append_replay_lifecycle(pg, derived_class=derived_class, derived_id=derived_id, owner_id=owner_id, event=start)
    try:
        candidate = await build_candidate_snapshot(pg, derived_class=derived_class, row=row, requested_version=target_version)
    except KeyError as exc:
        return await finalize_replay_result(
            pg,
            derived_class=derived_class,
            derived_id=derived_id,
            owner_id=owner_id,
            inspection=inspection,
            request_id=request_id,
            persist_replacement=persist_replacement,
            result="failed",
            current=current,
            failure_reason=str(exc).strip("'"),
        )
    except ValueError as exc:
        return await finalize_replay_result(
            pg,
            derived_class=derived_class,
            derived_id=derived_id,
            owner_id=owner_id,
            inspection=inspection,
            request_id=request_id,
            persist_replacement=persist_replacement,
            result="unsupported",
            current=current,
            failure_reason=str(exc),
        )
    same = current.get("normalized_output_hash") == candidate.get("normalized_output_hash") and current.get("derivation_version") == candidate.get("derivation_version")
    result = "identical" if same else "replaced"
    replacement_id = None
    if persist_replacement and result == "replaced":
        try:
            if classification == "replay_only":
                result = "unsupported"
            elif derived_class == "memory_item":
                recipe = candidate["candidate"]
                created = await pg.promote_memory_item(
                    owner_id=owner_id,
                    memory_type=recipe.get("memory_type") or row.get("memory_type"),
                    summary=recipe["summary"],
                    source_refs_json=candidate["source_refs"],
                    source_ref_hash=candidate["source_ref_hash"],
                    scores_json=normalize_scores(recipe.get("scores") or {}),
                    promotion_state="promoted",
                    confidence=recipe.get("confidence"),
                    explanation_json={"derivation_recipe": recipe, "rebuild": {"request_id": request_id}},
                    generation_trace_id=request_id,
                    expires_at=recipe.get("expires_at"),
                    request_id=request_id,
                    reinforce=False,
                    supersedes_memory_id=derived_id,
                    derivation_version=candidate["derivation_version"],
                )
                replacement_id = created["memory"]["memory_id"]
            elif derived_class == "episode":
                recipe = candidate["candidate"]
                created = await pg.replace_episode(
                    old_episode_id=derived_id,
                    owner_id=owner_id,
                    request_id=request_id,
                    title=recipe["title"],
                    summary=recipe["summary"],
                    episode_type=recipe.get("episode_type") or row.get("episode_type"),
                    trigger_json=normalize_json_map(recipe.get("trigger") or {}),
                    outcome=recipe.get("outcome"),
                    significance=recipe.get("significance"),
                    unresolved_json=normalize_json_map(recipe.get("unresolved") or {}),
                    source_refs_json=candidate["source_refs"],
                    source_ref_hash=candidate["source_ref_hash"],
                    episode_key=candidate["stable_object_key"],
                    callback_candidates_json=normalize_json_list(recipe.get("callback_candidates") or []),
                    time_window_json=normalize_json_map(recipe.get("time_window") or {}),
                    participants_json=normalize_json_list(recipe.get("participants") or []),
                    derivation_version=candidate["derivation_version"],
                    confidence=recipe.get("confidence"),
                    explanation_json={"derivation_recipe": recipe, "rebuild": {"request_id": request_id}},
                    generation_trace_id=request_id,
                )
                replacement_id = created["episode"]["episode_id"]
            elif derived_class == "derived_text":
                params = row.get("derivation_params") or {}
                inherited = {key: value for key, value in params.items() if key not in {"lifecycle", "replacement_id"}}
                new_params = {
                    **inherited,
                    "derivation_version": candidate["derivation_version"],
                    "generation_trace_id": request_id,
                    "replacement_for": str(derived_id),
                    "status": "active",
                }
                if hasattr(pg, "replace_derived_text_atomically"):
                    replacement = await pg.replace_derived_text_atomically(
                        predecessor_derived_text_id=derived_id,
                        owner_id=owner_id,
                        request_id=request_id,
                        kind=row.get("kind") or "chunk",
                        text=candidate["candidate"]["text"],
                        language=row.get("language"),
                        derivation_params=new_params,
                    )
                    if replacement is None:
                        raise KeyError("derived_text_predecessor_missing")
                    replacement_id = replacement["replacement"]["derived_text_id"]
                else:
                    replacement = await pg.create_derived_text(
                        artifact_id=UUID(row["artifact_id"]),
                        kind=row.get("kind") or "chunk",
                        text=candidate["candidate"]["text"],
                        language=row.get("language"),
                        derivation_params=new_params,
                    )
                    replacement_id = replacement["derived_text_id"]
                    event = replay_event(request_id=request_id, event_type="superseded", reason_code="rebuild_replaced", replacement_id=replacement_id)
                    await append_replay_lifecycle(pg, derived_class=derived_class, derived_id=derived_id, owner_id=owner_id, event=event)
        except Exception as exc:
            failure_reason = str(exc) or type(exc).__name__
            return await finalize_replay_result(
                pg,
                derived_class=derived_class,
                derived_id=derived_id,
                owner_id=owner_id,
                inspection=inspection,
                request_id=request_id,
                persist_replacement=persist_replacement,
                result="failed",
                current=current,
                candidate={k: v for k, v in candidate.items() if k != "candidate"},
                failure_reason=failure_reason,
            )
    return await finalize_replay_result(
        pg,
        derived_class=derived_class,
        derived_id=derived_id,
        owner_id=owner_id,
        inspection=inspection,
        request_id=request_id,
        persist_replacement=persist_replacement,
        result=result,
        current=current,
        candidate={k: v for k, v in candidate.items() if k != "candidate"},
        replacement_id=replacement_id,
    )
