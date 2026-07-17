from __future__ import annotations

import asyncio
from copy import deepcopy
from hashlib import sha256
from uuid import UUID, uuid4

import psycopg
import pytest
from pydantic import ValidationError

from models import ClaimRecord, ClaimRecordCreateRequest, ClaimRecordCreateResponse
from services.claim_records import (
    ClaimRecordError,
    _canonical_record,
    create_claim_record,
    get_claim_record,
    list_claim_records,
    validate_claim_record_association,
)
from storage.postgres import (
    PostgresStore,
    _CLAIM_RECORD_COLUMNS,
    _claim_record_from_row,
)


PRIVATE_ANSWER = "PRIVATE_ASSISTANT_ANSWER_SENTINEL"
PRIVATE_TRACE = "PRIVATE_TRACE_INTERNAL_SENTINEL"
DEFAULT_ANCHOR = "The selected setting changed."
DEFAULT_ANCHOR_DIGEST = "sha256:" + sha256(DEFAULT_ANCHOR.encode()).hexdigest()


def _calibration(
    *,
    claim_id: str,
    references: list[dict],
    anchor: str = DEFAULT_ANCHOR,
) -> dict:
    return {
        "claim_id": claim_id,
        "claim_anchor": anchor,
        "claim_anchor_digest": "sha256:" + sha256(anchor.encode()).hexdigest(),
        "claim_class": "source_backed_fact",
        "calibration_status": "supported",
        "evidence_strength": "moderate",
        "confidence": "medium",
        "strongest_authority": "trusted_integration",
        "freshness_summary": "current",
        "uncertainty_disclosure_required": False,
        "validated_evidence_references": references,
        "limitation_codes": ["single_source"],
        "user_safe_summary": "This claim has current recorded support.",
    }


def _reference(
    *,
    ref_type: str,
    ref_id: str,
    owner_id: str,
    conversation_id: str | None,
) -> dict:
    return {
        "ref_type": ref_type,
        "ref_id": ref_id,
        "owner_id": owner_id,
        "conversation_id": conversation_id,
        "support_kind": "direct",
        "authority": "trusted_integration",
        "freshness_state": "active",
    }


def _request(
    *,
    claim_id: str,
    owner_id: str,
    conversation_id: UUID | str,
    assistant_message_id: UUID | str,
    request_id: str,
    references: list[dict],
    acquisition_manifest_id: str | None = None,
) -> ClaimRecordCreateRequest:
    return ClaimRecordCreateRequest(
        schema_version="claim-record.v1",
        request_id=request_id,
        owner_id=owner_id,
        conversation_id=str(conversation_id),
        assistant_message_id=str(assistant_message_id),
        surface="desktop_private",
        runtime_session_id=f"session-{request_id}",
        runtime_turn_id=f"turn-{request_id}",
        acquisition_manifest_id=acquisition_manifest_id,
        calibration_result=_calibration(
            claim_id=claim_id,
            references=references,
        ),
    )


