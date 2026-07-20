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
)


class AcquisitionHistoryStore(Protocol):
    async def list_assistant_trace_candidates(
        self,
        *,
        owner_id: str,
        conversation_id: UUID,
        limit: int,
    ) -> list[dict[str, Any]]: ...


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


def _manifest_key_is_private(key: str) -> bool:
    normalized = key.casefold()
    if normalized in _PRIVATE_KEYS:
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
