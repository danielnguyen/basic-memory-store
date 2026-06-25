from __future__ import annotations

import types
from uuid import uuid4

from fastapi.testclient import TestClient
import psycopg

import main as main_module
from storage.postgres import PostgresStore


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