def _seed_request_scope(
    dsn: str,
    *,
    owner_id: str = "owner-claim",
    request_id: str = "request-claim",
    surface: str = "desktop_private",
    trace_status: str = "ok",
    acquisition_manifest_id: str | None = None,
) -> dict:
    conversation_id = uuid4()
    assistant_message_id = uuid4()
    user_message_id = uuid4()
    evidence_message_id = uuid4()
    artifact_id = uuid4()
    derived_text_id = uuid4()
    with psycopg.connect(dsn) as conn:
        prompt = {"private": PRIVATE_TRACE}
        if acquisition_manifest_id is not None:
            prompt["evidence_acquisition"] = {
                "enabled": True,
                "attempted": True,
                "status": "sufficient_for_declared_scope",
                "manifest_id": acquisition_manifest_id,
                "assistant_message_id": str(assistant_message_id),
                "response_digest": DEFAULT_ANCHOR_DIGEST,
                "inventory": {
                    "source_count": 3,
                },
                "plan": {
                    "plan_status": "ready",
                },
                "acquisition": {
                    "sources_considered": ["source-a", "source-b", "source-c"],
                    "sources_selected": ["source-a", "source-b"],
                    "source_references_returned": ["source-ref-a", "source-ref-b"],
                    "source_references_retained": ["source-ref-a", "source-ref-b"],
                    "attempts": [
                        {"source_id": "source-a", "outcome": "satisfied"},
                        {"source_id": "source-b", "outcome": "satisfied"},
                    ],
                },
                "sufficiency": {
                    "status": "sufficient_for_declared_scope",
                    "reason_codes": ["all_declared_requirements_satisfied"],
                },
            }
        conn.execute(
            "INSERT INTO conversations (id, owner_id, title) VALUES (%s, %s, 'Claim records')",
            (conversation_id, owner_id),
        )
        conn.execute(
            """
            INSERT INTO messages (id, conversation_id, owner_id, role, content, metadata)
            VALUES
              (%s, %s, %s, 'assistant', %s, %s::jsonb),
              (%s, %s, %s, 'user', 'user content', %s::jsonb),
              (%s, %s, %s, 'user', 'evidence content', '{}'::jsonb)
            """,
            (
                assistant_message_id,
                conversation_id,
                owner_id,
                PRIVATE_ANSWER,
                psycopg.types.json.Json({"request_id": request_id}),
                user_message_id,
                conversation_id,
                owner_id,
                psycopg.types.json.Json({"request_id": request_id}),
                evidence_message_id,
                conversation_id,
                owner_id,
            ),
        )
        conn.execute(
            """
            INSERT INTO artifacts (
              id, owner_id, conversation_id, filename, mime, size, object_uri, status
            ) VALUES (%s, %s, %s, 'evidence.txt', 'text/plain', 8, 'memory://evidence', 'completed')
            """,
            (artifact_id, owner_id, conversation_id),
        )
        conn.execute(
            """
            INSERT INTO derived_text (id, artifact_id, kind, text)
            VALUES (%s, %s, 'text', 'private derived content')
            """,
            (derived_text_id, artifact_id),
        )
        trace_references = [
            {"ref_type": "message", "ref_id": str(evidence_message_id)},
            {"ref_type": "artifact", "ref_id": str(artifact_id)},
            {"ref_type": "derived_text", "ref_id": str(derived_text_id)},
            {"ref_type": "external_source", "ref_id": "external-source-1"},
        ]
        conn.execute(
            """
            INSERT INTO traces (
              request_id, conversation_id, owner_id, surface, profile_json,
              retrieval_json, router_decision_json, model_call_json, references_json,
              cost_json, status, prompt_json
            ) VALUES (
              %s, %s, %s, %s, '{}'::jsonb, %s::jsonb, '{}'::jsonb, '{}'::jsonb,
              %s::jsonb, '{}'::jsonb, %s, %s::jsonb
            )
            """,
            (
                request_id,
                conversation_id,
                owner_id,
                surface,
                psycopg.types.json.Json({"diagnostic": PRIVATE_TRACE}),
                psycopg.types.json.Json(trace_references),
                trace_status,
                psycopg.types.json.Json(prompt),
            ),
        )
        conn.commit()
    return {
        "owner_id": owner_id,
        "request_id": request_id,
        "conversation_id": conversation_id,
        "assistant_message_id": assistant_message_id,
        "user_message_id": user_message_id,
        "evidence_message_id": evidence_message_id,
        "artifact_id": artifact_id,
        "derived_text_id": derived_text_id,
    }


def _run_create(dsn: str, body: ClaimRecordCreateRequest):
    async def run():
        store = PostgresStore(dsn)
        await store.open()
        try:
            return await create_claim_record(store, body)
        finally:
            await store.close()

    return asyncio.run(run())


def _pure_manifest_record_and_association(
    *,
    sufficiency_status: str = "sufficient_for_declared_scope",
    plan_status: str = "ready",
) -> tuple[dict, dict]:
    conversation_id = str(uuid4())
    assistant_message_id = str(uuid4())
    manifest_id = "evidence_manifest_33333333333333333333333333333333"
    body = _request(
        claim_id="claim_pure_manifest",
        owner_id="owner-pure",
        conversation_id=conversation_id,
        assistant_message_id=assistant_message_id,
        request_id="request-pure-manifest",
        references=[
            _reference(
                ref_type="external_source",
                ref_id="external-source-1",
                owner_id="owner-pure",
                conversation_id=None,
            )
        ],
        acquisition_manifest_id=manifest_id,
    )
    record = _canonical_record(body)
    association = {
        "existing": None,
        "conversation": {"owner_id": body.owner_id},
        "assistant_message": {
            "owner_id": body.owner_id,
            "conversation_id": conversation_id,
            "role": "assistant",
            "metadata": {"request_id": body.request_id},
        },
        "trace": {
            "owner_id": body.owner_id,
            "conversation_id": conversation_id,
            "surface": body.surface,
            "status": "ok",
            "references": [
                {"ref_type": "external_source", "ref_id": "external-source-1"}
            ],
            "prompt": {
                "evidence_acquisition": {
                    "attempted": True,
                    "status": sufficiency_status,
                    "manifest_id": manifest_id,
                    "assistant_message_id": assistant_message_id,
                    "response_digest": DEFAULT_ANCHOR_DIGEST,
                    "plan": {"plan_status": plan_status},
                    "sufficiency": {
                        "status": sufficiency_status,
                    },
                }
            },
        },
        "local_references": {},
    }
    return record, association


