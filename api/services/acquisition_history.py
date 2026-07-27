from __future__ import annotations

import copy
import json
import math
import re
from hashlib import sha256
from typing import Any, Protocol
from uuid import UUID

from models import (
    AcquisitionHistoryRecord,
    AcquisitionHistoryResolveRequest,
    AcquisitionHistoryResolveResponse,
    ClaimRecord,
    HistoryRootLineage,
    ImmediateHistoryRecord,
    ImmediateHistoryResolveRequest,
    ImmediateHistoryResolveResponse,
    ImmediateHistoryResolveRequestV1,
    ImmediateHistoryResolveRequestV2,
    ImmediateHistoryResolveResponseV1,
    ImmediateHistoryResolveResponseV2,
)


class AcquisitionHistoryStore(Protocol):
    async def list_assistant_trace_candidates(
        self,
        *,
        owner_id: str,
        conversation_id: UUID,
        limit: int,
    ) -> list[dict[str, Any]]: ...

    async def list_claim_records(
        self,
        *,
        owner_id: str,
        conversation_id: str,
        assistant_message_id: str | None,
        request_id: str | None,
        limit: int,
    ) -> list[dict[str, Any]]: ...

    async def get_assistant_history_root(
        self,
        *,
        message_id: UUID,
    ) -> dict[str, Any] | None: ...


_PARAGRAPH_SEPARATOR = re.compile(r"\r?\n[ \t]*\r?\n")
_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,119}$")
_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_MANIFEST_REQUIRED_FIELDS = {
    "enabled",
    "attempted",
    "status",
    "manifest_id",
    "assistant_message_id",
    "response_digest",
    "shape",
    "inventory",
    "plan",
    "acquisition",
    "sufficiency",
}
_MANIFEST_OPTIONAL_FIELDS = {"next_steps"}
_PRIVATE_KEYS = {
    "text",
    "content",
    "messages",
    "prompt",
    "provider_text",
    "raw",
    "credentials",
    "secret",
    "url",
    "path",
    "exception",
    "reasoning",
}
_PRIVATE_KEY_PARTS = {
    "content",
    "credential",
    "credentials",
    "exception",
    "messages",
    "path",
    "paths",
    "private",
    "prompt",
    "raw",
    "reasoning",
    "secret",
    "secrets",
    "text",
    "url",
    "urls",
}
_PRIVATE_COMPACT_KEYS = {
    "accesstoken",
    "apikey",
    "authorization",
    "authorizationheader",
    "authtoken",
    "bearertoken",
    "clientsecret",
    "passphrase",
    "passwd",
    "password",
    "privatekey",
    "refreshtoken",
    "sessioncookie",
    "sessiontoken",
    "setcookie",
    "signingkey",
}
_ALLOWED_STRUCTURAL_KEYS = {
    "acquisition_manifest_id",
    "candidate_count",
    "context_delivery_status",
    "dsa_error_codes",
    "evaluation_id",
    "evidence_plan_id",
    "item_count",
    "next_steps",
    "prompt_retained_item_count",
    "reason_codes",
    "selected_next_step",
    "selection_id",
}
_MAX_MANIFEST_BYTES = 65_536
_MAX_MANIFEST_DEPTH = 8
_MAX_COLLECTION_LENGTH = 64
_MAX_STRING_LENGTH = 1_000


def _response_digest(content: str) -> str:
    return "sha256:" + sha256(content.encode("utf-8")).hexdigest()


def _normalized_first_paragraph(content: Any) -> str | None:
    if not isinstance(content, str) or not content:
        return None
    first_paragraph = _PARAGRAPH_SEPARATOR.split(content, maxsplit=1)[0]
    normalized = " ".join(first_paragraph.split())
    if not normalized or len(normalized) > 500:
        return None
    return normalized


def _bounded_identifier(value: Any) -> str | None:
    if not isinstance(value, str) or _IDENTIFIER_RE.fullmatch(value) is None:
        return None
    return value


