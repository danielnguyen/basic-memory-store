from __future__ import annotations

from typing import Any, Callable

from services.derivation_versions import EPISODE_DERIVATION_VERSION, MEMORY_ITEM_DERIVATION_VERSION


ACTIVE_STATUS = "active"
DEFAULT_DERIVATION_VERSION = "v1"
MAX_SOURCE_REFS = 50
MAX_EXPLANATION_CHARS = 500


def _required_text(row: dict[str, Any], key: str) -> str:
    value = row.get(key)
    if value is None or not str(value).strip():
        raise ValueError(f"derived object row is missing required field: {key}")
    return str(value).strip()


def _required_existing_id(row: dict[str, Any], *keys: str) -> str:
    for key in keys:
        value = row.get(key)
        if value is not None and str(value).strip():
            return str(value).strip()
    raise ValueError(f"derived object row is missing an identifier; expected one of: {', '.join(keys)}")


def _bounded_optional_text(value: Any, *, max_chars: int = MAX_EXPLANATION_CHARS) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    return text[:max_chars]


def _bounded_metadata(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    out: dict[str, Any] = {}
    for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))[:8]:
        if not isinstance(key, str) or not key.strip():
            continue
        if isinstance(item, (str, int, float, bool)) or item is None:
            out[key.strip()[:64]] = item[:160] if isinstance(item, str) else item
    return out


def normalize_contract_source_refs(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list) or not value:
        raise ValueError("derived object row is missing required field: source_refs")
    if len(value) > MAX_SOURCE_REFS:
        raise ValueError("derived object source_refs exceed the supported bound")

    normalized: list[dict[str, Any]] = []
    for raw in value:
        if not isinstance(raw, dict):
            raise ValueError("derived object source_ref must be an object")
        ref_type = str(raw.get("ref_type") or "").strip()
        ref_id = str(raw.get("ref_id") or "").strip()
        support_kind = str(raw.get("support_kind") or "direct").strip()
        if not ref_type or not ref_id or not support_kind:
            raise ValueError("derived object source_ref requires ref_type, ref_id, and support_kind")
        if len(ref_type) > 64 or len(ref_id) > 160 or len(support_kind) > 64:
            raise ValueError("derived object source_ref exceeds the supported bound")
        item: dict[str, Any] = {
            "ref_type": ref_type,
            "ref_id": ref_id,
            "support_kind": support_kind,
        }
        for key in ("span", "field_path", "note"):
            bounded = _bounded_optional_text(raw.get(key), max_chars=160)
            if bounded is not None:
                item[key] = bounded
        metadata = _bounded_metadata(raw.get("metadata"))
        if metadata:
            item["metadata"] = metadata
        normalized.append(item)

    return sorted(
        normalized,
        key=lambda item: (
            item["ref_type"],
            item["ref_id"],
            item["support_kind"],
            str(item),
        ),
    )


def source_ref(ref_type: str, ref_id: str, *, support_kind: str = "direct", **extra: Any) -> dict[str, Any]:
    return normalize_contract_source_refs(
        [{
            "ref_type": ref_type,
            "ref_id": ref_id,
            "support_kind": support_kind,
            **extra,
        }]
    )[0]


def _contract(
    *,
    row: dict[str, Any],
    id_keys: tuple[str, ...],
    derivation_type: str,
    source_refs: Any,
    derivation_version: Any,
    default_derivation_version: str = DEFAULT_DERIVATION_VERSION,
    status: Any,
    confidence: Any,
    explanation: Any,
    generation_trace_id: Any,
    effective_status: Any = None,
) -> dict[str, Any]:
    compatibility_defaults: list[str] = []
    if derivation_version is None or not str(derivation_version).strip():
        derivation_version = default_derivation_version
        compatibility_defaults.append("derivation_version")
    if status is None or not str(status).strip():
        status = ACTIVE_STATUS
        compatibility_defaults.append("status")

    confidence_value = float(confidence) if isinstance(confidence, (int, float)) else None
    return {
        "derived_id": _required_existing_id(row, *id_keys),
        "owner_id": _required_text(row, "owner_id"),
        "derivation_type": _bounded_optional_text(derivation_type, max_chars=64),
        "source_refs": normalize_contract_source_refs(source_refs),
        "derivation_version": str(derivation_version).strip(),
        "created_at": _required_text(row, "created_at"),
        "status": str(status).strip(),
        "effective_status": _bounded_optional_text(effective_status, max_chars=64),
        "confidence": confidence_value,
        "explanation": _bounded_optional_text(explanation),
        "generation_trace_id": _bounded_optional_text(generation_trace_id, max_chars=160),
        "compatibility_defaults": compatibility_defaults,
        "provenance_status": "complete",
    }