@pytest.mark.parametrize(
    ("sufficiency_status", "plan_status"),
    [
        ("sufficient_for_declared_scope", "ready"),
        ("sufficient_with_limitations", "ready_with_limitations"),
    ],
)
def test_manifest_association_and_legacy_serialization_are_bounded(
    sufficiency_status,
    plan_status,
):
    record, association = _pure_manifest_record_and_association(
        sufficiency_status=sufficiency_status,
        plan_status=plan_status,
    )
    manifest_id = record["acquisition_manifest_id"]

    assert validate_claim_record_association(record, association) is None
    linked = ClaimRecord(**{**record, "created_at": "2026-07-17T12:00:00+00:00"})
    linked_response = ClaimRecordCreateResponse(created=True, record=linked).model_dump()
    assert linked_response["record"]["acquisition_manifest_id"] == manifest_id

    legacy_record = ClaimRecord(
        **{
            **record,
            "acquisition_manifest_id": None,
            "created_at": "2026-07-17T12:00:00+00:00",
        }
    )
    legacy_response = ClaimRecordCreateResponse(
        created=True,
        record=legacy_record,
    ).model_dump()
    assert "acquisition_manifest_id" not in legacy_response["record"]


@pytest.mark.parametrize(
    ("mutation", "expected"),
    [
        ("prompt_missing", "acquisition_manifest_not_in_trace"),
        ("manifest_missing", "acquisition_manifest_not_in_trace"),
        ("manifest_malformed", "acquisition_manifest_not_in_trace"),
        ("manifest_id_missing", "acquisition_manifest_not_in_trace"),
        ("manifest_id_mismatch", "acquisition_manifest_association_mismatch"),
        ("assistant_mismatch", "acquisition_manifest_association_mismatch"),
        ("digest_mismatch", "acquisition_manifest_association_mismatch"),
        ("not_attempted", "acquisition_manifest_not_eligible"),
        ("plan_missing", "acquisition_manifest_not_eligible"),
        ("plan_unsupported", "acquisition_manifest_not_eligible"),
        ("sufficiency_missing", "acquisition_manifest_not_eligible"),
        ("top_insufficient", "acquisition_manifest_not_eligible"),
        ("nested_unknown", "acquisition_manifest_not_eligible"),
        ("status_disagreement", "acquisition_manifest_not_eligible"),
    ],
)
def test_manifest_association_errors_are_bounded_without_trace_disclosure(
    mutation,
    expected,
):
    record, association = _pure_manifest_record_and_association()
    trace = association["trace"]
    manifest = trace["prompt"]["evidence_acquisition"]
    if mutation == "prompt_missing":
        trace.pop("prompt")
    elif mutation == "manifest_missing":
        trace["prompt"].pop("evidence_acquisition")
    elif mutation == "manifest_malformed":
        trace["prompt"]["evidence_acquisition"] = ["private"]
    elif mutation == "manifest_id_missing":
        manifest.pop("manifest_id")
    elif mutation == "manifest_id_mismatch":
        manifest["manifest_id"] = "evidence_manifest_44444444444444444444444444444444"
    elif mutation == "assistant_mismatch":
        manifest["assistant_message_id"] = str(uuid4())
    elif mutation == "digest_mismatch":
        manifest["response_digest"] = "sha256:" + "0" * 64
    elif mutation == "not_attempted":
        manifest["attempted"] = False
    elif mutation == "plan_missing":
        manifest.pop("plan")
    elif mutation == "plan_unsupported":
        manifest["plan"]["plan_status"] = "unsupported"
    elif mutation == "sufficiency_missing":
        manifest.pop("sufficiency")
    elif mutation == "top_insufficient":
        manifest["status"] = "insufficient"
    elif mutation == "nested_unknown":
        manifest["sufficiency"]["status"] = "unknown"
    else:
        manifest["status"] = "sufficient_with_limitations"

    with pytest.raises(ClaimRecordError) as exc_info:
        validate_claim_record_association(record, association)
    assert exc_info.value.code == expected
    assert PRIVATE_TRACE not in str(exc_info.value)