def _compact_key(value: str) -> str:
    return "".join(
        character for character in value.casefold() if character.isalnum()
    )


def _manifest_key_is_private(key: str) -> bool:
    normalized = key.casefold()
    if normalized in _ALLOWED_STRUCTURAL_KEYS:
        return False
    if (
        normalized in _PRIVATE_KEYS
        or _compact_key(key) in _PRIVATE_COMPACT_KEYS
    ):
        return True
    return bool(
        _PRIVATE_KEY_PARTS
        & {part for part in re.split(r"[^a-z0-9]+", normalized) if part}
    )


def _manifest_value_is_safe(value: Any, *, depth: int = 0) -> bool:
    if depth > _MAX_MANIFEST_DEPTH:
        return False
    if value is None or isinstance(value, bool):
        return True
    if isinstance(value, int):
        return abs(value) <= 1_000_000_000
    if isinstance(value, float):
        return math.isfinite(value) and abs(value) <= 1_000_000_000
    if isinstance(value, str):
        if len(value) > _MAX_STRING_LENGTH:
            return False
        lowered = value.casefold()
        return not (
            "://" in lowered
            or value.startswith(("/", "\\\\"))
            or re.match(r"^[A-Za-z]:[\\/]", value) is not None
        )
    if isinstance(value, list):
        return len(value) <= _MAX_COLLECTION_LENGTH and all(
            _manifest_value_is_safe(item, depth=depth + 1) for item in value
        )
    if isinstance(value, dict):
        if len(value) > _MAX_COLLECTION_LENGTH:
            return False
        for key, item in value.items():
            if (
                not isinstance(key, str)
                or not key
                or len(key) > 120
                or _manifest_key_is_private(key)
                or not _manifest_value_is_safe(item, depth=depth + 1)
            ):
                return False
        return True
    return False


