from __future__ import annotations

from typing import Any


ACTIVE_STATUS = "active"
DEFAULT_DERIVATION_VERSION = "v1"


def source_ref(ref_type: str, ref_id: str, *, support_kind: str = "direct", **extra: Any) -> dict[str, Any]:
    ref = {
        "ref_type": ref_type,
        "ref_id": ref_id,
        "support_kind": support_kind,
    }
    ref.update({k: v for k, v in extra.items() if v is not None})
    return ref


def derived_text_contract_view(row: dict[str, Any]) -> dict[str, Any]:
    """Adapter view for existing derived_text rows; does not imply a schema migration."""
    params = row.get("derivation_params") if isinstance(row.get("derivation_params"), dict) else {}
    artifact_id = row.get("artifact_id")
    source_refs = params.get("source_refs")
    if not source_refs and artifact_id:
        source_refs = [source_ref("artifact", str(artifact_id))]

    return {
        "derived_id": str(row.get("derived_text_id") or row.get("id")),
        "owner_id": row.get("owner_id"),
        "derivation_type": row.get("kind") or params.get("derivation_type") or "derived_text",
        "source_refs": source_refs or [],
        "derivation_version": params.get("derivation_version") or DEFAULT_DERIVATION_VERSION,
        "created_at": row.get("created_at"),
        "status": row.get("status") or ACTIVE_STATUS,
        "confidence": params.get("confidence"),
        "explanation": params.get("explanation"),
        "generation_trace_id": params.get("generation_trace_id"),
    }


def proactive_suggestion_contract_view(row: dict[str, Any]) -> dict[str, Any]:
    evidence = row.get("evidence_json") if isinstance(row.get("evidence_json"), dict) else {}
    explanation = row.get("explanation_json") if isinstance(row.get("explanation_json"), dict) else {}
    source_refs = evidence.get("source_refs")
    event_id = row.get("source_event_log_id")
    if not source_refs and event_id:
        source_refs = [source_ref("event_log", str(event_id))]

    return {
        "derived_id": str(row.get("suggestion_id") or row.get("id")),
        "owner_id": row.get("owner_id"),
        "derivation_type": row.get("kind") or "proactive_suggestion",
        "source_refs": source_refs or [],
        "derivation_version": explanation.get("derivation_version") or DEFAULT_DERIVATION_VERSION,
        "created_at": row.get("created_at"),
        "status": row.get("status") or ACTIVE_STATUS,
        "confidence": explanation.get("confidence"),
        "explanation": explanation.get("rationale") or explanation.get("explanation"),
        "generation_trace_id": explanation.get("generation_trace_id"),
    }
