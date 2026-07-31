from __future__ import annotations

import asyncio
import hashlib
import json
import types
from uuid import uuid4

from fastapi.testclient import TestClient
import httpx
import psycopg
import pytest

import main as main_module
from services.claim_records import validate_claim_record_association
from storage.postgres import (
    HistoryRootLineageValidationError,
    MessageAppendConflictError,
    PostgresStore,
)
from storage.qdrant import _policy_payload


class FakeQdrant:
    def __init__(self):
        self.upserts = []

    def ping(self):
        return True

    async def upsert_message_vector(self, **kwargs):
        self.upserts.append(kwargs)


def _settings():
    return types.SimpleNamespace(
        memory_api_key="testkey",
        require_request_id=True,
        enforce_request_id_header_body_match=True,
        enable_trace_storage=True,
        min_index_chars=3,
        index_assistant_messages=True,
        index_user_questions=True,
    )


def test_trace_create_and_retrieve_by_request_id_with_postgresql_16(
    monkeypatch,
    postgres_database,
):
    conversation_id = uuid4()
    with psycopg.connect(postgres_database) as conn:
        conn.execute(
            """
            INSERT INTO conversations (id, owner_id, client_id, title)
            VALUES (%s, %s, %s, %s)
            """,
            (conversation_id, "owner-fixture", "client-chat", "Trace fixture"),
        )
        conn.commit()

    store = PostgresStore(postgres_database)
    monkeypatch.setattr(main_module, "settings", _settings(), raising=True)
    monkeypatch.setattr(main_module, "pg", store, raising=True)
    monkeypatch.setattr(main_module, "qdrant", FakeQdrant(), raising=True)

    with TestClient(main_module.app) as client:
        request_id = "request-trace-postgres-001"
        trace = {
            "request_id": request_id,
            "conversation_id": str(conversation_id),
            "owner_id": "owner-fixture",
            "client_id": "client-chat",
            "surface": "chat",
            "profile": {"profile_ref": "default:1"},
            "retrieval": {
                "raw_ids": ["message-1"],
                "augmented_ids": ["message-1"],
                "fallback_to_raw_reasons": [],
            },
            "prompt": {
                "layers": ["profile", "retrieval", "current_turn"],
                "budget_enforcement": "not_enforced",
            },
            "router_decision": {"selected_provider": "local-primary"},
            "manual_override": {},
            "model_call": {"provider": "local-fallback", "status": "ok"},
            "model_calls": [
                {"provider": "local-primary", "status": "failed", "error": "bounded_failure"},
                {"provider": "local-fallback", "status": "ok"},
            ],
            "fallback": {"attempted": True, "selected": "local-fallback"},
            "artifacts": {"included_ids": [], "omission_reason": "not_requested"},
            "references": [{"ref_type": "message", "ref_id": "message-1"}],
            "cost": {},
            "latency_ms": 7,
            "status": "degraded",
            "error": None,
        }
        created = client.post(
            "/v1/traces",
            headers={"X-API-Key": "testkey", "X-Request-ID": request_id},
            json=trace,
        )
        assert created.status_code == 200, created.text

        retrieved = client.get(
            f"/v1/traces/{request_id}",
            headers={"X-API-Key": "testkey"},
        )
        assert retrieved.status_code == 200, retrieved.text
        body = retrieved.json()
        assert body["request_id"] == request_id
        assert body["conversation_id"] == str(conversation_id)
        assert body["prompt"]["budget_enforcement"] == "not_enforced"
        assert body["model_call"]["provider"] == "local-fallback"
        assert [attempt["status"] for attempt in body["model_calls"]] == ["failed", "ok"]
        assert body["references"] == [{"ref_type": "message", "ref_id": "message-1"}]
        assert body["status"] == "degraded"


def test_request_id_mismatch_persists_no_trace_with_postgresql_16(
    monkeypatch,
    postgres_database,
):
    store = PostgresStore(postgres_database)
    monkeypatch.setattr(main_module, "settings", _settings(), raising=True)
    monkeypatch.setattr(main_module, "pg", store, raising=True)
    monkeypatch.setattr(main_module, "qdrant", FakeQdrant(), raising=True)

    with TestClient(main_module.app) as client:
        body_request_id = "request-body-conflict"
        header_request_id = "request-header-conflict"
        response = client.post(
            "/v1/traces",
            headers={"X-API-Key": "testkey", "X-Request-ID": header_request_id},
            json={
                "request_id": body_request_id,
                "conversation_id": str(uuid4()),
                "owner_id": "owner-fixture",
                "surface": "chat",
                "profile": {},
                "retrieval": {},
                "router_decision": {},
                "model_call": {},
                "status": "ok",
            },
        )
        assert response.status_code == 400

    with psycopg.connect(postgres_database) as conn:
        count = conn.execute(
            "SELECT count(*) FROM traces WHERE request_id = ANY(%s)",
            ([body_request_id, header_request_id],),
        ).fetchone()[0]
    assert count == 0


def test_message_policy_metadata_round_trips_dedicated_column_with_postgresql_16(postgres_database):
    policy = {
        "memory_domains": ["technical"],
        "sensitivity": "low",
        "entity_ids": [],
        "relationship_ids": [],
        "relationship_scopes": [],
    }
    legacy_metadata = {"domain": "personal"}

    async def run():
        store = PostgresStore(postgres_database)
        await store.open()
        try:
            conversation_id = await store.create_conversation(
                owner_id="owner-policy",
                client_id="client-policy",
                title="Policy metadata",
            )
            message_id = await store.add_message(
                conversation_id=conversation_id,
                owner_id="owner-policy",
                role="assistant",
                content="Structured policy belongs in the dedicated column.",
                client_id="client-policy",
                metadata=legacy_metadata,
                policy_metadata=policy,
            )

            snippets = await store.get_message_snippets_by_ids([message_id])
            reindex_rows = await store.get_messages_for_reindex(owner_id="owner-policy")
        finally:
            await store.close()
        return str(message_id), snippets, reindex_rows

    message_id, snippets, reindex_rows = asyncio.run(run())

    assert len(snippets) == 1
    assert snippets[0]["message_id"] == message_id
    assert snippets[0]["metadata"] == legacy_metadata
    assert snippets[0]["policy_metadata"] == policy
    assert reindex_rows[0]["metadata"] == legacy_metadata
    assert reindex_rows[0]["policy_metadata"] == policy
    qdrant_payload = _policy_payload(reindex_rows[0]["policy_metadata"])
    assert qdrant_payload["retrieval_policy_valid"] is True
    assert qdrant_payload["memory_domains"] == ["technical"]


