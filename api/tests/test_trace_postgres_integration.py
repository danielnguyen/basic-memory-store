from __future__ import annotations

import asyncio
import hashlib
import types
from uuid import uuid4

from fastapi.testclient import TestClient
import httpx
import psycopg

import main as main_module
from services.claim_records import validate_claim_record_association
from storage.postgres import PostgresStore
from storage.qdrant import _policy_payload


class FakeQdrant:
    def ping(self):
        return True


def _settings():
    return types.SimpleNamespace(
        memory_api_key="testkey",
        require_request_id=True,
        enforce_request_id_header_body_match=True,
        enable_trace_storage=True,
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
            cross_owner_message_id = await store.add_message(
                conversation_id=conversation_id,
                owner_id="other-owner",
                role="assistant",
                content="cross owner working must not consume limit",
                client_id="client-working",
                policy_metadata=policy,
            )
        finally:
            await store.close()
        return conversation_id, owner_message_id, cross_owner_message_id

    conversation_id, owner_message_id, cross_owner_message_id = asyncio.run(run())
    with psycopg.connect(postgres_database) as conn:
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
            await store.add_message(
                conversation_id=conversation_id,
                owner_id="other-owner",
                role="assistant",
                content="Cross-owner assistant response.",
                client_id="client-history",
                metadata={"request_id": "history-request-cross-owner"},
            )
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