@pytest.mark.parametrize(
    "manifest_id",
    [
        "",
        "manifest value",
        "https://manifest.invalid",
        "manifest?secret=value",
        "x" * 121,
    ],
)
def test_acquisition_manifest_identifier_contract_rejects_unsafe_values(
    manifest_id,
):
    body = _request(
        claim_id="claim_invalid_manifest",
        owner_id="owner-pure",
        conversation_id=str(uuid4()),
        assistant_message_id=str(uuid4()),
        request_id="request-invalid-manifest",
        references=[],
    )
    payload = body.model_dump(mode="json")
    payload["acquisition_manifest_id"] = manifest_id

    with pytest.raises(ValidationError):
        ClaimRecordCreateRequest.model_validate(payload)

    payload = body.model_dump(mode="json")
    payload["unrestricted_metadata"] = {"source_inventory": ["private"]}
    with pytest.raises(ValidationError):
        ClaimRecordCreateRequest.model_validate(payload)


def test_claim_record_column_decoder_preserves_manifest_position():
    row = (
        "claim_decoder",
        "claim-record.v1",
        "owner-decoder",
        uuid4(),
        "request-decoder",
        uuid4(),
        "desktop_private",
        "session-decoder",
        "turn-decoder",
        "evidence_manifest_55555555555555555555555555555555",
        DEFAULT_ANCHOR,
        DEFAULT_ANCHOR_DIGEST,
        "source_backed_fact",
        "supported",
        "moderate",
        "medium",
        "trusted_integration",
        "current",
        False,
        [],
        [],
        "Bounded decoder summary.",
        "2026-07-17T12:00:00+00:00",
    )

    decoded = _claim_record_from_row(row)

    assert "acquisition_manifest_id" in {
        column.strip() for column in _CLAIM_RECORD_COLUMNS.split(",")
    }
    assert (
        decoded["acquisition_manifest_id"]
        == "evidence_manifest_55555555555555555555555555555555"
    )
    assert decoded["claim_anchor"] == DEFAULT_ANCHOR
    assert decoded["user_safe_summary"] == "Bounded decoder summary."


def test_valid_record_is_immutable_idempotent_and_private(postgres_database):
    scope = _seed_request_scope(postgres_database)
    references = [
        _reference(
            ref_type="external_source",
            ref_id="external-source-1",
            owner_id=scope["owner_id"],
            conversation_id=str(scope["conversation_id"]),
        )
    ]
    body = _request(claim_id="claim_external_1", references=references, **{
        key: scope[key]
        for key in ("owner_id", "conversation_id", "assistant_message_id", "request_id")
    })

    created, first = _run_create(postgres_database, body)
    replay_created, replay = _run_create(postgres_database, body)

    assert created is True
    assert replay_created is False
    assert replay == first
    assert first.acquisition_manifest_id is None
    assert "acquisition_manifest_id" not in first.model_dump()
    assert replay.validated_evidence_references[0].ref_id == "external-source-1"
    with psycopg.connect(postgres_database) as conn:
        count = conn.execute(
            "SELECT count(*) FROM claim_records WHERE claim_id = 'claim_external_1'"
        ).fetchone()[0]
        stored_json = conn.execute(
            "SELECT evidence_references_json FROM claim_records WHERE claim_id = 'claim_external_1'"
        ).fetchone()[0]
    assert count == 1
    assert set(stored_json[0]) == {
        "ref_type",
        "ref_id",
        "owner_id",
        "conversation_id",
        "support_kind",
        "authority",
        "freshness_state",
    }
    serialized = first.model_dump_json()
    assert PRIVATE_ANSWER not in serialized
    assert PRIVATE_TRACE not in serialized


def test_manifest_link_round_trips_without_copying_acquisition_scope(
    postgres_database,
):
    manifest_id = "evidence_manifest_0123456789abcdef0123456789abcdef"
    scope = _seed_request_scope(
        postgres_database,
        request_id="request-linked-manifest",
        acquisition_manifest_id=manifest_id,
    )
    reference = _reference(
        ref_type="artifact",
        ref_id=str(scope["artifact_id"]),
        owner_id=scope["owner_id"],
        conversation_id=str(scope["conversation_id"]),
    )
    body = _request(
        claim_id="claim_linked_manifest",
        references=[reference],
        acquisition_manifest_id=manifest_id,
        **{
            key: scope[key]
            for key in ("owner_id", "conversation_id", "assistant_message_id", "request_id")
        },
    )

    created, first = _run_create(postgres_database, body)
    replay_created, replay = _run_create(postgres_database, body)

    assert created is True
    assert replay_created is False
    assert replay == first
    assert first.acquisition_manifest_id == manifest_id
    assert len(first.validated_evidence_references) == 1
    assert first.validated_evidence_references[0].ref_id == str(scope["artifact_id"])
    async def load():
        store = PostgresStore(postgres_database)
        await store.open()
        try:
            one = await get_claim_record(
                store,
                claim_id=body.calibration_result.claim_id,
                owner_id=scope["owner_id"],
                conversation_id=str(scope["conversation_id"]),
            )
            listed = await list_claim_records(
                store,
                owner_id=scope["owner_id"],
                conversation_id=str(scope["conversation_id"]),
                assistant_message_id=None,
                request_id=None,
                limit=20,
            )
            return one, listed
        finally:
            await store.close()

    one, listed = asyncio.run(load())
    assert one.acquisition_manifest_id == manifest_id
    assert listed[0].acquisition_manifest_id == manifest_id
    serialized = one.model_dump_json()
    assert manifest_id in serialized
    for excluded in (
        "sources_considered",
        "sources_selected",
        "source_references_returned",
        "source_references_retained",
        "attempts",
        "reason_codes",
        "source-a",
        "source-b",
    ):
        assert excluded not in serialized

    changed = body.model_copy(deep=True)
    changed.acquisition_manifest_id = "evidence_manifest_ffffffffffffffffffffffffffffffff"
    with pytest.raises(ClaimRecordError, match="claim_record_conflict"):
        _run_create(postgres_database, changed)
    omitted = body.model_copy(deep=True)
    omitted.acquisition_manifest_id = None
    with pytest.raises(ClaimRecordError, match="claim_record_conflict"):
        _run_create(postgres_database, omitted)