def test_working_message_queries_are_owner_qualified_before_limit_with_postgresql_16(postgres_database):
    policy = {
        "memory_domains": ["technical"],
        "sensitivity": "low",
        "entity_ids": [],
        "relationship_ids": [],
        "relationship_scopes": [],
    }
    policy_filter = {
        "allowed_domains": ["technical"],
        "blocked_domains": [],
        "allowed_sensitivities": ["low", "medium", "high"],
        "relationship_scope": {"applied": False},
    }

    async def run():
        store = PostgresStore(postgres_database)
        await store.open()
        try:
            conversation_id = await store.create_conversation(
                owner_id="owner-working",
                client_id="client-working",
                title="Working owner scope",
            )
            owner_message_id = await store.add_message(
                conversation_id=conversation_id,
                owner_id="owner-working",
                role="assistant",
                content="same owner working survives",
                client_id="client-working",
                policy_metadata=policy,
            )
            cross_owner_message_id = uuid4()
        finally:
            await store.close()
        return conversation_id, owner_message_id, cross_owner_message_id

    conversation_id, owner_message_id, cross_owner_message_id = asyncio.run(run())
    with psycopg.connect(postgres_database) as conn:
        conn.execute(
            """
            INSERT INTO messages (
              id, conversation_id, owner_id, client_id, role, content,
              policy_metadata, created_at
            ) VALUES (
              %s, %s, 'other-owner', 'client-working', 'assistant',
              'cross owner working must not consume limit', %s::jsonb, now()
            )
            """,
            (cross_owner_message_id, conversation_id, json.dumps(policy)),
        )
        conn.execute(
            """
            UPDATE messages
            SET created_at = CASE
              WHEN id = %s THEN now() - interval '1 hour'
              WHEN id = %s THEN now()
              ELSE created_at
            END
            WHERE id = ANY(%s)
            """,
            (owner_message_id, cross_owner_message_id, [owner_message_id, cross_owner_message_id]),
        )
        conn.commit()

    async def read():
        store = PostgresStore(postgres_database)
        await store.open()
        try:
            mandatory = await store.get_recent_message_items(
                conversation_id=conversation_id,
                owner_id="owner-working",
                limit=1,
                policy_filter=policy_filter,
            )
            legacy = await store.get_recent_message_snippets(
                conversation_id=conversation_id,
                owner_id="owner-working",
                limit=1,
            )
        finally:
            await store.close()
        return mandatory, legacy

    mandatory, legacy = asyncio.run(read())

    assert [item["message_id"] for item in mandatory] == [str(owner_message_id)]
    assert mandatory[0]["policy_metadata"] == policy
    assert [item["message_id"] for item in legacy] == [str(owner_message_id)]


def _acquisition_history_manifest(message_id: str, content: str) -> dict:
    response_digest = "sha256:" + hashlib.sha256(content.encode("utf-8")).hexdigest()
    return {
        "enabled": True,
        "attempted": True,
        "status": "insufficient",
        "manifest_id": "evidence_manifest_postgres",
        "assistant_message_id": message_id,
        "response_digest": response_digest,
        "shape": {
            "derivation_status": "derived",
            "task_shape": "targeted_lookup",
            "candidate_count": 1,
            "clarification_required": False,
            "reason_codes": ["shape_derived"],
        },
        "inventory": {
            "inventory_status": "complete_for_declared_scope",
            "inventory_source_count": 1,
        },
        "plan": {
            "plan_id": "evidence_plan_postgres",
            "plan_status": "ready",
            "completeness_expectation": "targeted_scope",
            "contradiction_search_required": False,
            "selected_strategies": ["targeted_retrieval"],
            "material_requirement_count": 2,
            "optional_requirement_count": 0,
            "limitation_codes": [],
        },
        "acquisition": {
            "strategy_attempted": "targeted_retrieval",
            "sources_considered": [],
            "sources_considered_count": 1,
            "sources_selected": [],
            "sources_selected_count": 1,
            "sources_used": [],
            "sources_used_count": 1,
            "source_references_retained": [],
            "source_references_retained_count": 0,
            "source_identifiers_suppressed": True,
            "item_count": 1,
            "prompt_retained_item_count": 0,
            "dsa_outcome": "ok",
            "dsa_error_codes": [],
            "context_delivery_status": "filtered",
            "requirement_facts": [],
        },
        "next_steps": {
            "selection_count": 1,
            "selections": [
                {
                    "selection_id": "evidence_next_step_postgres",
                    "selected_next_step": "withhold_unsupported_conclusion",
                    "conclusion_disposition": "requested_conclusion_withheld",
                    "provider_disposition": "blocked",
                    "reacquisition_guard": "not_applicable",
                    "clarification_target": None,
                    "reason_codes": ["unsupported_conclusion_withheld"],
                    "additional_acquisition_executed": False,
                }
            ],
            "additional_acquisition_count": 0,
            "initial_attempt": None,
            "dependency_status": None,
        },
        "sufficiency": {
            "evaluation_id": "evidence_eval_postgres",
            "status": "insufficient",
            "reason_codes": ["material_requirement_not_satisfied"],
            "answer_constraints": ["withhold_unqualified_conclusion"],
            "qualification_required": True,
            "additional_acquisition_required": True,
        },
    }


def test_assistant_trace_candidates_use_scoped_left_join_and_newest_order(
    postgres_database,
):
    async def seed_and_read():
        store = PostgresStore(postgres_database)
        await store.open()
        try:
            conversation_id = await store.create_conversation(
                owner_id="owner-history",
                client_id="client-history",
                title="Acquisition history",
            )
            other_conversation_id = await store.create_conversation(
                owner_id="owner-history",
                client_id="client-history",
                title="Other history",
            )
            older_id = await store.add_message(
                conversation_id=conversation_id,
                owner_id="owner-history",
                role="assistant",
                content="Older assistant response.",
                client_id="client-history",
                metadata={"request_id": "history-request-old"},
            )
            newest_id = await store.add_message(
                conversation_id=conversation_id,
                owner_id="owner-history",
                role="assistant",
                content="Newest assistant response without a trace.",
                client_id="client-history",
                metadata={"request_id": "history-request-new"},
            )
            await store.add_message(
                conversation_id=conversation_id,
                owner_id="owner-history",
                role="user",
                content="Newer user message is not a candidate.",
                client_id="client-history",
                metadata={"request_id": "history-request-user"},
            )
            cross_owner_id = uuid4()
            await store.add_message(
                conversation_id=other_conversation_id,
                owner_id="owner-history",
                role="assistant",
                content="Other-conversation assistant response.",
                client_id="client-history",
                metadata={"request_id": "history-request-other-conversation"},
            )
            await store.create_trace(
                {
                    "request_id": "history-request-old",
                    "conversation_id": conversation_id,
                    "owner_id": "owner-history",
                    "surface": "web",
                    "status": "ok",
                    "prompt": {},
                }
            )
            with psycopg.connect(postgres_database) as conn:
                conn.execute(
                    """
                    INSERT INTO messages (
                      id, conversation_id, owner_id, client_id, role, content,
                      metadata, created_at
                    ) VALUES (
                      %s, %s, 'other-owner', 'client-history', 'assistant',
                      'Cross-owner assistant response.', %s::jsonb,
                      now() + interval '1 minute'
                    )
                    """,
                    (
                        cross_owner_id,
                        conversation_id,
                        json.dumps({"request_id": "history-request-cross-owner"}),
                    ),
                )
                conn.execute(
                    """
                    UPDATE messages
                    SET created_at = CASE
                      WHEN id = %s THEN now() - interval '1 minute'
                      WHEN id = %s THEN now()
                      ELSE created_at
                    END
                    WHERE id = ANY(%s)
                    """,
                    (older_id, newest_id, [older_id, newest_id]),
                )
                conn.commit()
            candidates = await store.list_assistant_trace_candidates(
                owner_id="owner-history",
                conversation_id=conversation_id,
                limit=50,
            )
        finally:
            await store.close()
        return str(newest_id), str(older_id), candidates

    newest_id, older_id, candidates = asyncio.run(seed_and_read())

    assert [item["message_id"] for item in candidates] == [newest_id, older_id]
    assert candidates[0]["trace_id"] is None
    assert candidates[0]["trace_request_id"] is None
    assert candidates[0]["message_request_id"] == "history-request-new"
    assert candidates[1]["trace_request_id"] == "history-request-old"
    assert candidates[1]["message_request_id"] == "history-request-old"
    assert candidates[1]["trace_prompt"] == {}


