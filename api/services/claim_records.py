from __future__ import annotations

import re
from hashlib import sha256
from typing import Any, Callable, Protocol
from uuid import UUID

from models import ClaimRecord, ClaimRecordCreateRequest


class ClaimRecordError(RuntimeError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class ClaimRecordStore(Protocol):
    async def create_claim_record(
        self,
        *,
        record: dict[str, Any],
        validate_association: Callable[
            [dict[str, Any], dict[str, Any]],
            dict[str, Any] | None,
        ],
    ) -> dict[str, Any]: ...

    async def get_claim_record(
        self,
        *,
        claim_id: str,
        owner_id: str,
        conversation_id: str,
    ) -> dict[str, Any] | None: ...

    async def list_claim_records(
        self,
        *,
        owner_id: str,
        conversation_id: str,
        assistant_message_id: str | None,
        request_id: str | None,
        limit: int,
    ) -> list[dict[str, Any]]: ...


_PARAGRAPH_SEPARATOR = re.compile(r"\r?\n[ \t]*\r?\n")


def _assistant_response_digest(content: str) -> str:
    return "sha256:" + sha256(content.encode("utf-8")).hexdigest()


def _normalized_first_response_paragraph(content: Any) -> str | None:
    if not isinstance(content, str) or not content:
        return None
    first_paragraph = _PARAGRAPH_SEPARATOR.split(content, maxsplit=1)[0]
    normalized = " ".join(first_paragraph.split())
    return normalized or None


def normalize_trace_reference_identity(value: Any) -> tuple[str, str] | None:
    if not isinstance(value, dict):
        return None
    if not {"ref_type", "ref_id"} <= set(value):
        return None
    ref_type = value.get("ref_type")
    ref_id = value.get("ref_id")
    if not isinstance(ref_type, str) or not isinstance(ref_id, str):
        return None
    ref_type = ref_type.strip()
    ref_id = ref_id.strip()
    if not ref_type or not ref_id:
        return None
    if ref_type not in {
        "message",
        "derived_text",
        "artifact",
        "external_source",
        "world_state_claim",
        "tool_output",
        "integration_event",
    }:
        return None
    if len(ref_type) > 64 or len(ref_id) > 120:
        return None
    return ref_type, ref_id


def _canonical_record(body: ClaimRecordCreateRequest) -> dict[str, Any]:
    result = body.calibration_result
    expected_digest = "sha256:" + sha256(result.claim_anchor.encode("utf-8")).hexdigest()
    if result.claim_anchor_digest != expected_digest:
        raise ClaimRecordError("claim_anchor_digest_mismatch")
    try:
        conversation_id = str(UUID(body.conversation_id))
    except ValueError as exc:
        raise ClaimRecordError("conversation_not_found") from exc
    try:
        assistant_message_id = str(UUID(body.assistant_message_id))
    except ValueError as exc:
        raise ClaimRecordError("assistant_message_not_found") from exc
    for reference in result.validated_evidence_references:
        if reference.ref_type not in {"message", "artifact", "derived_text"}:
            continue
        try:
            UUID(reference.ref_id)
        except ValueError as exc:
            raise ClaimRecordError("evidence_reference_not_found") from exc
    return {
        "claim_id": result.claim_id,
        "schema_version": body.schema_version,
        "owner_id": body.owner_id,
        "conversation_id": conversation_id,
        "request_id": body.request_id,
        "assistant_message_id": assistant_message_id,
        "surface": body.surface,
        "runtime_session_id": body.runtime_session_id,
        "runtime_turn_id": body.runtime_turn_id,
        "acquisition_manifest_id": body.acquisition_manifest_id,
        "presented_to_user": body.presented_to_user,
        "claim_anchor": result.claim_anchor,
        "claim_anchor_digest": result.claim_anchor_digest,
        "claim_class": result.claim_class,
        "calibration_status": result.calibration_status,
        "evidence_strength": result.evidence_strength,
        "confidence": result.confidence,
        "strongest_authority": result.strongest_authority,
        "freshness_summary": result.freshness_summary,
        "uncertainty_disclosure_required": result.uncertainty_disclosure_required,
        "validated_evidence_references": [
            {
                **reference.model_dump(mode="json"),
                "conversation_id": (
                    conversation_id
                    if reference.conversation_id is not None
                    else None
                ),
            }
            for reference in result.validated_evidence_references
        ],
        "limitation_codes": list(result.limitation_codes),
        "user_safe_summary": result.user_safe_summary,
        "support": (
            body.support.model_dump(mode="json")
            if body.support is not None
            else None
        ),
    }


def validate_claim_record_association(
    record: dict[str, Any],
    association: dict[str, Any],
) -> dict[str, Any] | None:
    existing = association.get("existing")
    if existing is not None:
        comparable = {
            key: value
            for key, value in existing.items()
            if key != "created_at"
        }
        if comparable != record:
            raise ClaimRecordError("claim_record_conflict")
        return existing

    conversation = association.get("conversation")
    if conversation is None or conversation.get("owner_id") != record["owner_id"]:
        raise ClaimRecordError("conversation_not_found")

    message = association.get("assistant_message")
    if (
        message is None
        or message.get("owner_id") != record["owner_id"]
        or message.get("conversation_id") != record["conversation_id"]
    ):
        raise ClaimRecordError("assistant_message_not_found")
    if message.get("role") != "assistant":
        raise ClaimRecordError("assistant_message_not_assistant")
    metadata = message.get("metadata")
    if not isinstance(metadata, dict) or metadata.get("request_id") != record["request_id"]:
        raise ClaimRecordError("assistant_message_request_mismatch")

    trace = association.get("trace")
    if trace is None:
        raise ClaimRecordError("request_trace_not_found")
    if (
        trace.get("owner_id") != record["owner_id"]
        or trace.get("conversation_id") != record["conversation_id"]
        or trace.get("surface") != record["surface"]
    ):
        raise ClaimRecordError("request_trace_scope_mismatch")
    if trace.get("status") not in {"ok", "degraded"}:
        raise ClaimRecordError("request_trace_not_eligible")

    acquisition_manifest_id = record.get("acquisition_manifest_id")
    if acquisition_manifest_id is not None:
        prompt = trace.get("prompt")
        if not isinstance(prompt, dict):
            raise ClaimRecordError("acquisition_manifest_not_in_trace")
        manifest = prompt.get("evidence_acquisition")
        if not isinstance(manifest, dict):
            raise ClaimRecordError("acquisition_manifest_not_in_trace")
        retained_manifest_id = manifest.get("manifest_id")
        if not isinstance(retained_manifest_id, str) or not retained_manifest_id:
            raise ClaimRecordError("acquisition_manifest_not_in_trace")
        if retained_manifest_id != acquisition_manifest_id:
            raise ClaimRecordError("acquisition_manifest_association_mismatch")
        message_content = message.get("content")
        association_invalid = (
            manifest.get("assistant_message_id") != record["assistant_message_id"]
            or not isinstance(message_content, str)
            or manifest.get("response_digest")
            != _assistant_response_digest(message_content)
        )
        if record["schema_version"] == "claim-record.v1":
            association_invalid = association_invalid or (
                _normalized_first_response_paragraph(message_content)
                != " ".join(record["claim_anchor"].split())
            )
        if association_invalid:
            raise ClaimRecordError("acquisition_manifest_association_mismatch")

        accepted_statuses = {
            "sufficient_for_declared_scope",
            "sufficient_with_limitations",
        }
        plan = manifest.get("plan")
        sufficiency = manifest.get("sufficiency")
        top_level_status = manifest.get("status")
        nested_status = (
            sufficiency.get("status")
            if isinstance(sufficiency, dict)
            else None
        )
        if record["schema_version"] == "claim-record.v1" and (
            manifest.get("attempted") is not True
            or not isinstance(plan, dict)
            or plan.get("plan_status") not in {"ready", "ready_with_limitations"}
            or top_level_status not in accepted_statuses
            or nested_status not in accepted_statuses
            or top_level_status != nested_status
        ):
            raise ClaimRecordError("acquisition_manifest_not_eligible")

    if record["schema_version"] == "claim-record.v2":
        prompt = trace.get("prompt")
        reasoning = (
            prompt.get("general_evidence_reasoning")
            if isinstance(prompt, dict)
            else None
        )
        support = record.get("support")
        association_invalid = (
            not isinstance(reasoning, dict)
            or not isinstance(support, dict)
            or reasoning.get("claim_digest") != record["claim_anchor_digest"]
            or reasoning.get("runtime_session_id") != record["runtime_session_id"]
            or reasoning.get("runtime_turn_id") != record["runtime_turn_id"]
            or reasoning.get("presented_to_user")
            is not record["presented_to_user"]
        )
        if record["presented_to_user"]:
            association_invalid = association_invalid or (
                _normalized_first_response_paragraph(message.get("content"))
                != " ".join(record["claim_anchor"].split())
            )
        if association_invalid:
            raise ClaimRecordError("shadow_claim_not_in_trace")

    traced_identities = {
        identity
        for value in trace.get("references", [])
        if (identity := normalize_trace_reference_identity(value)) is not None
    }
    submitted_identities = {
        (reference["ref_type"], reference["ref_id"])
        for reference in record["validated_evidence_references"]
    }
    if not submitted_identities <= traced_identities:
        raise ClaimRecordError("evidence_reference_not_in_trace")

    local_references = association.get("local_references", {})
    for reference in record["validated_evidence_references"]:
        identity = (reference["ref_type"], reference["ref_id"])
        if reference["ref_type"] not in {"message", "artifact", "derived_text"}:
            continue
        local = local_references.get(identity)
        if local is None:
            raise ClaimRecordError("evidence_reference_not_found")
        if local.get("owner_id") != record["owner_id"]:
            raise ClaimRecordError("evidence_reference_scope_mismatch")
        local_conversation = local.get("conversation_id")
        if reference["ref_type"] == "message":
            if local_conversation != record["conversation_id"]:
                raise ClaimRecordError("evidence_reference_scope_mismatch")
        elif (
            local_conversation is not None
            and local_conversation != record["conversation_id"]
        ):
            raise ClaimRecordError("evidence_reference_scope_mismatch")
    return None


async def create_claim_record(
    store: ClaimRecordStore,
    body: ClaimRecordCreateRequest,
) -> tuple[bool, ClaimRecord]:
    stored = await store.create_claim_record(
        record=_canonical_record(body),
        validate_association=validate_claim_record_association,
    )
    return bool(stored["created"]), ClaimRecord(**stored["record"])


async def get_claim_record(
    store: ClaimRecordStore,
    *,
    claim_id: str,
    owner_id: str,
    conversation_id: str,
) -> ClaimRecord:
    stored = await store.get_claim_record(
        claim_id=claim_id,
        owner_id=owner_id,
        conversation_id=conversation_id,
    )
    if stored is None:
        raise ClaimRecordError("claim_record_not_found")
    return ClaimRecord(**stored)


async def list_claim_records(
    store: ClaimRecordStore,
    *,
    owner_id: str,
    conversation_id: str,
    assistant_message_id: str | None,
    request_id: str | None,
    limit: int,
) -> list[ClaimRecord]:
    stored = await store.list_claim_records(
        owner_id=owner_id,
        conversation_id=conversation_id,
        assistant_message_id=assistant_message_id,
        request_id=request_id,
        limit=limit,
    )
    return [ClaimRecord(**record) for record in stored]
