import os
import types
import uuid

from fastapi.testclient import TestClient

os.environ.setdefault("MEMORY_API_KEY", "testkey")
os.environ.setdefault("PG_DSN", "postgresql://user:pass@localhost:5432/test")
os.environ.setdefault("QDRANT_URL", "http://localhost:6333")
os.environ.setdefault("LITELLM_BASE_URL", "http://localhost:4000")

import main as main_module
from services.derivation_versions import EPISODE_DERIVATION_VERSION


class FakePG:
    def __init__(self):
        self.episode_calls = []
        self.link_calls = []

    async def open(self):
        return None

    async def close(self):
        return None

    async def create_or_update_episode(self, **kwargs):
        self.episode_calls.append(kwargs)
        now = "2026-01-01T00:00:00+00:00"
        return {
            "episode": {
                "episode_id": str(uuid.uuid4()),
                "owner_id": kwargs["owner_id"],
                "title": kwargs["title"],
                "summary": kwargs["summary"],
                "episode_type": kwargs["episode_type"],
                "trigger_json": kwargs["trigger_json"],
                "outcome": kwargs["outcome"],
                "significance": kwargs["significance"],
                "unresolved_json": kwargs["unresolved_json"],
                "source_refs_json": kwargs["source_refs_json"],
                "source_ref_hash": kwargs["source_ref_hash"],
                "episode_key": kwargs["episode_key"],
                "callback_candidates_json": kwargs["callback_candidates_json"],
                "time_window_json": kwargs["time_window_json"],
                "participants_json": kwargs["participants_json"],
                "status": "active",
                "derivation_version": kwargs["derivation_version"],
                "confidence": kwargs["confidence"],
                "explanation_json": kwargs["explanation_json"],
                "generation_trace_id": kwargs["generation_trace_id"],
                "created_at": now,
                "updated_at": now,
            },
            "created": False,
            "updated": True,
        }

    async def create_episode_links(self, **kwargs):
        self.link_calls.append(kwargs)
        now = "2026-01-01T00:00:00+00:00"
        links = []
        for link in kwargs["links"]:
            links.append(
                {
                    "link_id": str(uuid.uuid4()),
                    "episode_id": str(kwargs["episode_id"]),
                    "owner_id": kwargs["owner_id"],
                    "ref_type": link["ref_type"],
                    "ref_id": link["ref_id"],
                    "relationship": link["relationship"],
                    "created_at": now,
                }
            )
        return {
            "episode_id": str(kwargs["episode_id"]),
            "created_count": 1,
            "existing_count": max(0, len(kwargs["links"]) - 1),
            "links": links,
        }

    async def get_episode_debug(self, episode_id, owner_id):
        now = "2026-01-01T00:00:00+00:00"
        return {
            "episode": {
                "episode_id": str(episode_id),
                "owner_id": "owner",
                "title": "Memory promotion completed",
                "summary": "Manual capture of the memory-promotion milestone.",
                "episode_type": "milestone",
                "trigger_json": {"kind": "manual"},
                "outcome": "completed",
                "significance": "moves memory work into episode planning",
                "unresolved_json": {},
                "source_refs_json": [{"ref_type": "memory_item", "ref_id": "mem-1", "support_kind": "direct"}],
                "source_ref_hash": "hash",
                "episode_key": "episode-key",
                "callback_candidates_json": [],
                "time_window_json": {"start": "2026-01-01"},
                "participants_json": ["operator"],
                "status": "active",
                "derivation_version": EPISODE_DERIVATION_VERSION,
                "confidence": 0.8,
                "explanation_json": {"rationale": "manual incident capture"},
                "generation_trace_id": "rid-1",
                "created_at": now,
                "updated_at": now,
            },
            "links": [
                {
                    "link_id": str(uuid.uuid4()),
                    "episode_id": str(episode_id),
                    "owner_id": "owner",
                    "ref_type": "memory_item",
                    "ref_id": "mem-1",
                    "relationship": "supports",
                    "created_at": now,
                }
            ],
            "events": [
                {
                    "event_id": str(uuid.uuid4()),
                    "episode_id": str(episode_id),
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


def test_create_episode_normalizes_refs_and_returns_update_shape(monkeypatch):
    pg = FakePG()
    monkeypatch.setattr(main_module, "settings", _settings(), raising=True)
    monkeypatch.setattr(main_module, "pg", pg, raising=True)
    monkeypatch.setattr(main_module, "qdrant", FakeQdrant(), raising=True)

    client = TestClient(main_module.app)
    try:
        body = {
            "request_id": "rid-1",
            "owner_id": "owner",
            "title": "Memory promotion completed",
            "summary": "Manual capture of the memory-promotion milestone.",
            "episode_type": "milestone",
            "source_refs": [
                {"ref_type": "memory_item", "ref_id": "mem-2"},
                {"ref_type": "memory_item", "ref_id": "mem-1"},
            ],
            "trigger": {"kind": "manual"},
            "time_window": {"end": "2026-01-02", "start": "2026-01-01"},
            "participants": ["operator"],
            "confidence": 0.8,
            "explanation": {"rationale": "manual incident capture"},
            "generation_trace_id": "rid-source",
        }
        response = client.post(
            "/v1/internal/episodes",
            headers={"X-API-Key": "testkey", "X-Request-ID": "rid-1"},
            json=body,
        )

        assert response.status_code == 200
        out = response.json()
        assert out["created"] is False
        assert out["updated"] is True
        assert out["episode"]["derivation_version"] == EPISODE_DERIVATION_VERSION
        call = pg.episode_calls[0]
        assert call["derivation_version"] == EPISODE_DERIVATION_VERSION
        assert [ref["ref_id"] for ref in call["source_refs_json"]] == ["mem-1", "mem-2"]
        assert len(call["source_ref_hash"]) == 64
        assert len(call["episode_key"]) == 64
    finally:
        client.close()


def test_episode_links_and_debug_endpoints(monkeypatch):
    pg = FakePG()
    monkeypatch.setattr(main_module, "settings", _settings(), raising=True)
    monkeypatch.setattr(main_module, "pg", pg, raising=True)
    monkeypatch.setattr(main_module, "qdrant", FakeQdrant(), raising=True)
    episode_id = str(uuid.uuid4())

    client = TestClient(main_module.app)
    try:
        link_response = client.post(
            f"/v1/internal/episodes/{episode_id}/links",
            headers={"X-API-Key": "testkey", "X-Request-ID": "rid-2"},
            json={
                "request_id": "rid-2",
                "owner_id": "owner",
                "links": [
                    {"ref_type": "memory_item", "ref_id": "mem-1", "relationship": "supports"},
                    {"ref_type": "message", "ref_id": "msg-1", "relationship": "documents"},
                ],
            },
        )
        assert link_response.status_code == 200
        assert link_response.json()["created_count"] == 1
        assert link_response.json()["existing_count"] == 1
        assert pg.link_calls[0]["links"][0]["ref_type"] == "memory_item"

        debug = client.get(
            f"/v1/internal/episodes/{episode_id}/debug?owner_id=owner",
            headers={"X-API-Key": "testkey"},
        )
        assert debug.status_code == 200
        body = debug.json()
        assert body["episode"]["episode_id"] == episode_id
        assert body["links"][0]["relationship"] == "supports"
        assert body["events"][0]["event_type"] == "created"
        assert body["events"][0]["reason"] == {"request_id": "rid-1"}
    finally:
        client.close()