def test_assistant_trace_candidates_use_message_id_as_deterministic_tiebreaker(
    postgres_database,
):
    conversation_id = uuid4()
    first_id = uuid4()
    second_id = uuid4()
    with psycopg.connect(postgres_database) as conn:
        conn.execute(
            """
            INSERT INTO conversations (id, owner_id, client_id, title)
            VALUES (%s, %s, %s, %s)
            """,
            (conversation_id, "owner-history-tie", "client-history", "Tie order"),
        )
        conn.execute(
            """
            INSERT INTO messages (
                id, conversation_id, owner_id, client_id, role, content,
                metadata, created_at
            ) VALUES
              (%s, %s, %s, %s, 'assistant', %s, %s::jsonb, %s),
              (%s, %s, %s, %s, 'assistant', %s, %s::jsonb, %s)
            """,
            (
                first_id,
                conversation_id,
                "owner-history-tie",
                "client-history",
                "First tie response.",
                '{"request_id":"history-tie-first"}',
                "2026-07-20T00:00:00+00:00",
                second_id,
                conversation_id,
                "owner-history-tie",
                "client-history",
                "Second tie response.",
                '{"request_id":"history-tie-second"}',
                "2026-07-20T00:00:00+00:00",
            ),
        )
        conn.commit()

    async def read():
        store = PostgresStore(postgres_database)
        await store.open()
        try:
            return await store.list_assistant_trace_candidates(
                owner_id="owner-history-tie",
                conversation_id=conversation_id,
                limit=50,
            )
        finally:
            await store.close()

    candidates = asyncio.run(read())
    assert [item["message_id"] for item in candidates] == sorted(
        [str(first_id), str(second_id)],
        reverse=True,
    )
    assert all(item["trace_id"] is None for item in candidates)


def test_acquisition_history_resolves_without_claim_and_performs_no_writes(
    monkeypatch,
    postgres_database,
):
    conversation_id = uuid4()
    message_id = uuid4()
    content = (
        "The available evidence remains incomplete.\n\n"
        "I’m withholding the requested conclusion."
    )
    manifest = _acquisition_history_manifest(str(message_id), content)
    with psycopg.connect(postgres_database) as conn:
        conn.execute(
            """
            INSERT INTO conversations (id, owner_id, client_id, title)
            VALUES (%s, %s, %s, %s)
            """,
            (conversation_id, "owner-history", "client-history", "History API"),
        )
        conn.execute(
            """
            INSERT INTO messages (
                id, conversation_id, owner_id, client_id, role, content, metadata
            ) VALUES (%s, %s, %s, %s, 'assistant', %s, %s::jsonb)
            """,
            (
                message_id,
                conversation_id,
                "owner-history",
                "client-history",
                content,
                '{"request_id":"history-request-api"}',
            ),
        )
        conn.commit()

    async def seed_trace():
        store = PostgresStore(postgres_database)
        await store.open()
        try:
            await store.create_trace(
                {
                    "request_id": "history-request-api",
                    "conversation_id": conversation_id,
                    "owner_id": "owner-history",
                    "surface": "web",
                    "status": "ok",
                    "prompt": {
                        "evidence_acquisition": manifest,
                        "unrelated": {"safe_count": 3},
                    },
                    "profile": {"private": "PROFILE SENTINEL"},
                    "retrieval": {"private": "RETRIEVAL SENTINEL"},
                    "router_decision": {"private": "ROUTER SENTINEL"},
                    "model_call": {"private": "MODEL SENTINEL"},
                    "fallback": {"private": "FALLBACK SENTINEL"},
                    "cost": {"private": "COST SENTINEL"},
                }
            )
        finally:
            await store.close()

    asyncio.run(seed_trace())
    store = PostgresStore(postgres_database)
    monkeypatch.setattr(main_module, "settings", _settings(), raising=True)
    monkeypatch.setattr(main_module, "pg", store, raising=True)
    monkeypatch.setattr(main_module, "qdrant", FakeQdrant(), raising=True)

    with psycopg.connect(postgres_database) as conn:
        before = conn.execute(
            """
            SELECT
              (SELECT count(*) FROM messages),
              (SELECT count(*) FROM traces),
              (SELECT count(*) FROM claim_records)
            """
        ).fetchone()

    with TestClient(main_module.app) as client:
        response = client.post(
            "/v1/internal/acquisition-history/resolve",
            headers={
                "X-API-Key": "testkey",
                "X-Request-ID": "history-lookup-api",
            },
            json={
                "schema_version": "acquisition-history-resolution.v1",
                "request_id": "history-lookup-api",
                "owner_id": "owner-history",
                "conversation_id": str(conversation_id),
                "surface": "web",
                "target_mode": "immediate_previous",
                "response_digest": "sha256:"
                + hashlib.sha256(content.encode("utf-8")).hexdigest(),
                "normalized_first_paragraph": (
                    "The available evidence remains incomplete."
                ),
            },
        )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["resolution_status"] == "resolved"
    assert body["record"]["acquisition_manifest"] == manifest
    assert body["record"]["normalized_first_paragraph"] == (
        "The available evidence remains incomplete."
    )
    serialized = response.text
    assert "I’m withholding the requested conclusion." not in serialized
    for sentinel in (
        "PROFILE SENTINEL",
        "RETRIEVAL SENTINEL",
        "ROUTER SENTINEL",
        "MODEL SENTINEL",
        "FALLBACK SENTINEL",
        "COST SENTINEL",
    ):
        assert sentinel not in serialized

    with psycopg.connect(postgres_database) as conn:
        after = conn.execute(
            """
            SELECT
              (SELECT count(*) FROM messages),
              (SELECT count(*) FROM traces),
              (SELECT count(*) FROM claim_records)
            """
        ).fetchone()
    assert before == after


