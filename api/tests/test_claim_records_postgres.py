from __future__ import annotations

import asyncio
from hashlib import sha256
from uuid import UUID, uuid4

import psycopg
import pytest

from models import ClaimRecordCreateRequest
from services.claim_records import (
    ClaimRecordError,
    create_claim_record,
    get_claim_record,
    list_claim_records,
)
from storage.postgres import PostgresStore


PRIVATE_ANSWER = "PRIVATE_ASSISTANT_ANSWER_SENTINEL"
PRIVATE_TRACE = "PRIVATE_TRACE_INTERNAL_SENTINEL"


def _calibration(
    *,
    claim_id: str,
    references: list[dict],
    anchor: str = "The selected setting changed.",
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
    conversation_id: UUID,
    assistant_message_id: UUID,
    request_id: str,
    references: list[dict],
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
) -> dict:
    conversation_id = uuid4()
    assistant_message_id = uuid4()
    user_message_id = uuid4()
    evidence_message_id = uuid4()
    artifact_id = uuid4()
    derived_text_id = uuid4()
    with psycopg.connect(dsn) as conn:
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
                psycopg.types.json.Json({"private": PRIVATE_TRACE}),
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
