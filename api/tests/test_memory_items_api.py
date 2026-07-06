import os
import types
import uuid

from fastapi.testclient import TestClient

os.environ.setdefault("MEMORY_API_KEY", "testkey")
os.environ.setdefault("PG_DSN", "postgresql://user:pass@localhost:5432/test")
os.environ.setdefault("QDRANT_URL", "http://localhost:6333")
os.environ.setdefault("LITELLM_BASE_URL", "http://localhost:4000")

import main as main_module
from services.derivation_versions import MEMORY_ITEM_DERIVATION_VERSION


class FakePG:
    def __init__(self):
        self.promote_calls = []
        self.reinforce_calls = []
        self.transition_calls = []
        self.decay_calls = []
        self.decision_calls = []
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
                "derivation_version": kwargs.get("derivation_version", MEMORY_ITEM_DERIVATION_VERSION),
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
        scores = {**kwargs["scores_json"]}
        scores.setdefault("salience_score", 0.6)
        return {
            "memory_id": str(kwargs["memory_id"]),
            "owner_id": kwargs["owner_id"],
            "memory_type": "core",
            "summary": "remember concise answers",
            "source_refs_json": [{"ref_type": "message", "ref_id": "m-1", "support_kind": "direct"}],
            "source_ref_hash": "hash",
            "scores_json": scores,
            "promotion_state": "promoted",
            "status": "active",
            "supersedes_memory_id": None,
            "superseded_by_memory_id": None,
            "last_reinforced_at": now,
            "expires_at": None,
            "derivation_version": MEMORY_ITEM_DERIVATION_VERSION,
            "confidence": None,
            "explanation_json": {},
            "generation_trace_id": None,
            "created_at": now,
            "updated_at": now,
        }

    async def decay_memory_item(self, **kwargs):
        self.decay_calls.append(kwargs)
        now = "2026-01-01T00:00:02+00:00"
        return {
            "memory_id": str(kwargs["memory_id"]),
            "owner_id": kwargs["owner_id"],
            "memory_type": "core",
            "summary": "remember concise answers",
            "source_refs_json": [{"ref_type": "message", "ref_id": "m-1", "support_kind": "direct"}],
            "source_ref_hash": "hash",
            "scores_json": {"salience_score": 0.3, "last_decay_factor": kwargs["decay_factor"]},
            "promotion_state": "decayed" if kwargs["demote"] else "promoted",
            "status": "forgotten_or_demoted" if kwargs["demote"] else "stale",
            "supersedes_memory_id": None,
            "superseded_by_memory_id": None,
            "last_reinforced_at": None,
            "expires_at": None,
            "derivation_version": MEMORY_ITEM_DERIVATION_VERSION,
            "confidence": None,
            "explanation_json": {},
            "generation_trace_id": None,
            "created_at": now,
            "updated_at": now,
        }

    async def record_memory_decision(self, **kwargs):
        self.decision_calls.append(kwargs)
        now = "2026-01-01T00:00:03+00:00"
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
                "promotion_state": kwargs["promotion_state"],
                "status": kwargs["status"],
                "supersedes_memory_id": None,
                "superseded_by_memory_id": None,
                "last_reinforced_at": None,
                "expires_at": None,
                "derivation_version": MEMORY_ITEM_DERIVATION_VERSION,
                "confidence": None,
                "explanation_json": kwargs["explanation_json"],
                "generation_trace_id": None,
                "created_at": now,
                "updated_at": now,
            },
            "events": [
                {
                    "event_id": str(uuid.uuid4()),
                    "memory_id": memory_id,
                    "owner_id": kwargs["owner_id"],
                    "event_type": kwargs["event_type"],
                    "reason_json": {**kwargs["reason_json"], "request_id": kwargs["request_id"]},
                    "created_at": now,
                }
            ],
        }

    async def transition_memory_item(self, **kwargs):
        self.transition_calls.append(kwargs)
        now = "2026-01-01T00:00:01+00:00"
        return {
            "memory": {
                "memory_id": str(kwargs["memory_id"]),
                "owner_id": kwargs["owner_id"],
                "memory_type": "core",
                "summary": "remember concise answers",
                "source_refs_json": [{"ref_type": "message", "ref_id": "m-1", "support_kind": "direct"}],
                "source_ref_hash": "hash",
                "scores_json": {},
                "promotion_state": "promoted",
                "status": kwargs["new_status"],
                "supersedes_memory_id": str(kwargs["related_memory_id"])
                if kwargs["new_status"] == "corrected" and kwargs["related_memory_id"]
                else None,
                "superseded_by_memory_id": None,
                "last_reinforced_at": None,
                "expires_at": None,
                "derivation_version": MEMORY_ITEM_DERIVATION_VERSION,
                "confidence": None,
                "explanation_json": {},
                "generation_trace_id": None,
                "created_at": now,
                "updated_at": now,
            },
            "changed": True,
            "events_appended": ["state_changed"],
        }

    async def get_memory_debug(self, memory_id, owner_id):
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
                "derivation_version": MEMORY_ITEM_DERIVATION_VERSION,
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
        assert out["memory"]["derivation_version"] == MEMORY_ITEM_DERIVATION_VERSION
        call = pg.promote_calls[0]
        assert "derivation_version" not in call
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
        assert reinforce.json()["scores"]["salience_score"] == 0.6
        assert pg.reinforce_calls[0]["scores_json"] == {"recurrence_score": 0.4}

        debug = client.get(
            f"/v1/internal/memory/{memory_id}/debug?owner_id=owner",
            headers={"X-API-Key": "testkey"},
        )
        assert debug.status_code == 200
        body = debug.json()
        assert body["memory"]["memory_id"] == memory_id
        assert body["events"][0]["event_type"] == "created"
        assert body["events"][0]["reason"] == {"request_id": "rid-1"}
    finally:
        client.close()