def test_immediate_history_resolves_newest_support_and_acquisition_without_writes(
    monkeypatch,
    postgres_database,
):
    owner_id = "owner-immediate-history"
    surface = "telegram"
    conversation_id = uuid4()
    older_message_id = uuid4()
    newest_message_id = uuid4()
    older_request_id = "immediate-history-older"
    newest_request_id = "immediate-history-newest"
    older_content = "An older response has a retained record."
    newest_content = (
        "The retained record supports the newest response.\n\n"
        "This full response remains server-owned."
    )
    manifest = _acquisition_history_manifest(str(newest_message_id), newest_content)

    with psycopg.connect(postgres_database) as conn:
        conn.execute(
            """
            INSERT INTO conversations (id, owner_id, client_id, title)
            VALUES (%s, %s, %s, %s)
            """,
            (conversation_id, owner_id, "telegram:fixture", "Immediate history"),
        )
        conn.execute(
            """
            INSERT INTO messages (
                id, conversation_id, owner_id, client_id, role, content,
                metadata, created_at
            ) VALUES
              (%s, %s, %s, %s, 'assistant', %s, %s::jsonb, %s),
              (%s, %s, %s, %s, 'assistant', %s, %s::jsonb, %s)
            """,
            (
                older_message_id,
                conversation_id,
                owner_id,
                "telegram:fixture",
                older_content,
                '{"request_id":"immediate-history-older"}',
                "2026-07-20T00:00:00+00:00",
                newest_message_id,
                conversation_id,
                owner_id,
                "telegram:fixture",
                newest_content,
                '{"request_id":"immediate-history-newest"}',
                "2026-07-20T00:01:00+00:00",
            ),
        )
        conn.commit()

    async def seed_records():
        store = PostgresStore(postgres_database)
        await store.open()
        try:
            await store.create_trace(
                {
                    "request_id": newest_request_id,
                    "conversation_id": conversation_id,
                    "owner_id": owner_id,
                    "surface": surface,
                    "status": "ok",
                    "prompt": {"evidence_acquisition": manifest},
                    "references": [
                        {
                            "ref_type": "integration_event",
                            "ref_id": "retained-event-postgres",
                        }
                    ],
                }
            )
            anchor = "The retained record supports the newest response."
            await store.create_claim_record(
                record={
                    "claim_id": "claim_immediate_history_postgres",
                    "schema_version": "claim-record.v1",
                    "owner_id": owner_id,
                    "conversation_id": str(conversation_id),
                    "request_id": newest_request_id,
                    "assistant_message_id": str(newest_message_id),
                    "surface": surface,
                    "runtime_session_id": "runtime-session-postgres",
                    "runtime_turn_id": "runtime-turn-postgres",
                    "acquisition_manifest_id": None,
                    "claim_anchor": anchor,
                    "claim_anchor_digest": "sha256:"
                    + hashlib.sha256(anchor.encode("utf-8")).hexdigest(),
                    "claim_class": "source_backed_fact",
                    "calibration_status": "supported",
                    "evidence_strength": "strong",
                    "confidence": "high",
                    "strongest_authority": "trusted_integration",
                    "freshness_summary": "current",
                    "uncertainty_disclosure_required": False,
                    "validated_evidence_references": [
                        {
                            "ref_type": "integration_event",
                            "ref_id": "retained-event-postgres",
                            "owner_id": owner_id,
                            "conversation_id": str(conversation_id),
                            "support_kind": "direct",
                            "authority": "trusted_integration",
                            "freshness_state": "active",
                        }
                    ],
                    "limitation_codes": [],
                    "user_safe_summary": "One retained event directly supports it.",
                },
                validate_association=validate_claim_record_association,
            )
        finally:
            await store.close()

    asyncio.run(seed_records())

    with psycopg.connect(postgres_database) as conn:
        before = conn.execute(
            """
            SELECT
              (SELECT count(*) FROM messages),
              (SELECT count(*) FROM traces),
              (SELECT count(*) FROM claim_records)
            """
        ).fetchone()

    monkeypatch.setattr(main_module, "settings", _settings(), raising=True)
    monkeypatch.setattr(main_module, "qdrant", FakeQdrant(), raising=True)

    async def resolve(explanation_kind: str):
        store = PostgresStore(postgres_database)
        monkeypatch.setattr(main_module, "pg", store, raising=True)
        await store.open()
        try:
            transport = httpx.ASGITransport(app=main_module.app)
            async with httpx.AsyncClient(
                transport=transport,
                base_url="http://testserver",
            ) as client:
                request_id = f"immediate-history-{explanation_kind}-lookup"
                return await client.post(
                    "/v1/internal/immediate-history/resolve",
                    headers={
                        "X-API-Key": "testkey",
                        "X-Request-ID": request_id,
                    },
                    json={
                        "schema_version": "immediate-history-resolution.v1",
                        "request_id": request_id,
                        "owner_id": owner_id,
                        "conversation_id": str(conversation_id),
                        "surface": surface,
                        "explanation_kind": explanation_kind,
                    },
                )
        finally:
            await store.close()

    support_response = asyncio.run(resolve("support"))
    acquisition_response = asyncio.run(resolve("acquisition"))

    assert support_response.status_code == 200, support_response.text
    support = support_response.json()
    assert support["reason_code"] == "support_record_resolved"
    assert support["record"]["assistant_message_id"] == str(newest_message_id)
    assert support["record"]["original_request_id"] == newest_request_id
    assert support["record"]["support_record"]["claim_id"] == (
        "claim_immediate_history_postgres"
    )
    assert older_request_id not in support_response.text
    assert "This full response remains server-owned." not in support_response.text

    assert acquisition_response.status_code == 200, acquisition_response.text
    acquisition = acquisition_response.json()
    assert acquisition["reason_code"] == "acquisition_record_resolved"
    assert acquisition["record"]["assistant_message_id"] == str(newest_message_id)
    assert acquisition["record"]["acquisition_record"]["acquisition_manifest"] == manifest
    assert older_request_id not in acquisition_response.text
    assert "This full response remains server-owned." not in acquisition_response.text

    with psycopg.connect(postgres_database) as conn:
        after = conn.execute(
            """
            SELECT
              (SELECT count(*) FROM messages),
              (SELECT count(*) FROM traces),
              (SELECT count(*) FROM claim_records)
            """
        ).fetchone()
    assert before == after


