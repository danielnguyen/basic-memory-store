from __future__ import annotations

from hashlib import sha256
import json
from pathlib import Path
from typing import Any
from uuid import UUID

from services.chunking import chunk_text
from services.derived_contract import CONTRACT_ADAPTERS, normalize_contract_source_refs
from services.episodes import (
    DEFAULT_DERIVATION_VERSION as EPISODE_DERIVATION_VERSION,
    episode_key,
    normalize_json_list,
    normalize_json_map,
    source_ref_hash as episode_source_ref_hash,
)
from services.memory_items import (
    DEFAULT_DERIVATION_VERSION as MEMORY_DERIVATION_VERSION,
    normalize_scores,
    source_ref_hash,
)
from services.proactive import PROACTIVE_DERIVATION_VERSION


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
    return {
        **metadata,
        "lifecycle": {
            **lifecycle,
            "status": event["event_type"] if event["event_type"] in {"invalidated", "rebuilding"} else lifecycle.get("status"),
            "invalidated_reason": event["reason_code"] if event["event_type"] == "invalidated" else lifecycle.get("invalidated_reason"),
            "last_request_id": event["request_id"],
            "events": updated_events,
        },
    }, True


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
        if row.get("artifact_id") and params.get("derivation_version") == "file-chunk-v1":
            return {"classification": "rebuildable", "reason": "artifact-backed deterministic chunk derivation"}
        return {"classification": "not_rebuildable", "reason": "missing artifact chunk recipe"}
    if derived_class == "proactive_suggestion":
        return {"classification": "replay_only", "reason": "proactive rules are deterministic but replay must not repeat delivery effects"}
    if derived_class == "memory_item":
        explanation = row.get("explanation_json") if isinstance(row.get("explanation_json"), dict) else {}
        recipe = explanation.get("derivation_recipe") if isinstance(explanation.get("derivation_recipe"), dict) else {}
        if recipe.get("kind") == MEMORY_RECIPE_KIND:
            return {"classification": "rebuildable", "reason": "bounded memory promotion recipe is persisted"}
        return {"classification": "not_rebuildable", "reason": "memory item lacks a deterministic promotion recipe"}
    if derived_class == "episode":
        explanation = row.get("explanation_json") if isinstance(row.get("explanation_json"), dict) else {}
        recipe = explanation.get("derivation_recipe") if isinstance(explanation.get("derivation_recipe"), dict) else {}
        if recipe.get("kind") == EPISODE_RECIPE_KIND:
            return {"classification": "rebuildable", "reason": "bounded episode construction recipe is persisted"}
        return {"classification": "not_rebuildable", "reason": "episode lacks a deterministic construction recipe"}
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
        "lifecycle_status": lifecycle.get("status") or contract.get("status"),
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
        saved = await pg.update_derived_text_params(derived_text_id=derived_id, owner_id=owner_id, derivation_params=updated)
        return {"changed": changed, "inspection": inspect_row(derived_class=derived_class, row=saved or row)}
    if derived_class == "proactive_suggestion":
        evidence = row.get("evidence_json") if isinstance(row.get("evidence_json"), dict) else {}
        updated, changed = append_lifecycle_event(evidence, event)
        saved = await pg.update_proactive_suggestion_evidence(suggestion_id=derived_id, owner_id=owner_id, evidence_json=updated)
        return {"changed": changed, "inspection": inspect_row(derived_class=derived_class, row=saved or row)}
    return None