@pytest.mark.parametrize(
    ("mutation", "expected"),
    [
        ("manifest_absent", "acquisition_manifest_not_in_trace"),
        ("manifest_malformed", "acquisition_manifest_not_in_trace"),
        ("manifest_id_absent", "acquisition_manifest_not_in_trace"),
        ("manifest_id_mismatch", "acquisition_manifest_association_mismatch"),
        ("assistant_message_mismatch", "acquisition_manifest_association_mismatch"),
        ("response_digest_mismatch", "acquisition_manifest_association_mismatch"),
        ("not_attempted", "acquisition_manifest_not_eligible"),
        ("plan_unsupported", "acquisition_manifest_not_eligible"),
        ("plan_not_compiled", "acquisition_manifest_not_eligible"),
        ("top_insufficient", "acquisition_manifest_not_eligible"),
        ("top_unknown", "acquisition_manifest_not_eligible"),
        ("nested_insufficient", "acquisition_manifest_not_eligible"),
        ("nested_unknown", "acquisition_manifest_not_eligible"),
        ("status_disagreement", "acquisition_manifest_not_eligible"),
        ("trace_owner", "request_trace_scope_mismatch"),
        ("trace_conversation", "request_trace_scope_mismatch"),
        ("trace_surface", "request_trace_scope_mismatch"),
        ("trace_failed", "request_trace_not_eligible"),
    ],
)
def test_manifest_association_failures_are_atomic(
    postgres_database,
    mutation,
    expected,
):
    manifest_id = "evidence_manifest_11111111111111111111111111111111"
    scope = _seed_request_scope(
        postgres_database,
        request_id=f"request-manifest-{mutation}",
        acquisition_manifest_id=manifest_id,
    )
    body = _request(
        claim_id=f"claim_manifest_{mutation}",
        references=[
            _reference(
                ref_type="external_source",
                ref_id="external-source-1",
                owner_id=scope["owner_id"],
                conversation_id=None,
            )
        ],
        acquisition_manifest_id=manifest_id,
        **{
            key: scope[key]
            for key in ("owner_id", "conversation_id", "assistant_message_id", "request_id")
        },
    )

    with psycopg.connect(postgres_database) as conn:
        prompt = deepcopy(
            conn.execute(
                "SELECT prompt_json FROM traces WHERE request_id = %s",
                (scope["request_id"],),
            ).fetchone()[0]
        )
        manifest = prompt["evidence_acquisition"]
        if mutation == "manifest_absent":
            prompt.pop("evidence_acquisition")
        elif mutation == "manifest_malformed":
            prompt["evidence_acquisition"] = []
        elif mutation == "manifest_id_absent":
            manifest.pop("manifest_id")
        elif mutation == "manifest_id_mismatch":
            manifest["manifest_id"] = "evidence_manifest_22222222222222222222222222222222"
        elif mutation == "assistant_message_mismatch":
            manifest["assistant_message_id"] = str(uuid4())
        elif mutation == "response_digest_mismatch":
            manifest["response_digest"] = "sha256:" + "0" * 64
        elif mutation == "not_attempted":
            manifest["attempted"] = False
        elif mutation == "plan_unsupported":
            manifest["plan"]["plan_status"] = "unsupported"
        elif mutation == "plan_not_compiled":
            manifest["plan"]["plan_status"] = "not_compiled"
        elif mutation == "top_insufficient":
            manifest["status"] = "insufficient"
        elif mutation == "top_unknown":
            manifest["status"] = "unknown"
        elif mutation == "nested_insufficient":
            manifest["sufficiency"]["status"] = "insufficient"
        elif mutation == "nested_unknown":
            manifest["sufficiency"]["status"] = "unknown"
        elif mutation == "status_disagreement":
            manifest["status"] = "sufficient_with_limitations"
        elif mutation == "trace_owner":
            conn.execute(
                "UPDATE traces SET owner_id = 'other-owner' WHERE request_id = %s",
                (scope["request_id"],),
            )
        elif mutation == "trace_conversation":
            other_conversation = uuid4()
            conn.execute(
                "INSERT INTO conversations (id, owner_id, title) VALUES (%s, %s, 'other')",
                (other_conversation, scope["owner_id"]),
            )
            conn.execute(
                "UPDATE traces SET conversation_id = %s WHERE request_id = %s",
                (other_conversation, scope["request_id"]),
            )
        elif mutation == "trace_surface":
            conn.execute(
                "UPDATE traces SET surface = 'other' WHERE request_id = %s",
                (scope["request_id"],),
            )
        elif mutation == "trace_failed":
            conn.execute(
                "UPDATE traces SET status = 'failed' WHERE request_id = %s",
                (scope["request_id"],),
            )
        if mutation not in {
            "trace_owner",
            "trace_conversation",
            "trace_surface",
            "trace_failed",
        }:
            conn.execute(
                "UPDATE traces SET prompt_json = %s::jsonb WHERE request_id = %s",
                (psycopg.types.json.Json(prompt), scope["request_id"]),
            )
        conn.commit()

    with pytest.raises(ClaimRecordError, match=expected):
        _run_create(postgres_database, body)
    with psycopg.connect(postgres_database) as conn:
        assert conn.execute(
            "SELECT count(*) FROM claim_records WHERE claim_id = %s",
            (body.calibration_result.claim_id,),
        ).fetchone()[0] == 0