def _seed_history_root(
    postgres_database: str,
    *,
    record_kind: str,
    owner_id: str | None = None,
    surface: str = "telegram",
) -> dict:
    owner_id = owner_id or f"owner-lineage-{uuid4().hex[:8]}"
    request_id = f"root-request-{uuid4().hex}"
    content = "The retained root record supports this response."

    async def seed():
        store = PostgresStore(postgres_database)
        await store.open()
        try:
            conversation_id = await store.create_conversation(
                owner_id=owner_id,
                client_id="telegram:lineage-fixture",
                title="History root lineage",
            )
            message_id = await store.add_message(
                conversation_id=conversation_id,
                owner_id=owner_id,
                role="assistant",
                content=content,
                client_id="telegram:lineage-fixture",
                metadata={"request_id": request_id},
            )
            prompt = (
                {
                    "evidence_acquisition": _acquisition_history_manifest(
                        str(message_id), content
                    )
                }
                if record_kind == "acquisition"
                else {}
            )
            references = (
                [
                    {
                        "ref_type": "integration_event",
                        "ref_id": "retained-lineage-event",
                    }
                ]
                if record_kind == "support"
                else []
            )
            await store.create_trace(
                {
                    "request_id": request_id,
                    "conversation_id": conversation_id,
                    "owner_id": owner_id,
                    "surface": surface,
                    "status": "ok",
                    "prompt": prompt,
                    "references": references,
                }
            )
            if record_kind == "support":
                await store.create_claim_record(
                    record={
                        "claim_id": f"claim_lineage_{uuid4().hex}",
                        "schema_version": "claim-record.v1",
                        "owner_id": owner_id,
                        "conversation_id": str(conversation_id),
                        "request_id": request_id,
                        "assistant_message_id": str(message_id),
                        "surface": surface,
                        "runtime_session_id": "runtime-session-lineage",
                        "runtime_turn_id": "runtime-turn-lineage",
                        "acquisition_manifest_id": None,
                        "claim_anchor": content,
                        "claim_anchor_digest": "sha256:"
                        + hashlib.sha256(content.encode("utf-8")).hexdigest(),
                        "claim_class": "source_backed_fact",
                        "calibration_status": "supported",
                        "evidence_strength": "strong",
                        "confidence": "high",
                        "strongest_authority": "trusted_integration",
                        "freshness_summary": "current",
                        "uncertainty_disclosure_required": False,
                        "validated_evidence_references": [
                            {
                                "ref_type": "integration_event",
                                "ref_id": "retained-lineage-event",
                                "owner_id": owner_id,
                                "conversation_id": str(conversation_id),
                                "support_kind": "direct",
                                "authority": "trusted_integration",
                                "freshness_state": "active",
                            }
                        ],
                        "limitation_codes": [],
                        "user_safe_summary": "One retained event supports it.",
                    },
                    validate_association=validate_claim_record_association,
                )
        finally:
            await store.close()
        return conversation_id, message_id

    conversation_id, message_id = asyncio.run(seed())
    return {
        "owner_id": owner_id,
        "conversation_id": conversation_id,
        "message_id": message_id,
        "request_id": request_id,
        "content": content,
        "surface": surface,
        "record_kind": record_kind,
    }


def _lineage_payload(root: dict, *, record_kind: str | None = None) -> dict:
    return {
        "schema_version": "history-root-lineage.v1",
        "root_assistant_message_id": str(root["message_id"]),
        "record_kind": record_kind or root["record_kind"],
    }


def _append_through_api(
    monkeypatch,
    postgres_database: str,
    *,
    conversation_id,
    body: dict,
):
    store = PostgresStore(postgres_database)
    qdrant = FakeQdrant()
    monkeypatch.setattr(main_module, "settings", _settings(), raising=True)
    monkeypatch.setattr(main_module, "pg", store, raising=True)
    monkeypatch.setattr(main_module, "qdrant", qdrant, raising=True)
    with TestClient(main_module.app) as client:
        response = client.post(
            f"/v1/conversations/{conversation_id}/messages",
            headers={"X-API-Key": "testkey"},
            json=body,
        )
    return response, qdrant


def _resolve_v2_through_api(
    monkeypatch,
    postgres_database: str,
    *,
    root: dict,
    explanation_kind: str,
    surface: str | None = None,
):
    request_id = f"lineage-resolution-{uuid4().hex}"
    store = PostgresStore(postgres_database)
    monkeypatch.setattr(main_module, "settings", _settings(), raising=True)
    monkeypatch.setattr(main_module, "pg", store, raising=True)
    monkeypatch.setattr(main_module, "qdrant", FakeQdrant(), raising=True)
    with TestClient(main_module.app) as client:
        return client.post(
            "/v1/internal/immediate-history/resolve",
            headers={
                "X-API-Key": "testkey",
                "X-Request-ID": request_id,
            },
            json={
                "schema_version": "immediate-history-resolution.v2",
                "request_id": request_id,
                "owner_id": root["owner_id"],
                "conversation_id": str(root["conversation_id"]),
                "surface": surface or root["surface"],
                "explanation_kind": explanation_kind,
            },
        )


def test_message_append_without_lineage_remains_unchanged_with_postgresql_16(
    monkeypatch,
    postgres_database,
):
    root = _seed_history_root(postgres_database, record_kind="support")
    response, _ = _append_through_api(
        monkeypatch,
        postgres_database,
        conversation_id=root["conversation_id"],
        body={
            "owner_id": root["owner_id"],
            "role": "assistant",
            "content": "Ordinary assistant message.",
            "client_id": "telegram:lineage-fixture",
            "metadata": {"request_id": "ordinary-append"},
        },
    )

    assert response.status_code == 200, response.text
    assert set(response.json()) == {"message_id"}
    with psycopg.connect(postgres_database) as conn:
        metadata = conn.execute(
            "SELECT metadata FROM messages WHERE id = %s",
            (response.json()["message_id"],),
        ).fetchone()[0]
    assert metadata == {"request_id": "ordinary-append"}


def test_message_append_without_lineage_or_metadata_remains_unchanged(
    monkeypatch,
    postgres_database,
):
    root = _seed_history_root(postgres_database, record_kind="support")
    response, qdrant = _append_through_api(
        monkeypatch,
        postgres_database,
        conversation_id=root["conversation_id"],
        body={
            "owner_id": root["owner_id"],
            "role": "assistant",
            "content": "Ordinary assistant message without metadata.",
            "client_id": "telegram:lineage-fixture",
        },
    )

    assert response.status_code == 200, response.text
    assert set(response.json()) == {"message_id"}
    assert len(qdrant.upserts) == 1
    with psycopg.connect(postgres_database) as conn:
        metadata = conn.execute(
            "SELECT metadata FROM messages WHERE id = %s",
            (response.json()["message_id"],),
        ).fetchone()[0]
    assert metadata is None