def test_evaluate_suppression_persists_debuggable_decision_record(monkeypatch):
    pg = FakePG()
    monkeypatch.setattr(main_module, "settings", _settings(), raising=True)
    monkeypatch.setattr(main_module, "pg", pg, raising=True)
    monkeypatch.setattr(main_module, "qdrant", FakeQdrant(), raising=True)

    client = TestClient(main_module.app)
    try:
        response = client.post(
            "/v1/internal/memory/evaluate",
            headers={"X-API-Key": "testkey", "X-Request-ID": "rid-evaluate"},
            json={
                "request_id": "rid-evaluate",
                "owner_id": "owner",
                "persist_decision": True,
                "candidate": {"summary": "unsupported trivia", "unsupported": True},
            },
        )

        assert response.status_code == 200
        body = response.json()
        assert body["decision"] == "suppress"
        assert "unsupported" in body["suppression_reasons"]
        assert body["decision_record"]["memory"]["promotion_state"] == "suppressed"
        assert body["decision_record"]["memory"]["freshness_state"] == "forgotten_or_demoted"
        assert body["decision_record"]["events"][0]["event_type"] == "suppressed"
        assert pg.decision_calls[0]["source_refs_json"][0]["support_kind"] == "decision_record"
    finally:
        client.close()


def test_decay_endpoint_lowers_salience_without_changing_source_refs(monkeypatch):
    pg = FakePG()
    monkeypatch.setattr(main_module, "settings", _settings(), raising=True)
    monkeypatch.setattr(main_module, "pg", pg, raising=True)
    monkeypatch.setattr(main_module, "qdrant", FakeQdrant(), raising=True)
    memory_id = str(uuid.uuid4())

    client = TestClient(main_module.app)
    try:
        response = client.post(
            f"/v1/internal/memory/{memory_id}/decay",
            headers={"X-API-Key": "testkey", "X-Request-ID": "rid-decay"},
            json={
                "request_id": "rid-decay",
                "owner_id": "owner",
                "decay_factor": 0.5,
                "demote": True,
                "reason": {"code": "stale_implementation_detail", "metadata": {"age_days": 120}},
            },
        )

        assert response.status_code == 200
        body = response.json()
        assert body["events_appended"] == ["decayed"]
        assert body["memory"]["scores"]["salience_score"] == 0.3
        assert body["memory"]["source_refs"] == [{"ref_type": "message", "ref_id": "m-1", "support_kind": "direct"}]
        assert body["memory"]["freshness_state"] == "forgotten_or_demoted"
        assert pg.decay_calls[0]["reason_json"]["reason_code"] == "stale_implementation_detail"
    finally:
        client.close()


def test_transition_endpoint_preserves_durable_and_effective_state(monkeypatch):
    pg = FakePG()
    monkeypatch.setattr(main_module, "settings", _settings(), raising=True)
    monkeypatch.setattr(main_module, "pg", pg, raising=True)
    monkeypatch.setattr(main_module, "qdrant", FakeQdrant(), raising=True)
    memory_id = str(uuid.uuid4())

    client = TestClient(main_module.app)
    try:
        response = client.post(
            f"/v1/internal/memory/{memory_id}/transition",
            headers={"X-API-Key": "testkey", "X-Request-ID": "rid-transition"},
            json={
                "request_id": "rid-transition",
                "owner_id": "owner",
                "status": "invalidated",
                "reason": {"code": "support_withdrawn", "metadata": {"source": "review"}},
            },
        )
        assert response.status_code == 200
        body = response.json()
        assert body["memory"]["status"] == "invalidated"
        assert body["memory"]["freshness_state"] == "forgotten_or_demoted"
        assert body["events_appended"] == ["state_changed"]
        assert pg.transition_calls[0]["reason_code"] == "support_withdrawn"
    finally:
        client.close()


def test_transition_rejects_unsupported_status_before_storage(monkeypatch):
    pg = FakePG()
    monkeypatch.setattr(main_module, "settings", _settings(), raising=True)
    monkeypatch.setattr(main_module, "pg", pg, raising=True)
    monkeypatch.setattr(main_module, "qdrant", FakeQdrant(), raising=True)

    client = TestClient(main_module.app)
    try:
        response = client.post(
            f"/v1/internal/memory/{uuid.uuid4()}/transition",
            headers={"X-API-Key": "testkey", "X-Request-ID": "rid-transition"},
            json={
                "request_id": "rid-transition",
                "owner_id": "owner",
                "status": "current",
                "reason": {"code": "unsupported"},
            },
        )
        assert response.status_code == 422
        assert pg.transition_calls == []
    finally:
        client.close()
