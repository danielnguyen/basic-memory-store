import os
import types
import uuid

from fastapi.testclient import TestClient

os.environ.setdefault("MEMORY_API_KEY", "testkey")
os.environ.setdefault("PG_DSN", "postgresql://user:pass@localhost:5432/test")
os.environ.setdefault("QDRANT_URL", "http://localhost:6333")
os.environ.setdefault("LITELLM_BASE_URL", "http://localhost:4000")

import main as main_module


class FakePG:
    def __init__(self):
        self.promote_calls = []
        self.reinforce_calls = []
        self.debug_payload = None

    async def open(self):
        return None

    async def close(self):
        return None

    async def promote_memory_item(self, **kwargs):
        self.promote_calls.append(kwargs)
        now = "2026-01-01T00:00:00+00:00"
        memory_id = str(uuid.uuid4())
        return {
            "memory": {
                "memory_id": memory_id,
                "owner_id": kwargs["owner_id"],
                "memory_type": kwargs["memory_type"],
                "summary": kwargs["summary"],
                "source_refs_json": kwargs["source_refs_json"],
                "source_ref_hash": kwargs["source_ref_hash"],
                "scores_json": kwargs["scores_json"],
                "promotion_state": "promoted",
                "status": "active",
                "supersedes_memory_id": str(kwargs["supersedes_memory_id"])
                if kwargs["supersedes_memory_id"]
                else None,
                "superseded_by_memory_id": None,
                "last_reinforced_at": now if kwargs["reinforce"] else None,
                "expires_at": kwargs["expires_at"],
                "derivation_version": "r20-mvp-v1",
                "confidence": kwargs["confidence"],
                "explanation_json": kwargs["explanation_json"],
                "generation_trace_id": kwargs["generation_trace_id"],
                "created_at": now,
                "updated_at": now,
            },
            "created": False,
            "updated": True,
            "reinforced": kwargs["reinforce"],
            "superseded": bool(kwargs["supersedes_memory_id"]),
            "events_appended": ["updated", "reinforced"] if kwargs["reinforce"] else ["updated"],
        }

    async def reinforce_memory_item(self, **kwargs):
        self.reinforce_calls.append(kwargs)
        now = "2026-01-01T00:00:00+00:00"
        return {
            "memory_id": str(kwargs["memory_id"]),
            "owner_id": kwargs["owner_id"],
            "memory_type": "core",
            "summary": "remember concise answers",
            "source_refs_json": [{"ref_type": "message", "ref_id": "m-1", "support_kind": "direct"}],
            "source_ref_hash": "hash",
            "scores_json": kwargs["scores_json"],
            "promotion_state": "promoted",
            "status": "active",
            "supersedes_memory_id": None,
            "superseded_by_memory_id": None,
            "last_reinforced_at": now,
            "expires_at": None,
            "derivation_version": "r20-mvp-v1",
            "confidence": None,
            "explanation_json": {},
            "generation_trace_id": None,
            "created_at": now,
            "updated_at": now,
        }

    async def get_memory_debug(self, memory_id):
        now = "2026-01-01T00:00:00+00:00"
        return {
            "memory": {
                "memory_id": str(memory_id),
                "owner_id": "owner",
                "memory_type": "core",
                "summary": "remember concise answers",
                "source_refs_json": [{"ref_type": "message", "ref_id": "m-1", "support_kind": "direct"}],
                "source_ref_hash": "hash",
                "scores_json": {"utility_score": 0.9},
                "promotion_state": "promoted",
                "status": "active",
                "supersedes_memory_id": None,
                "superseded_by_memory_id": None,
                "last_reinforced_at": None,
                "expires_at": None,
                "derivation_version": "r20-mvp-v1",
                "confidence": 0.8,
                "explanation_json": {"rationale": "explicit"},
                "generation_trace_id": "rid-1",
                "created_at": now,
                "updated_at": now,
            },
            "events": [
                {
                    "event_id": str(uuid.uuid4()),
                    "memory_id": str(memory_id),
                    "owner_id": "owner",
                    "event_type": "created",
                    "reason_json": {"request_id": "rid-1"},
                    "created_at": now,
                }
            ],
        }


class FakeQdrant:
    def ping(self):
        return True


def _settings():
    return types.SimpleNamespace(
        memory_api_key="testkey",
        require_request_id=True,
        enforce_request_id_header_body_match=True,
    )


def test_promote_normalizes_refs_and_returns_deterministic_audit_order(monkeypatch):
    pg = FakePG()
    monkeypatch.setattr(main_module, "settings", _settings(), raising=True)
    monkeypatch.setattr(main_module, "pg", pg, raising=True)
    monkeypatch.setattr(main_module, "qdrant", FakeQdrant(), raising=True)

    client = TestClient(main_module.app)
    try:
        body = {
            "request_id": "rid-1",
            "owner_id": "owner",
            "memory_type": "core",
            "summary": "remember concise answers",
            "source_refs": [
                {"ref_type": "message", "ref_id": "m-2"},
                {"ref_type": "message", "ref_id": "m-1"},
            ],
            "scores": {"utility_score": 0.9},
            "confidence": 0.8,
            "explanation": {"rationale": "explicit"},
            "generation_trace_id": "rid-source",
            "reinforce": True,
        }

        response = client.post(
            "/v1/internal/memory/promote",
            headers={"X-API-Key": "testkey", "X-Request-ID": "rid-1"},
            json=body,
        )

        assert response.status_code == 200
        out = response.json()
        assert out["events_appended"] == ["updated", "reinforced"]
        assert out["updated"] is True
        assert out["reinforced"] is True
        call = pg.promote_calls[0]
        assert [ref["ref_id"] for ref in call["source_refs_json"]] == ["m-1", "m-2"]
        assert len(call["source_ref_hash"]) == 64
    finally:
        client.close()


def test_reinforce_and_debug_endpoints(monkeypatch):
    pg = FakePG()
    monkeypatch.setattr(main_module, "settings", _settings(), raising=True)
    monkeypatch.setattr(main_module, "pg", pg, raising=True)
    monkeypatch.setattr(main_module, "qdrant", FakeQdrant(), raising=True)
    memory_id = str(uuid.uuid4())

    client = TestClient(main_module.app)
    try:
        reinforce = client.post(
            f"/v1/internal/memory/{memory_id}/reinforce",
            headers={"X-API-Key": "testkey", "X-Request-ID": "rid-2"},
            json={
                "request_id": "rid-2",
                "owner_id": "owner",
                "scores": {"recurrence_score": 0.4},
                "reason": {"source": "manual"},
            },
        )
        assert reinforce.status_code == 200
        assert reinforce.json()["last_reinforced_at"] is not None
        assert pg.reinforce_calls[0]["scores_json"] == {"recurrence_score": 0.4}

        debug = client.get(
            f"/v1/internal/memory/{memory_id}/debug",
            headers={"X-API-Key": "testkey"},
        )
        assert debug.status_code == 200
        body = debug.json()
        assert body["memory"]["memory_id"] == memory_id
        assert body["events"][0]["event_type"] == "created"
        assert body["events"][0]["reason"] == {"request_id": "rid-1"}
    finally:
        client.close()