@pytest.mark.parametrize(
    "spelling",
    ["uppercase", "compact"],
)
def test_uuid_spellings_are_canonical_and_idempotent(
    postgres_database,
    spelling,
):
    scope = _seed_request_scope(postgres_database)
    canonical_conversation_id = str(scope["conversation_id"])
    canonical_assistant_message_id = str(scope["assistant_message_id"])
    if spelling == "uppercase":
        supplied_conversation_id = canonical_conversation_id.upper()
        supplied_assistant_message_id = canonical_assistant_message_id.upper()
    else:
        supplied_conversation_id = canonical_conversation_id.replace("-", "")
        supplied_assistant_message_id = canonical_assistant_message_id.replace("-", "")
    reference = _reference(
        ref_type="external_source",
        ref_id="external-source-1",
        owner_id=scope["owner_id"],
        conversation_id=supplied_conversation_id,
    )
    body = _request(
        claim_id=f"claim_uuid_{spelling}",
        owner_id=scope["owner_id"],
        conversation_id=supplied_conversation_id,
        assistant_message_id=supplied_assistant_message_id,
        request_id=scope["request_id"],
        references=[reference],
    )

    created, first = _run_create(postgres_database, body)
    replay_created, replay = _run_create(postgres_database, body)

    assert created is True
    assert replay_created is False
    assert replay == first
    assert first.conversation_id == canonical_conversation_id
    assert first.assistant_message_id == canonical_assistant_message_id
    assert (
        first.validated_evidence_references[0].conversation_id
        == canonical_conversation_id
    )
    with psycopg.connect(postgres_database) as conn:
        stored = conn.execute(
            """
            SELECT conversation_id::text, assistant_message_id::text,
                   evidence_references_json, count(*) OVER ()
            FROM claim_records
            WHERE claim_id = %s
            """,
            (body.calibration_result.claim_id,),
        ).fetchone()
    assert stored[0] == canonical_conversation_id
    assert stored[1] == canonical_assistant_message_id
    assert stored[2][0]["conversation_id"] == canonical_conversation_id
    assert stored[3] == 1


def test_local_reference_types_require_existing_scoped_records(postgres_database):
    scope = _seed_request_scope(postgres_database)
    for index, (ref_type, ref_id) in enumerate(
        (
            ("message", scope["evidence_message_id"]),
            ("artifact", scope["artifact_id"]),
            ("derived_text", scope["derived_text_id"]),
        )
    ):
        reference = _reference(
            ref_type=ref_type,
            ref_id=str(ref_id),
            owner_id=scope["owner_id"],
            conversation_id=str(scope["conversation_id"]),
        )
        body = _request(
            claim_id=f"claim_local_{index}",
            references=[reference],
            **{
                key: scope[key]
                for key in ("owner_id", "conversation_id", "assistant_message_id", "request_id")
            },
        )
        created, record = _run_create(postgres_database, body)
        assert created is True
        assert record.validated_evidence_references[0].ref_type == ref_type


