import types
import uuid

from fastapi.testclient import TestClient

import main as main_module


class FakePG:
    async def open(self):
        return None

    async def close(self):
        return None

    async def ping(self):
        return True

    async def create_trace(self, trace):
        return uuid.uuid4()

    async def get_trace_by_request_id(self, request_id):
        return {
            "trace_id": str(uuid.uuid4()),
            "request_id": request_id,
            "conversation_id": str(uuid.uuid4()),
            "owner_id": "owner",
            "client_id": "vscode",
            "surface": "vscode",
            "profile": {},
            "retrieval": {},
            "router_decision": {},
            "manual_override": {},
            "model_call": {},
            "fallback": {},
            "cost": {},
            "latency_ms": 1,
            "status": "ok",
            "error": None,
            "created_at": "2026-01-01T00:00:00+00:00",
        }


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


def test_trace_create_and_get(monkeypatch):
    monkeypatch.setattr(main_module, "settings", _settings(), raising=True)
    monkeypatch.setattr(main_module, "pg", FakePG(), raising=True)
    monkeypatch.setattr(main_module, "qdrant", FakeQdrant(), raising=True)

    client = TestClient(main_module.app)
    try:
        rid = "rid-123"
        create_body = {
            "request_id": rid,
            "conversation_id": str(uuid.uuid4()),
            "owner_id": "owner",
            "surface": "vscode",
            "profile": {},
            "retrieval": {},
            "router_decision": {},
            "manual_override": {},
            "model_call": {},
            "fallback": {},
            "cost": {},
            "status": "ok",
        }
        r = client.post(
            "/v1/traces",
            headers={"X-API-Key": "testkey", "X-Request-ID": rid},
            json=create_body,
        )
        assert r.status_code == 200
        assert r.json()["request_id"] == rid

        g = client.get(f"/v1/traces/{rid}", headers={"X-API-Key": "testkey"})
        assert g.status_code == 200
        assert g.json()["request_id"] == rid
    finally:
        client.close()