async def build_candidate_snapshot(pg: Any, *, derived_class: str, row: dict[str, Any], requested_version: str | None) -> dict[str, Any]:
    if derived_class == "derived_text":
        params = row.get("derivation_params") if isinstance(row.get("derivation_params"), dict) else {}
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
        chunks = chunk_text(text, chunk_size=int(params.get("chunk_size") or 1200), chunk_overlap=int(params.get("chunk_overlap") or 120))
        chunk_index = int(params.get("chunk_index") or 0)
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
        return {
            "derived_class": "proactive_suggestion",
            "derivation_version": PROACTIVE_DERIVATION_VERSION,
            "stable_object_key": f"{row.get('source_event_log_id')}:{row.get('kind')}",
            "normalized_output_hash": structural_hash({
                "kind": row.get("kind"),
                "title_hash": structural_hash(str(row.get("title") or "")),
                "body_hash": structural_hash(str(row.get("body") or "")),
                "rule": (row.get("explanation_json") or {}).get("rule") if isinstance(row.get("explanation_json"), dict) else None,
                "source_event_log_id": (row.get("evidence_json") or {}).get("source_event_log_id") if isinstance(row.get("evidence_json"), dict) else None,
            }),
            "candidate": {"side_effects": "suppressed"},
        }
    if derived_class == "memory_item":
        recipe = (row.get("explanation_json") or {}).get("derivation_recipe") or {}
        if recipe.get("kind") != MEMORY_RECIPE_KIND:
            raise ValueError("unsupported_rebuild")
        refs = normalize_contract_source_refs(recipe.get("source_refs") or row.get("source_refs_json"))
        return {
            "derived_class": "memory_item",
            "derivation_version": requested_version or recipe.get("derivation_version") or MEMORY_DERIVATION_VERSION,
            "source_refs": refs,
            "source_ref_hash": source_ref_hash(refs),
            "stable_object_key": source_ref_hash(refs),
            "normalized_output_hash": structural_hash({
                "memory_type": recipe.get("memory_type") or row.get("memory_type"),
                "summary_hash": structural_hash(str(recipe.get("summary") or "")),
                "scores": normalize_scores(recipe.get("scores") or {}),
                "promotion_state": "promoted",
            }),
            "candidate": recipe,
        }
    if derived_class == "episode":
        recipe = (row.get("explanation_json") or {}).get("derivation_recipe") or {}
        if recipe.get("kind") != EPISODE_RECIPE_KIND:
            raise ValueError("unsupported_rebuild")
        refs = normalize_contract_source_refs(recipe.get("source_refs") or row.get("source_refs_json"))
        trigger = normalize_json_map(recipe.get("trigger") or row.get("trigger_json") or {})
        time_window = normalize_json_map(recipe.get("time_window") or row.get("time_window_json") or {})
        ref_hash = episode_source_ref_hash(refs)
        key = episode_key(episode_type=recipe.get("episode_type") or row.get("episode_type"), source_ref_hash_value=ref_hash, trigger_json=trigger, time_window_json=time_window)
        return {
            "derived_class": "episode",
            "derivation_version": requested_version or recipe.get("derivation_version") or EPISODE_DERIVATION_VERSION,
            "source_refs": refs,
            "source_ref_hash": ref_hash,
            "stable_object_key": key,
            "normalized_output_hash": structural_hash({
                "episode_type": recipe.get("episode_type") or row.get("episode_type"),
                "title_hash": structural_hash(str(recipe.get("title") or "")),
                "summary_hash": structural_hash(str(recipe.get("summary") or "")),
                "outcome_hash": structural_hash(str(recipe.get("outcome") or "")),
                "significance_hash": structural_hash(str(recipe.get("significance") or "")),
                "unresolved": normalize_json_map(recipe.get("unresolved") or {}),
                "callback_count": len(recipe.get("callback_candidates") or []),
                "participant_count": len(recipe.get("participants") or []),
            }),
            "candidate": recipe,
        }
    raise ValueError("unsupported_class")


async def replay_derived(
    pg: Any,
    *,
    derived_class: str,
    derived_id: UUID,
    owner_id: str,
    request_id: str,
    requested_derivation_version: str | None,
    persist_replacement: bool,
) -> dict[str, Any] | None:
    row = await load_derived_row(pg, derived_class=derived_class, derived_id=derived_id, owner_id=owner_id)
    if row is None:
        return None
    inspection = inspect_row(derived_class=derived_class, row=row)
    classification = inspection["rebuildability"]
    current = inspection["structural_snapshot"]
    try:
        candidate = await build_candidate_snapshot(pg, derived_class=derived_class, row=row, requested_version=requested_derivation_version)
    except KeyError as exc:
        return {**inspection, "replay": {"request_id": request_id, "mode": "persist" if persist_replacement else "validation_only", "result": "failed", "failure_reason": str(exc), "current": current}}
    except ValueError as exc:
        return {**inspection, "replay": {"request_id": request_id, "mode": "persist" if persist_replacement else "validation_only", "result": "unsupported", "failure_reason": str(exc), "current": current}}
    same = current.get("normalized_output_hash") == candidate.get("normalized_output_hash") and current.get("derivation_version") == candidate.get("derivation_version")
    result = "identical" if same else "replaced"
    replacement_id = None
    if persist_replacement and result == "replaced":
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
            new_params = {
                **params,
                "derivation_version": candidate["derivation_version"],
                "generation_trace_id": request_id,
                "replacement_for": str(derived_id),
            }
            replacement = await pg.create_derived_text(
                artifact_id=UUID(row["artifact_id"]),
                kind=row.get("kind") or "chunk",
                text=candidate["candidate"]["text"],
                language=row.get("language"),
                derivation_params=new_params,
            )
            replacement_id = replacement["derived_text_id"]
            event = lifecycle_event(request_id=request_id, event_type="superseded", reason_code="rebuild_replaced", metadata={"replacement_id": replacement_id})
            updated, _ = append_lifecycle_event(params, event)
            await pg.update_derived_text_params(derived_text_id=derived_id, owner_id=owner_id, derivation_params=updated)
    return {
        **inspection,
        "replay": {
            "request_id": request_id,
            "mode": "persist" if persist_replacement else "validation_only",
            "result": result,
            "current": current,
            "candidate": {k: v for k, v in candidate.items() if k != "candidate"},
            "replacement_id": replacement_id,
        },
    }