def test_same_claim_id_with_changed_payload_conflicts_and_preserves_original(postgres_database):
    scope = _seed_request_scope(postgres_database)
    reference = _reference(
        ref_type="external_source",
        ref_id="external-source-1",
        owner_id=scope["owner_id"],
        conversation_id=None,
    )
    body = _request(
        claim_id="claim_conflict",
        references=[reference],
        **{
            key: scope[key]
            for key in ("owner_id", "conversation_id", "assistant_message_id", "request_id")
        },
    )
    _run_create(postgres_database, body)
    changed_payload = body.model_copy(deep=True)
    changed_payload.calibration_result.user_safe_summary = "A different bounded summary."

    with pytest.raises(ClaimRecordError, match="claim_record_conflict"):
        _run_create(postgres_database, changed_payload)

    async def load():
        store = PostgresStore(postgres_database)
        await store.open()
        try:
            return await get_claim_record(
                store,
                claim_id=body.calibration_result.claim_id,
                owner_id=scope["owner_id"],
                conversation_id=str(scope["conversation_id"]),
            )
        finally:
            await store.close()

    stored = asyncio.run(load())
    assert stored.user_safe_summary == body.calibration_result.user_safe_summary


@pytest.mark.parametrize(
    ("mutation", "expected"),
    [
        ("conversation_owner", "conversation_not_found"),
        ("message_role", "assistant_message_not_assistant"),
        ("message_request", "assistant_message_request_mismatch"),
        ("trace_missing", "request_trace_not_found"),
        ("trace_surface", "request_trace_scope_mismatch"),
        ("trace_failed", "request_trace_not_eligible"),
        ("reference_absent", "evidence_reference_not_in_trace"),
    ],
)
def test_association_failures_insert_nothing(postgres_database, mutation, expected):
    scope = _seed_request_scope(postgres_database, request_id=f"request-{mutation}")
    reference = _reference(
        ref_type="external_source",
        ref_id="external-source-1",
        owner_id=scope["owner_id"],
        conversation_id=None,
    )
    body = _request(
        claim_id=f"claim_{mutation}",
        references=[reference],
        **{
            key: scope[key]
            for key in ("owner_id", "conversation_id", "assistant_message_id", "request_id")
        },
    )
    with psycopg.connect(postgres_database) as conn:
        if mutation == "conversation_owner":
            conn.execute(
                "UPDATE conversations SET owner_id = 'other-owner' WHERE id = %s",
                (scope["conversation_id"],),
            )
        elif mutation == "message_role":
            conn.execute(
                "UPDATE messages SET role = 'user' WHERE id = %s",
                (scope["assistant_message_id"],),
            )
        elif mutation == "message_request":
            conn.execute(
                "UPDATE messages SET metadata = '{\"request_id\":\"other\"}'::jsonb WHERE id = %s",
                (scope["assistant_message_id"],),
            )
        elif mutation == "trace_missing":
            conn.execute("DELETE FROM traces WHERE request_id = %s", (scope["request_id"],))
        elif mutation == "trace_surface":
            conn.execute(
                "UPDATE traces SET surface = 'other' WHERE request_id = %s",
                (scope["request_id"],),
            )
        elif mutation == "trace_failed":
            conn.execute(
                "UPDATE traces SET status = 'failed' WHERE request_id = %s",
                (scope["request_id"],),
            )
        else:
            body.calibration_result.validated_evidence_references[0].ref_id = "not-in-trace"
        conn.commit()

    with pytest.raises(ClaimRecordError, match=expected):
        _run_create(postgres_database, body)
    with psycopg.connect(postgres_database) as conn:
        assert conn.execute(
            "SELECT count(*) FROM claim_records WHERE claim_id = %s",
            (body.calibration_result.claim_id,),
        ).fetchone()[0] == 0