@pytest.mark.parametrize("record_kind", ["support", "acquisition"])
def test_valid_lineage_append_is_private_durable_and_resolvable_after_reopen(
    monkeypatch,
    postgres_database,
    record_kind,
):
    root = _seed_history_root(postgres_database, record_kind=record_kind)
    lineage = _lineage_payload(root)
    policy = {
        "memory_domains": ["technical"],
        "sensitivity": "low",
        "content_class": None,
        "entity_ids": [],
        "relationship_ids": [],
        "relationship_scopes": [],
    }
    response, qdrant = _append_through_api(
        monkeypatch,
        postgres_database,
        conversation_id=root["conversation_id"],
        body={
            "owner_id": root["owner_id"],
            "role": "assistant",
            "content": "A historical explanation backed by the original root.",
            "client_id": "telegram:lineage-fixture",
            "metadata": {
                "request_id": "historical-explanation-append",
                "selected_model": "not_called",
                "response_kind": "claim_explanation",
            },
            "history_root_lineage": lineage,
            "policy_metadata": policy,
        },
    )

    assert response.status_code == 200, response.text
    assert set(response.json()) == {"message_id"}
    assert "history_root_lineage" not in response.text
    assert len(qdrant.upserts) == 1
    message_id = response.json()["message_id"]
    with psycopg.connect(postgres_database) as conn:
        metadata, policy_metadata = conn.execute(
            "SELECT metadata, policy_metadata FROM messages WHERE id = %s",
            (message_id,),
        ).fetchone()
    assert metadata == {
        "request_id": "historical-explanation-append",
        "selected_model": "not_called",
        "response_kind": "claim_explanation",
        "history_root_lineage": lineage,
    }
    assert policy_metadata == policy

    resolved = _resolve_v2_through_api(
        monkeypatch,
        postgres_database,
        root=root,
        explanation_kind=record_kind,
    )
    assert resolved.status_code == 200, resolved.text
    result = resolved.json()
    assert result["resolution_status"] == "resolved"
    assert result["resolution_source"] == "root_lineage"
    assert result["lineage_dereference_count"] == 1
    assert result["record"]["assistant_message_id"] == str(root["message_id"])
    assert result["history_root_lineage"] == lineage


def test_supplied_identity_lineage_retry_and_mismatch_are_bounded(
    monkeypatch,
    postgres_database,
):
    root = _seed_history_root(postgres_database, record_kind="support")
    message_id = uuid4()
    lineage = _lineage_payload(root)
    policy = {
        "memory_domains": ["technical"],
        "sensitivity": "low",
        "content_class": None,
        "entity_ids": [],
        "relationship_ids": [],
        "relationship_scopes": [],
    }
    body = {
        "message_id": str(message_id),
        "owner_id": root["owner_id"],
        "role": "assistant",
        "content": "Durable explanation with exact lineage.",
        "client_id": "web:lineage-fixture",
        "metadata": {
            "request_id": "durable-lineage-append",
            "response_kind": "claim_explanation",
        },
        "history_root_lineage": lineage,
        "policy_metadata": policy,
    }
    first, qdrant = _append_through_api(
        monkeypatch,
        postgres_database,
        conversation_id=root["conversation_id"],
        body=body,
    )
    with psycopg.connect(postgres_database) as conn:
        after_first = conn.execute(
            "SELECT updated_at FROM conversations WHERE id = %s",
            (root["conversation_id"],),
        ).fetchone()[0]
    retry, retry_qdrant = _append_through_api(
        monkeypatch,
        postgres_database,
        conversation_id=root["conversation_id"],
        body={
            **body,
            "metadata": {
                "response_kind": "claim_explanation",
                "request_id": "durable-lineage-append",
            },
        },
    )
    with psycopg.connect(postgres_database) as conn:
        after_retry = conn.execute(
            "SELECT updated_at FROM conversations WHERE id = %s",
            (root["conversation_id"],),
        ).fetchone()[0]
        rows = conn.execute(
            "SELECT metadata, policy_metadata FROM messages WHERE id = %s",
            (message_id,),
        ).fetchall()

    assert first.status_code == retry.status_code == 200
    assert first.json() == retry.json() == {"message_id": str(message_id)}
    assert after_retry == after_first
    assert rows == [
        (
            {
                "request_id": "durable-lineage-append",
                "response_kind": "claim_explanation",
                "history_root_lineage": lineage,
            },
            policy,
        )
    ]
    assert qdrant.upserts[0]["message_id"] == message_id
    assert retry_qdrant.upserts[0]["message_id"] == message_id

    changed_content, changed_content_qdrant = _append_through_api(
        monkeypatch,
        postgres_database,
        conversation_id=root["conversation_id"],
        body={**body, "content": "PRIVATE CHANGED EXPLANATION"},
    )
    changed_lineage, changed_lineage_qdrant = _append_through_api(
        monkeypatch,
        postgres_database,
        conversation_id=root["conversation_id"],
        body={
            **body,
            "history_root_lineage": {**lineage, "record_kind": "acquisition"},
        },
    )
    for response in (changed_content, changed_lineage):
        assert response.status_code == 409
        assert response.json() == {"detail": MessageAppendConflictError.code}
        assert "PRIVATE CHANGED EXPLANATION" not in response.text
        assert str(root["message_id"]) not in response.text
    assert changed_content_qdrant.upserts == []
    assert changed_lineage_qdrant.upserts == []
    with psycopg.connect(postgres_database) as conn:
        assert conn.execute(
            "SELECT count(*) FROM messages WHERE id = %s",
            (message_id,),
        ).fetchone()[0] == 1
        assert conn.execute(
            "SELECT updated_at FROM conversations WHERE id = %s",
            (root["conversation_id"],),
        ).fetchone()[0] == after_first


@pytest.mark.parametrize(
    "mutation",
    [
        "reserved_metadata",
        "non_assistant",
        "malformed",
        "unsupported_version",
        "extra_field",
        "invalid_type",
        "overlong_root",
        "missing_root",
    ],
)
def test_invalid_lineage_append_is_bounded_atomic_and_leaks_no_root(
    monkeypatch,
    postgres_database,
    mutation,
):
    root = _seed_history_root(postgres_database, record_kind="support")
    lineage = _lineage_payload(root)
    role = "assistant"
    metadata = {
        "request_id": "rejected-lineage-append",
        "ordinary": "PRESERVED ROOT METADATA SENTINEL",
    }
    if mutation == "reserved_metadata":
        metadata["history_root_lineage"] = lineage
        supplied_lineage = None
    elif mutation == "non_assistant":
        role = "user"
        supplied_lineage = lineage
    elif mutation == "malformed":
        supplied_lineage = "PRIVATE MALFORMED LINEAGE SENTINEL"
    elif mutation == "unsupported_version":
        supplied_lineage = {**lineage, "schema_version": "history-root-lineage.v2"}
    elif mutation == "extra_field":
        supplied_lineage = {**lineage, "private_extra": "PRIVATE LINEAGE EXTRA"}
    elif mutation == "invalid_type":
        supplied_lineage = {**lineage, "root_assistant_message_id": 17}
    elif mutation == "overlong_root":
        supplied_lineage = {**lineage, "root_assistant_message_id": "x" * 121}
    elif mutation == "missing_root":
        supplied_lineage = {
            **lineage,
            "root_assistant_message_id": str(uuid4()),
        }
    else:
        raise AssertionError(mutation)
    body = {
        "owner_id": root["owner_id"],
        "role": role,
        "content": "This rejected explanation must not persist.",
        "metadata": metadata,
    }
    if supplied_lineage is not None:
        body["history_root_lineage"] = supplied_lineage

    with psycopg.connect(postgres_database) as conn:
        before_count = conn.execute("SELECT count(*) FROM messages").fetchone()[0]
        before_root_metadata = conn.execute(
            "SELECT metadata FROM messages WHERE id = %s",
            (root["message_id"],),
        ).fetchone()[0]
    response, _ = _append_through_api(
        monkeypatch,
        postgres_database,
        conversation_id=root["conversation_id"],
        body=body,
    )

    assert response.status_code == 422
    assert response.json() == {"detail": "history_root_lineage_invalid"}
    for sentinel in (
        str(root["message_id"]),
        json.dumps(lineage, sort_keys=True),
        "PRIVATE MALFORMED LINEAGE SENTINEL",
        "PRIVATE LINEAGE EXTRA",
        "PRESERVED ROOT METADATA SENTINEL",
    ):
        assert sentinel not in response.text
    with psycopg.connect(postgres_database) as conn:
        after_count = conn.execute("SELECT count(*) FROM messages").fetchone()[0]
        after_root_metadata = conn.execute(
            "SELECT metadata FROM messages WHERE id = %s",
            (root["message_id"],),
        ).fetchone()[0]
    assert after_count == before_count
    assert after_root_metadata == before_root_metadata


