from __future__ import annotations

import asyncio
import types
from uuid import uuid4

from fastapi.testclient import TestClient
import psycopg

import main as main_module
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