@pytest.mark.parametrize("ref_type", ["message", "artifact", "derived_text"])
def test_missing_or_wrong_scope_local_evidence_is_rejected(postgres_database, ref_type):
    scope = _seed_request_scope(postgres_database, request_id=f"request-local-{ref_type}")
    existing_id = {
        "message": scope["evidence_message_id"],
        "artifact": scope["artifact_id"],
        "derived_text": scope["derived_text_id"],
    }[ref_type]
    reference = _reference(
        ref_type=ref_type,
        ref_id=str(existing_id),
        owner_id=scope["owner_id"],
        conversation_id=str(scope["conversation_id"]),
    )
    body = _request(
        claim_id=f"claim_scope_{ref_type}",
        references=[reference],
        **{
            key: scope[key]
            for key in ("owner_id", "conversation_id", "assistant_message_id", "request_id")
        },
    )
    with psycopg.connect(postgres_database) as conn:
        if ref_type == "message":
            conn.execute("UPDATE messages SET owner_id = 'other-owner' WHERE id = %s", (existing_id,))
        elif ref_type == "artifact":
            conn.execute("UPDATE artifacts SET owner_id = 'other-owner' WHERE id = %s", (existing_id,))
        else:
            conn.execute(
                "UPDATE artifacts SET owner_id = 'other-owner' WHERE id = %s",
                (scope["artifact_id"],),
            )
        conn.commit()

    with pytest.raises(ClaimRecordError, match="evidence_reference_scope_mismatch"):
        _run_create(postgres_database, body)

    missing = body.model_copy(deep=True)
    missing.calibration_result.claim_id = f"claim_missing_{ref_type}"
    missing_id = str(uuid4())
    missing.calibration_result.validated_evidence_references[0].ref_id = missing_id
    with psycopg.connect(postgres_database) as conn:
        conn.execute(
            """
            UPDATE traces
            SET references_json = references_json || %s::jsonb
            WHERE request_id = %s
            """,
            (
                psycopg.types.json.Json([{"ref_type": ref_type, "ref_id": missing_id}]),
                scope["request_id"],
            ),
        )
        conn.commit()
    with pytest.raises(ClaimRecordError, match="evidence_reference_not_found"):
        _run_create(postgres_database, missing)


def test_scoped_read_and_filtered_list_are_deterministic(postgres_database):
    first_scope = _seed_request_scope(postgres_database, request_id="request-list-first")
    second_scope = _seed_request_scope(postgres_database, request_id="request-list-second")
    owner = first_scope["owner_id"]
    with psycopg.connect(postgres_database) as conn:
        conn.execute(
            "UPDATE conversations SET owner_id = %s WHERE id = %s",
            (owner, second_scope["conversation_id"]),
        )
        conn.commit()

    # Keep records in one conversation while using adjacent assistant responses.
    with psycopg.connect(postgres_database) as conn:
        conn.execute(
            "UPDATE messages SET conversation_id = %s, owner_id = %s WHERE id = %s",
            (first_scope["conversation_id"], owner, second_scope["assistant_message_id"]),
        )
        conn.execute(
            "UPDATE traces SET conversation_id = %s, owner_id = %s WHERE request_id = %s",
            (first_scope["conversation_id"], owner, second_scope["request_id"]),
        )
        conn.execute(
            "UPDATE messages SET created_at = '2026-07-14T22:00:00Z' WHERE id = %s",
            (first_scope["assistant_message_id"],),
        )
        conn.execute(
            "UPDATE messages SET created_at = '2026-07-14T23:00:00Z' WHERE id = %s",
            (second_scope["assistant_message_id"],),
        )
        conn.commit()

    async def create_and_list():
        store = PostgresStore(postgres_database)
        await store.open()
        try:
            for claim_id, scope in (
                ("claim_old_b", first_scope),
                ("claim_old_a", first_scope),
                ("claim_new", second_scope),
            ):
                body = _request(
                    claim_id=claim_id,
                    owner_id=owner,
                    conversation_id=first_scope["conversation_id"],
                    assistant_message_id=scope["assistant_message_id"],
                    request_id=scope["request_id"],
                    references=[
                        _reference(
                            ref_type="external_source",
                            ref_id="external-source-1",
                            owner_id=owner,
                            conversation_id=None,
                        )
                    ],
                )
                await create_claim_record(store, body)
            all_records = await list_claim_records(
                store,
                owner_id=owner,
                conversation_id=str(first_scope["conversation_id"]),
                assistant_message_id=None,
                request_id=None,
                limit=20,
            )
            selected = await list_claim_records(
                store,
                owner_id=owner,
                conversation_id=str(first_scope["conversation_id"]),
                assistant_message_id=str(first_scope["assistant_message_id"]),
                request_id=first_scope["request_id"],
                limit=20,
            )
            return all_records, selected
        finally:
            await store.close()

    all_records, selected = asyncio.run(create_and_list())
    assert [record.claim_id for record in all_records] == [
        "claim_new",
        "claim_old_b",
        "claim_old_a",
    ]
    assert {record.claim_id for record in selected} == {"claim_old_a", "claim_old_b"}

    async def isolated():
        store = PostgresStore(postgres_database)
        await store.open()
        try:
            return await store.get_claim_record(
                claim_id="claim_new",
                owner_id="other-owner",
                conversation_id=str(first_scope["conversation_id"]),
            )
        finally:
            await store.close()

    assert asyncio.run(isolated()) is None