@pytest.mark.parametrize(
    "mutation",
    [
        "metadata_omitted",
        "metadata_null",
        "request_id_absent",
        "request_id_null",
        "request_id_not_string",
        "request_id_empty",
        "request_id_forbidden_characters",
        "request_id_overlong",
        "content_empty",
    ],
)
def test_lineage_append_rejects_unusable_explanation_identity_atomically(
    monkeypatch,
    postgres_database,
    mutation,
):
    root = _seed_history_root(postgres_database, record_kind="support")
    lineage = _lineage_payload(root)
    body = {
        "owner_id": root["owner_id"],
        "role": "assistant",
        "content": "Explanation identity must be reusable by history resolution.",
        "history_root_lineage": lineage,
    }
    submitted_request_id = None
    if mutation == "metadata_null":
        body["metadata"] = None
    elif mutation == "request_id_absent":
        body["metadata"] = {"ordinary": "PRIVATE ORDINARY METADATA"}
    elif mutation == "request_id_null":
        body["metadata"] = {"request_id": None}
    elif mutation == "request_id_not_string":
        body["metadata"] = {"request_id": 17}
    elif mutation == "request_id_empty":
        submitted_request_id = ""
        body["metadata"] = {"request_id": submitted_request_id}
    elif mutation == "request_id_forbidden_characters":
        submitted_request_id = "PRIVATE REQUEST ID WITH SPACES"
        body["metadata"] = {"request_id": submitted_request_id}
    elif mutation == "request_id_overlong":
        submitted_request_id = "PRIVATE-" + ("x" * 121)
        body["metadata"] = {"request_id": submitted_request_id}
    elif mutation == "content_empty":
        submitted_request_id = "bounded-explanation-request"
        body["metadata"] = {"request_id": submitted_request_id}
        body["content"] = ""
    elif mutation != "metadata_omitted":
        raise AssertionError(mutation)

    with psycopg.connect(postgres_database) as conn:
        before_count = conn.execute("SELECT count(*) FROM messages").fetchone()[0]
        before_root_metadata = conn.execute(
            "SELECT metadata FROM messages WHERE id = %s",
            (root["message_id"],),
        ).fetchone()[0]
    response, qdrant = _append_through_api(
        monkeypatch,
        postgres_database,
        conversation_id=root["conversation_id"],
        body=body,
    )

    assert response.status_code == 422
    assert response.json() == {"detail": "history_root_lineage_invalid"}
    for sentinel in (
        submitted_request_id,
        str(root["message_id"]),
        json.dumps(lineage, sort_keys=True),
        "PRIVATE ORDINARY METADATA",
    ):
        if sentinel:
            assert sentinel not in response.text
    assert qdrant.upserts == []
    with psycopg.connect(postgres_database) as conn:
        after_count = conn.execute("SELECT count(*) FROM messages").fetchone()[0]
        after_root_metadata = conn.execute(
            "SELECT metadata FROM messages WHERE id = %s",
            (root["message_id"],),
        ).fetchone()[0]
    assert after_count == before_count
    assert after_root_metadata == before_root_metadata


@pytest.mark.parametrize(
    "metadata",
    [
        None,
        {},
        {"request_id": None},
        {"request_id": 17},
        {"request_id": ""},
        {"request_id": "PRIVATE REQUEST ID WITH SPACES"},
        {"request_id": "x" * 121},
    ],
)
def test_postgres_store_rejects_lineage_without_valid_explanation_request_id(
    postgres_database,
    metadata,
):
    root = _seed_history_root(postgres_database, record_kind="support")
    lineage = _lineage_payload(root)
    with psycopg.connect(postgres_database) as conn:
        before_count = conn.execute("SELECT count(*) FROM messages").fetchone()[0]

    async def append():
        store = PostgresStore(postgres_database)
        await store.open()
        try:
            with pytest.raises(HistoryRootLineageValidationError) as exc:
                await store.add_message(
                    conversation_id=root["conversation_id"],
                    owner_id=root["owner_id"],
                    role="assistant",
                    content="Direct storage explanation append.",
                    metadata=metadata,
                    history_root_lineage=lineage,
                )
            assert str(exc.value) == "history_root_lineage_invalid"
        finally:
            await store.close()

    asyncio.run(append())
    with psycopg.connect(postgres_database) as conn:
        after_count = conn.execute("SELECT count(*) FROM messages").fetchone()[0]
    assert after_count == before_count