def _validated_manifest(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    fields = set(value)
    if not _MANIFEST_REQUIRED_FIELDS <= fields or not fields <= (
        _MANIFEST_REQUIRED_FIELDS | _MANIFEST_OPTIONAL_FIELDS
    ):
        return None
    if (
        value.get("attempted") is not True
        or _bounded_identifier(value.get("manifest_id")) is None
        or _bounded_identifier(value.get("assistant_message_id")) is None
        or not isinstance(value.get("response_digest"), str)
        or _DIGEST_RE.fullmatch(value["response_digest"]) is None
    ):
        return None
    try:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    except (TypeError, ValueError, OverflowError):
        return None
    if len(encoded) > _MAX_MANIFEST_BYTES or not _manifest_value_is_safe(value):
        return None
    return copy.deepcopy(value)


def _response(
    body: AcquisitionHistoryResolveRequest,
    *,
    resolution_status: str,
    match_count: int,
    reason_code: str,
    record: AcquisitionHistoryRecord | None = None,
) -> AcquisitionHistoryResolveResponse:
    return AcquisitionHistoryResolveResponse(
        schema_version=body.schema_version,
        request_id=body.request_id,
        owner_id=body.owner_id,
        conversation_id=body.conversation_id,
        surface=body.surface,
        target_mode=body.target_mode,
        resolution_status=resolution_status,
        match_count=match_count,
        reason_code=reason_code,
        record=record,
    )


def _association_failure(
    body: AcquisitionHistoryResolveRequest,
    *,
    reason_code: str,
    match_count: int = 1,
    resolution_status: str = "invalid",
) -> AcquisitionHistoryResolveResponse:
    return _response(
        body,
        resolution_status=resolution_status,
        match_count=match_count,
        reason_code=reason_code,
    )


def _resolve_candidate(
    body: AcquisitionHistoryResolveRequest,
    candidate: dict[str, Any],
) -> AcquisitionHistoryResolveResponse:
    message_id = _bounded_identifier(candidate.get("message_id"))
    message_content = candidate.get("message_content")
    normalized_paragraph = _normalized_first_paragraph(message_content)
    exact_digest = (
        _response_digest(message_content)
        if isinstance(message_content, str) and message_content
        else None
    )
    if (
        message_id is None
        or candidate.get("message_owner_id") != body.owner_id
        or str(candidate.get("message_conversation_id"))
        != str(body.conversation_id)
        or candidate.get("message_role") != "assistant"
        or normalized_paragraph != body.normalized_first_paragraph
    ):
        return _association_failure(
            body,
            reason_code="manifest_association_invalid",
        )

    message_request_id = _bounded_identifier(
        candidate.get("message_request_id")
    )
    if message_request_id is None:
        return _association_failure(
            body,
            reason_code="assistant_message_request_mismatch",
        )

    trace_request_id = candidate.get("trace_request_id")
    if trace_request_id is None:
        reason = (
            "immediate_response_trace_absent"
            if body.target_mode == "immediate_previous"
            else "quoted_response_trace_absent"
        )
        return _association_failure(
            body,
            resolution_status="no_record",
            reason_code=reason,
        )
    if (
        _bounded_identifier(trace_request_id) is None
        or trace_request_id != message_request_id
    ):
        return _association_failure(
            body,
            reason_code="assistant_message_request_mismatch",
        )
    if (
        candidate.get("trace_owner_id") != body.owner_id
        or str(candidate.get("trace_conversation_id"))
        != str(body.conversation_id)
        or candidate.get("trace_surface") != body.surface
    ):
        return _association_failure(body, reason_code="trace_scope_mismatch")
    if candidate.get("trace_status") not in {"ok", "degraded"}:
        return _association_failure(
            body,
            reason_code="manifest_association_invalid",
        )

    prompt = candidate.get("trace_prompt")
    manifest = (
        prompt.get("evidence_acquisition")
        if isinstance(prompt, dict)
        else None
    )
    if not isinstance(manifest, dict):
        reason = (
            "immediate_response_manifest_absent"
            if body.target_mode == "immediate_previous"
            else "quoted_response_manifest_absent"
        )
        return _association_failure(
            body,
            resolution_status="no_record",
            reason_code=reason,
        )
    if (
        exact_digest is None
        or manifest.get("attempted") is not True
        or manifest.get("assistant_message_id") != message_id
        or manifest.get("response_digest") != exact_digest
    ):
        return _association_failure(
            body,
            reason_code="manifest_association_invalid",
        )

    projected_manifest = _validated_manifest(manifest)
    if projected_manifest is None:
        return _association_failure(
            body,
            reason_code="manifest_privacy_boundary_invalid",
        )

    return _response(
        body,
        resolution_status="resolved",
        match_count=1,
        reason_code=(
            "immediate_response_resolved"
            if body.target_mode == "immediate_previous"
            else "quoted_response_resolved"
        ),
        record=AcquisitionHistoryRecord(
            original_request_id=trace_request_id,
            assistant_message_id=message_id,
            surface=candidate["trace_surface"],
            trace_status=candidate["trace_status"],
            response_digest=exact_digest,
            normalized_first_paragraph=normalized_paragraph,
            acquisition_manifest=projected_manifest,
        ),
    )


async def resolve_acquisition_history(
    store: AcquisitionHistoryStore,
    body: AcquisitionHistoryResolveRequest,
) -> AcquisitionHistoryResolveResponse:
    limit = 1 if body.target_mode == "immediate_previous" else 50
    try:
        candidates = await store.list_assistant_trace_candidates(
            owner_id=body.owner_id,
            conversation_id=body.conversation_id,
            limit=limit,
        )
    except Exception:
        return _association_failure(
            body,
            resolution_status="invalid",
            match_count=0,
            reason_code="manifest_association_invalid",
        )
    if not isinstance(candidates, list):
        return _association_failure(
            body,
            resolution_status="invalid",
            match_count=0,
            reason_code="manifest_association_invalid",
        )
    candidates = candidates[:limit]

    if body.target_mode == "immediate_previous":
        if not candidates:
            return _response(
                body,
                resolution_status="no_record",
                match_count=0,
                reason_code="immediate_response_mismatch",
            )
        candidate = candidates[0]
        content = candidate.get("message_content")
        if (
            not isinstance(content, str)
            or not content
            or _response_digest(content) != body.response_digest
            or _normalized_first_paragraph(content)
            != body.normalized_first_paragraph
        ):
            return _response(
                body,
                resolution_status="no_record",
                match_count=0,
                reason_code="immediate_response_mismatch",
            )
        return _resolve_candidate(body, candidate)

    matches = [
        candidate
        for candidate in candidates
        if _normalized_first_paragraph(candidate.get("message_content"))
        == body.normalized_first_paragraph
    ]
    if not matches:
        return _response(
            body,
            resolution_status="no_record",
            match_count=0,
            reason_code="quoted_response_not_found",
        )
    if len(matches) > 1:
        return _response(
            body,
            resolution_status="ambiguous",
            match_count=len(matches),
            reason_code="quoted_response_ambiguous",
        )
    return _resolve_candidate(body, matches[0])


def _immediate_response_v1(
    body: ImmediateHistoryResolveRequestV1,
    *,
    resolution_status: str,
    match_count: int,
    reason_code: str,
    record: ImmediateHistoryRecord | None = None,
) -> ImmediateHistoryResolveResponseV1:
    return ImmediateHistoryResolveResponseV1(
        schema_version=body.schema_version,
        request_id=body.request_id,
        owner_id=body.owner_id,
        conversation_id=body.conversation_id,
        surface=body.surface,
        explanation_kind=body.explanation_kind,
        resolution_status=resolution_status,
        match_count=match_count,
        reason_code=reason_code,
        record=record,
    )


def _immediate_response_v2(
    body: ImmediateHistoryResolveRequestV2,
    *,
    resolution_status: str,
    reason_code: str,
    lineage_dereference_count: int = 0,
    match_count: int = 0,
    resolution_source: str = "none",
    record: ImmediateHistoryRecord | None = None,
    history_root_lineage: HistoryRootLineage | None = None,
) -> ImmediateHistoryResolveResponseV2:
    return ImmediateHistoryResolveResponseV2(
        schema_version=body.schema_version,
        request_id=body.request_id,
        owner_id=body.owner_id,
        conversation_id=body.conversation_id,
        surface=body.surface,
        explanation_kind=body.explanation_kind,
        resolution_status=resolution_status,
        resolution_source=resolution_source,
        lineage_dereference_count=lineage_dereference_count,
        match_count=match_count,
        reason_code=reason_code,
        record=record,
        history_root_lineage=history_root_lineage,
    )


def _immediate_candidate_identity(
    body: ImmediateHistoryResolveRequestV1 | ImmediateHistoryResolveRequestV2,
    candidate: Any,
) -> tuple[str, str] | None:
    if not isinstance(candidate, dict):
        return None
    message_id = _bounded_identifier(candidate.get("message_id"))
    original_request_id = _bounded_identifier(candidate.get("message_request_id"))
    if (
        message_id is None
        or original_request_id is None
        or candidate.get("message_owner_id") != body.owner_id
        or str(candidate.get("message_conversation_id")) != str(body.conversation_id)
        or candidate.get("message_role") != "assistant"
        or not isinstance(candidate.get("message_content"), str)
        or not candidate["message_content"]
    ):
        return None
    return message_id, original_request_id


def _support_record_matches(
    *,
    owner_id: str,
    conversation_id: UUID,
    surface: str,
    record: ClaimRecord,
    message_id: str,
    original_request_id: str,
    message_content: str,
) -> bool:
    expected_anchor = _normalized_first_paragraph(message_content)
    if (
        expected_anchor is None
        or record.owner_id != owner_id
        or record.conversation_id != str(conversation_id)
        or record.assistant_message_id != message_id
        or record.request_id != original_request_id
        or record.surface != surface
        or record.claim_anchor != expected_anchor
        or record.claim_anchor_digest != _response_digest(expected_anchor)
    ):
        return False
    return all(
        reference.owner_id == owner_id
        and (
            reference.conversation_id is None
            or reference.conversation_id == str(conversation_id)
        )
        for reference in record.validated_evidence_references
    )


def evaluate_support_records(
    records: Any,
    *,
    owner_id: str,
    conversation_id: UUID,
    surface: str,
    message_id: str,
    original_request_id: str,
    message_content: str,
) -> tuple[str, int, ClaimRecord | None, str | None]:
    if not isinstance(records, list):
        return "invalid", 0, None, "association"
    bounded = records[:2]
    if not bounded:
        return "no_record", 0, None, None
    if len(bounded) > 1:
        return "ambiguous", 2, None, None
    try:
        record = ClaimRecord.model_validate(bounded[0])
    except Exception:
        return "invalid", 1, None, "association"
    if record.surface != surface:
        return "invalid", 1, record, "surface"
    if not _support_record_matches(
        owner_id=owner_id,
        conversation_id=conversation_id,
        surface=surface,
        record=record,
        message_id=message_id,
        original_request_id=original_request_id,
        message_content=message_content,
    ):
        return "invalid", 1, record, "association"
    return "resolved", 1, record, None


async def _load_support_evaluation(
    store: AcquisitionHistoryStore,
    body: ImmediateHistoryResolveRequestV1 | ImmediateHistoryResolveRequestV2,
    *,
    message_id: str,
    original_request_id: str,
    message_content: str,
) -> tuple[str, int, ClaimRecord | None, str | None]:
    try:
        records = await store.list_claim_records(
            owner_id=body.owner_id,
            conversation_id=str(body.conversation_id),
            assistant_message_id=message_id,
            request_id=original_request_id,
            limit=2,
        )
    except Exception:
        return "unavailable", 0, None, None
    return evaluate_support_records(
        records,
        owner_id=body.owner_id,
        conversation_id=body.conversation_id,
        surface=body.surface,
        message_id=message_id,
        original_request_id=original_request_id,
        message_content=message_content,
    )


def evaluate_acquisition_candidate(
    candidate: dict[str, Any],
    *,
    request_id: str,
    owner_id: str,
    conversation_id: UUID,
    surface: str,
) -> tuple[str, AcquisitionHistoryRecord | None]:
    content = candidate.get("message_content")
    normalized_paragraph = _normalized_first_paragraph(content)
    if normalized_paragraph is None:
        return "invalid", None
    legacy_body = AcquisitionHistoryResolveRequest(
        schema_version="acquisition-history-resolution.v1",
        request_id=request_id,
        owner_id=owner_id,
        conversation_id=conversation_id,
        surface=surface,
        target_mode="immediate_previous",
        response_digest=_response_digest(content),
        normalized_first_paragraph=normalized_paragraph,
    )
    resolved = _resolve_candidate(legacy_body, candidate)
    if resolved.resolution_status == "resolved":
        return "resolved", resolved.record
    if resolved.resolution_status == "no_record":
        return "no_record", None
    return "invalid", None


async def _resolve_immediate_history_v1(
    store: AcquisitionHistoryStore,
    body: ImmediateHistoryResolveRequestV1,
) -> ImmediateHistoryResolveResponseV1:
    try:
        candidates = await store.list_assistant_trace_candidates(
            owner_id=body.owner_id,
            conversation_id=body.conversation_id,
            limit=1,
        )
    except Exception:
        return _immediate_response_v1(
            body,
            resolution_status="unavailable",
            match_count=0,
            reason_code="history_store_unavailable",
        )
    if not isinstance(candidates, list):
        return _immediate_response_v1(
            body,
            resolution_status="invalid",
            match_count=0,
            reason_code="immediate_response_invalid",
        )
    candidates = candidates[:1]
    if not candidates:
        return _immediate_response_v1(
            body,
            resolution_status="no_record",
            match_count=0,
            reason_code="immediate_response_not_found",
        )
    candidate = candidates[0]
    identity = _immediate_candidate_identity(body, candidate)
    if identity is None:
        return _immediate_response_v1(
            body,
            resolution_status="invalid",
            match_count=1,
            reason_code="immediate_response_invalid",
        )
    message_id, original_request_id = identity
    if body.explanation_kind == "support":
        status, match_count, claim, _ = await _load_support_evaluation(
            store,
            body,
            message_id=message_id,
            original_request_id=original_request_id,
            message_content=candidate["message_content"],
        )
        reasons = {
            "resolved": "support_record_resolved",
            "no_record": "support_record_not_found",
            "ambiguous": "support_record_ambiguous",
            "invalid": "support_record_invalid",
            "unavailable": "history_store_unavailable",
        }
        record = (
            ImmediateHistoryRecord(
                record_kind="support",
                assistant_message_id=message_id,
                original_request_id=original_request_id,
                support_record=claim,
            )
            if status == "resolved" and claim is not None
            else None
        )
        return _immediate_response_v1(
            body,
            resolution_status=status,
            match_count=match_count,
            reason_code=reasons[status],
            record=record,
        )

    status, acquisition = evaluate_acquisition_candidate(
        candidate,
        request_id=body.request_id,
        owner_id=body.owner_id,
        conversation_id=body.conversation_id,
        surface=body.surface,
    )
    reasons = {
        "resolved": "acquisition_record_resolved",
        "no_record": "acquisition_record_not_found",
        "invalid": "acquisition_record_invalid",
    }
    record = (
        ImmediateHistoryRecord(
            record_kind="acquisition",
            assistant_message_id=message_id,
            original_request_id=original_request_id,
            acquisition_record=acquisition,
        )
        if status == "resolved" and acquisition is not None
        else None
    )
    return _immediate_response_v1(
        body,
        resolution_status=status,
        match_count=1,
        reason_code=reasons[status],
        record=record,
    )


def _parse_history_root_lineage(
    value: Any,
) -> tuple[HistoryRootLineage | None, str | None]:
    if not isinstance(value, dict):
        return None, "lineage_malformed"
    if value.get("schema_version") != "history-root-lineage.v1":
        if isinstance(value.get("schema_version"), str):
            return None, "lineage_version_unsupported"
        return None, "lineage_malformed"
    try:
        return HistoryRootLineage.model_validate(value), None
    except Exception:
        return None, "lineage_malformed"


async def _resolve_immediate_history_v2(
    store: AcquisitionHistoryStore,
    body: ImmediateHistoryResolveRequestV2,
) -> ImmediateHistoryResolveResponseV2:
    try:
        candidates = await store.list_assistant_trace_candidates(
            owner_id=body.owner_id,
            conversation_id=body.conversation_id,
            limit=1,
        )
    except Exception:
        return _immediate_response_v2(
            body,
            resolution_status="unavailable",
            reason_code="history_store_unavailable",
        )
    if not isinstance(candidates, list):
        return _immediate_response_v2(
            body,
            resolution_status="invalid",
            reason_code="direct_response_invalid",
        )
    candidates = candidates[:1]
    if not candidates:
        return _immediate_response_v2(
            body,
            resolution_status="no_record",
            reason_code="direct_record_absent_lineage_absent",
        )

    candidate = candidates[0]
    identity = _immediate_candidate_identity(body, candidate)
    if identity is None:
        return _immediate_response_v2(
            body,
            resolution_status="invalid",
            reason_code="direct_response_invalid",
        )
    message_id, original_request_id = identity
    try:
        direct_root_id = UUID(message_id)
    except (TypeError, ValueError):
        return _immediate_response_v2(
            body,
            resolution_status="invalid",
            reason_code="direct_response_invalid",
        )

    if body.explanation_kind == "support":
        direct_status, _, direct_claim, _ = await _load_support_evaluation(
            store,
            body,
            message_id=message_id,
            original_request_id=original_request_id,
            message_content=candidate["message_content"],
        )
        if direct_status == "resolved" and direct_claim is not None:
            lineage = HistoryRootLineage(
                schema_version="history-root-lineage.v1",
                root_assistant_message_id=direct_root_id,
                record_kind="support",
            )
            return _immediate_response_v2(
                body,
                resolution_status="resolved",
                resolution_source="direct_record",
                match_count=1,
                reason_code="direct_support_record_resolved",
                record=ImmediateHistoryRecord(
                    record_kind="support",
                    assistant_message_id=message_id,
                    original_request_id=original_request_id,
                    support_record=direct_claim,
                ),
                history_root_lineage=lineage,
            )
        if direct_status == "ambiguous":
            return _immediate_response_v2(
                body,
                resolution_status="ambiguous",
                match_count=2,
                reason_code="direct_support_record_ambiguous",
            )
        if direct_status == "invalid":
            return _immediate_response_v2(
                body,
                resolution_status="invalid",
                reason_code="direct_support_record_invalid",
            )
        if direct_status == "unavailable":
            return _immediate_response_v2(
                body,
                resolution_status="unavailable",
                reason_code="history_store_unavailable",
            )
    else:
        direct_status, direct_acquisition = evaluate_acquisition_candidate(
            candidate,
            request_id=body.request_id,
            owner_id=body.owner_id,
            conversation_id=body.conversation_id,
            surface=body.surface,
        )
        if direct_status == "resolved" and direct_acquisition is not None:
            lineage = HistoryRootLineage(
                schema_version="history-root-lineage.v1",
                root_assistant_message_id=direct_root_id,
                record_kind="acquisition",
            )
            return _immediate_response_v2(
                body,
                resolution_status="resolved",
                resolution_source="direct_record",
                match_count=1,
                reason_code="direct_acquisition_record_resolved",
                record=ImmediateHistoryRecord(
                    record_kind="acquisition",
                    assistant_message_id=message_id,
                    original_request_id=original_request_id,
                    acquisition_record=direct_acquisition,
                ),
                history_root_lineage=lineage,
            )
        if direct_status == "invalid":
            return _immediate_response_v2(
                body,
                resolution_status="invalid",
                reason_code="direct_acquisition_record_invalid",
            )

    raw_lineage = candidate.get("message_history_root_lineage")
    if raw_lineage is None:
        return _immediate_response_v2(
            body,
            resolution_status="no_record",
            reason_code="direct_record_absent_lineage_absent",
        )
    lineage, lineage_error = _parse_history_root_lineage(raw_lineage)
    if lineage_error is not None or lineage is None:
        return _immediate_response_v2(
            body,
            resolution_status="invalid",
            reason_code=lineage_error or "lineage_malformed",
        )
    if lineage.record_kind != body.explanation_kind:
        return _immediate_response_v2(
            body,
            resolution_status="invalid",
            reason_code="lineage_record_kind_mismatch",
        )

    try:
        root = await store.get_assistant_history_root(
            message_id=lineage.root_assistant_message_id,
        )
    except Exception:
        return _immediate_response_v2(
            body,
            resolution_status="unavailable",
            lineage_dereference_count=1,
            reason_code="history_store_unavailable",
        )
    if root is None:
        return _immediate_response_v2(
            body,
            resolution_status="no_record",
            lineage_dereference_count=1,
            reason_code="lineage_root_missing",
        )
    if not isinstance(root, dict):
        return _immediate_response_v2(
            body,
            resolution_status="no_record",
            lineage_dereference_count=1,
            reason_code="lineage_root_unresolvable",
        )
    if root.get("message_owner_id") != body.owner_id:
        return _immediate_response_v2(
            body,
            resolution_status="invalid",
            lineage_dereference_count=1,
            reason_code="lineage_owner_mismatch",
        )
    if str(root.get("message_conversation_id")) != str(body.conversation_id):
        return _immediate_response_v2(
            body,
            resolution_status="invalid",
            lineage_dereference_count=1,
            reason_code="lineage_conversation_mismatch",
        )
    if root.get("message_role") != "assistant":
        return _immediate_response_v2(
            body,
            resolution_status="invalid",
            lineage_dereference_count=1,
            reason_code="lineage_root_role_invalid",
        )
    if root.get("message_history_root_lineage") is not None:
        return _immediate_response_v2(
            body,
            resolution_status="invalid",
            lineage_dereference_count=1,
            reason_code="lineage_root_recursive",
        )

    root_identity = _immediate_candidate_identity(body, root)
    if root_identity is None:
        return _immediate_response_v2(
            body,
            resolution_status="no_record",
            lineage_dereference_count=1,
            reason_code="lineage_root_unresolvable",
        )
    root_message_id, root_request_id = root_identity
    if root_message_id != str(lineage.root_assistant_message_id):
        return _immediate_response_v2(
            body,
            resolution_status="invalid",
            lineage_dereference_count=1,
            reason_code="lineage_root_association_invalid",
        )

    if body.explanation_kind == "support":
        root_status, _, root_claim, root_failure = await _load_support_evaluation(
            store,
            body,
            message_id=root_message_id,
            original_request_id=root_request_id,
            message_content=root["message_content"],
        )
        if root_status == "resolved" and root_claim is not None:
            return _immediate_response_v2(
                body,
                resolution_status="resolved",
                resolution_source="root_lineage",
                lineage_dereference_count=1,
                match_count=1,
                reason_code="root_lineage_support_record_resolved",
                record=ImmediateHistoryRecord(
                    record_kind="support",
                    assistant_message_id=root_message_id,
                    original_request_id=root_request_id,
                    support_record=root_claim,
                ),
                history_root_lineage=lineage,
            )
        if root_status == "no_record":
            reason = "lineage_root_not_direct_record_owner"
            status = "no_record"
        elif root_status == "unavailable":
            reason = "history_store_unavailable"
            status = "unavailable"
        elif root_failure == "surface":
            reason = "lineage_surface_mismatch"
            status = "invalid"
        else:
            reason = "lineage_root_association_invalid"
            status = "invalid"
        return _immediate_response_v2(
            body,
            resolution_status=status,
            lineage_dereference_count=1,
            reason_code=reason,
        )

    if root.get("trace_surface") is not None and root.get("trace_surface") != body.surface:
        return _immediate_response_v2(
            body,
            resolution_status="invalid",
            lineage_dereference_count=1,
            reason_code="lineage_surface_mismatch",
        )
    root_status, root_acquisition = evaluate_acquisition_candidate(
        root,
        request_id=body.request_id,
        owner_id=body.owner_id,
        conversation_id=body.conversation_id,
        surface=body.surface,
    )
    if root_status == "resolved" and root_acquisition is not None:
        return _immediate_response_v2(
            body,
            resolution_status="resolved",
            resolution_source="root_lineage",
            lineage_dereference_count=1,
            match_count=1,
            reason_code="root_lineage_acquisition_record_resolved",
            record=ImmediateHistoryRecord(
                record_kind="acquisition",
                assistant_message_id=root_message_id,
                original_request_id=root_request_id,
                acquisition_record=root_acquisition,
            ),
            history_root_lineage=lineage,
        )
    return _immediate_response_v2(
        body,
        resolution_status=("no_record" if root_status == "no_record" else "invalid"),
        lineage_dereference_count=1,
        reason_code=(
            "lineage_root_not_direct_record_owner"
            if root_status == "no_record"
            else "lineage_root_association_invalid"
        ),
    )


async def resolve_immediate_history(
    store: AcquisitionHistoryStore,
    body: ImmediateHistoryResolveRequest,
) -> ImmediateHistoryResolveResponse:
    if isinstance(body, ImmediateHistoryResolveRequestV2):
        return await _resolve_immediate_history_v2(store, body)
    return await _resolve_immediate_history_v1(store, body)