def derived_text_contract_view(row: dict[str, Any]) -> dict[str, Any]:
    """Bounded adapter view for a stored derived_text row joined to its artifact owner."""
    params = row.get("derivation_params") if isinstance(row.get("derivation_params"), dict) else {}
    artifact_id = row.get("artifact_id")
    refs = params.get("source_refs")
    if not refs and artifact_id:
        refs = [source_ref("artifact", str(artifact_id))]
    return _contract(
        row=row,
        id_keys=("derived_text_id", "id"),
        derivation_type=row.get("kind") or params.get("derivation_type") or "derived_text",
        source_refs=refs,
        derivation_version=params.get("derivation_version"),
        status=params.get("status"),
        confidence=params.get("confidence"),
        explanation=params.get("explanation"),
        generation_trace_id=params.get("generation_trace_id"),
    )


def proactive_suggestion_contract_view(row: dict[str, Any]) -> dict[str, Any]:
    evidence = row.get("evidence_json") if isinstance(row.get("evidence_json"), dict) else {}
    explanation = row.get("explanation_json") if isinstance(row.get("explanation_json"), dict) else {}
    lifecycle = evidence.get("lifecycle") if isinstance(evidence.get("lifecycle"), dict) else {}
    refs = evidence.get("source_refs")
    event_id = row.get("source_event_log_id")
    if not refs and event_id:
        refs = [source_ref("event_log", str(event_id))]
    return _contract(
        row=row,
        id_keys=("suggestion_id", "id"),
        derivation_type=row.get("kind") or "proactive_suggestion",
        source_refs=refs,
        derivation_version=explanation.get("derivation_version"),
        status=row.get("status"),
        effective_status=lifecycle.get("status"),
        confidence=explanation.get("confidence"),
        explanation=explanation.get("rationale") or explanation.get("explanation") or explanation.get("because"),
        generation_trace_id=explanation.get("generation_trace_id"),
    )


def memory_item_contract_view(row: dict[str, Any]) -> dict[str, Any]:
    explanation = row.get("explanation_json") if isinstance(row.get("explanation_json"), dict) else {}
    return _contract(
        row=row,
        id_keys=("memory_id", "id"),
        derivation_type=row.get("memory_type") or "memory_item",
        source_refs=row.get("source_refs_json"),
        derivation_version=row.get("derivation_version"),
        default_derivation_version=MEMORY_ITEM_DERIVATION_VERSION,
        status=row.get("status"),
        effective_status=row.get("freshness_state"),
        confidence=row.get("confidence"),
        explanation=explanation.get("rationale") or explanation.get("explanation"),
        generation_trace_id=row.get("generation_trace_id"),
    )


def episode_contract_view(row: dict[str, Any]) -> dict[str, Any]:
    explanation = row.get("explanation_json") if isinstance(row.get("explanation_json"), dict) else {}
    return _contract(
        row=row,
        id_keys=("episode_id", "id"),
        derivation_type=row.get("episode_type") or "episode",
        source_refs=row.get("source_refs_json"),
        derivation_version=row.get("derivation_version"),
        default_derivation_version=EPISODE_DERIVATION_VERSION,
        status=row.get("status"),
        confidence=row.get("confidence"),
        explanation=explanation.get("rationale") or explanation.get("explanation"),
        generation_trace_id=row.get("generation_trace_id"),
    )


CONTRACT_ADAPTERS: dict[str, Callable[[dict[str, Any]], dict[str, Any]]] = {
    "derived_text": derived_text_contract_view,
    "proactive_suggestion": proactive_suggestion_contract_view,
    "memory_item": memory_item_contract_view,
    "episode": episode_contract_view,
}