@pytest.mark.parametrize(
    "root_kind,mutation,declared_kind",
    [
        ("support", "cross_owner", "support"),
        ("support", "cross_conversation", "support"),
        ("support", "recursive", "support"),
        ("support", "root_role", "support"),
        ("support", "no_direct_record", "support"),
        ("support", "wrong_kind", "acquisition"),
        ("acquisition", "wrong_kind", "support"),
        ("support", "invalid_support_association", "support"),
        ("acquisition", "invalid_acquisition_association", "acquisition"),
    ],
)
def test_lineage_append_revalidates_root_scope_kind_role_and_association(
    monkeypatch,
    postgres_database,
    root_kind,
    mutation,
    declared_kind,
):
    root = _seed_history_root(postgres_database, record_kind=root_kind)
    append_owner = root["owner_id"]
    append_conversation = root["conversation_id"]
    lineage_root_id = root["message_id"]
    if mutation == "cross_owner":
        append_owner = "PRIVATE CROSS OWNER"
        async def create_cross_owner_destination():
            store = PostgresStore(postgres_database)
            await store.open()
            try:
                return await store.create_conversation(
                    owner_id=append_owner,
                    client_id="telegram:cross-owner-destination",
                )
            finally:
                await store.close()
        append_conversation = asyncio.run(create_cross_owner_destination())
    elif mutation == "cross_conversation":
        async def create_other_conversation():
            store = PostgresStore(postgres_database)
            await store.open()
            try:
                return await store.create_conversation(
                    owner_id=root["owner_id"],
                    client_id="telegram:other-conversation",
                )
            finally:
                await store.close()
        append_conversation = asyncio.run(create_other_conversation())
    elif mutation == "recursive":
        recursive = _lineage_payload(root)
        with psycopg.connect(postgres_database) as conn:
            metadata = conn.execute(
                "SELECT metadata FROM messages WHERE id = %s",
                (root["message_id"],),
            ).fetchone()[0]
            metadata["history_root_lineage"] = recursive
            conn.execute(
                "UPDATE messages SET metadata = %s::jsonb WHERE id = %s",
                (json.dumps(metadata), root["message_id"]),
            )
            conn.commit()
    elif mutation == "root_role":
        with psycopg.connect(postgres_database) as conn:
            conn.execute(
                "UPDATE messages SET role = 'user' WHERE id = %s",
                (root["message_id"],),
            )
            conn.commit()
    elif mutation == "no_direct_record":
        async def create_recordless_root():
            store = PostgresStore(postgres_database)
            await store.open()
            try:
                return await store.add_message(
                    conversation_id=root["conversation_id"],
                    owner_id=root["owner_id"],
                    role="assistant",
                    content="Recordless direct root.",
                    metadata={"request_id": f"recordless-{uuid4().hex}"},
                )
            finally:
                await store.close()
        lineage_root_id = asyncio.run(create_recordless_root())
    elif mutation == "invalid_support_association":
        with psycopg.connect(postgres_database) as conn:
            conn.execute(
                "UPDATE claim_records SET claim_anchor_digest = %s WHERE assistant_message_id = %s",
                ("sha256:" + ("f" * 64), root["message_id"]),
            )
            conn.commit()
    elif mutation == "invalid_acquisition_association":
        with psycopg.connect(postgres_database) as conn:
            prompt = conn.execute(
                "SELECT prompt_json FROM traces WHERE request_id = %s",
                (root["request_id"],),
            ).fetchone()[0]
            prompt["evidence_acquisition"]["response_digest"] = "sha256:" + ("f" * 64)
            conn.execute(
                "UPDATE traces SET prompt_json = %s::jsonb WHERE request_id = %s",
                (json.dumps(prompt), root["request_id"]),
            )
            conn.commit()

    lineage = {
        "schema_version": "history-root-lineage.v1",
        "root_assistant_message_id": str(lineage_root_id),
        "record_kind": declared_kind,
    }
    with psycopg.connect(postgres_database) as conn:
        before_count = conn.execute("SELECT count(*) FROM messages").fetchone()[0]
        before_root_metadata = conn.execute(
            "SELECT metadata FROM messages WHERE id = %s",
            (root["message_id"],),
        ).fetchone()[0]
    response, qdrant = _append_through_api(
        monkeypatch,
        postgres_database,
        conversation_id=append_conversation,
        body={
            "owner_id": append_owner,
            "role": "assistant",
            "content": "Rejected root association.",
            "metadata": {"request_id": "rejected-root-association"},
            "history_root_lineage": lineage,
        },
    )

    assert response.status_code == 422
    assert response.json() == {"detail": "history_root_lineage_invalid"}
    assert str(lineage_root_id) not in response.text
    assert "PRIVATE CROSS OWNER" not in response.text
    assert json.dumps(lineage, sort_keys=True) not in response.text
    assert qdrant.upserts == []
    with psycopg.connect(postgres_database) as conn:
        after_count = conn.execute("SELECT count(*) FROM messages").fetchone()[0]
        after_root_metadata = conn.execute(
            "SELECT metadata FROM messages WHERE id = %s",
            (root["message_id"],),
        ).fetchone()[0]
    assert after_count == before_count
    assert after_root_metadata == before_root_metadata


def test_two_explanation_appends_store_same_original_root_not_parent(
    monkeypatch,
    postgres_database,
):
    root = _seed_history_root(postgres_database, record_kind="acquisition")
    lineage = _lineage_payload(root)
    message_ids = []
    for index in range(2):
        response, _ = _append_through_api(
            monkeypatch,
            postgres_database,
            conversation_id=root["conversation_id"],
            body={
                "owner_id": root["owner_id"],
                "role": "assistant",
                "content": f"Historical explanation {index}.",
                "metadata": {"request_id": f"explanation-{index}"},
                "history_root_lineage": lineage,
            },
        )
        assert response.status_code == 200, response.text
        message_ids.append(response.json()["message_id"])

    with psycopg.connect(postgres_database) as conn:
        rows = conn.execute(
            "SELECT id, metadata->'history_root_lineage' FROM messages WHERE id = ANY(%s)",
            (message_ids,),
        ).fetchall()
    assert len(rows) == 2
    assert all(row[1] == lineage for row in rows)
    assert all(row[1]["root_assistant_message_id"] == str(root["message_id"]) for row in rows)
    assert message_ids[0] not in {row[1]["root_assistant_message_id"] for row in rows}


def test_resolution_surface_mismatch_occurs_after_valid_append_and_ordinary_answer_terminates(
    monkeypatch,
    postgres_database,
):
    root = _seed_history_root(postgres_database, record_kind="acquisition")
    lineage = _lineage_payload(root)
    lineaged, _ = _append_through_api(
        monkeypatch,
        postgres_database,
        conversation_id=root["conversation_id"],
        body={
            "owner_id": root["owner_id"],
            "role": "assistant",
            "content": "Historical acquisition explanation.",
            "metadata": {"request_id": "lineaged-before-surface-check"},
            "history_root_lineage": lineage,
        },
    )
    assert lineaged.status_code == 200, lineaged.text

    mismatch = _resolve_v2_through_api(
        monkeypatch,
        postgres_database,
        root=root,
        explanation_kind="acquisition",
        surface="other-surface",
    )
    assert mismatch.status_code == 200, mismatch.text
    assert mismatch.json()["reason_code"] == "lineage_surface_mismatch"
    assert mismatch.json()["history_root_lineage"] is None
    assert str(root["message_id"]) not in mismatch.text

    ordinary, _ = _append_through_api(
        monkeypatch,
        postgres_database,
        conversation_id=root["conversation_id"],
        body={
            "owner_id": root["owner_id"],
            "role": "assistant",
            "content": "Unrelated ordinary assistant answer.",
            "metadata": {"request_id": "ordinary-chain-terminator"},
        },
    )
    assert ordinary.status_code == 200, ordinary.text
    terminated = _resolve_v2_through_api(
        monkeypatch,
        postgres_database,
        root=root,
        explanation_kind="acquisition",
    )
    assert terminated.status_code == 200, terminated.text
    assert terminated.json()["reason_code"] == "direct_record_absent_lineage_absent"
    assert terminated.json()["lineage_dereference_count"] == 0
