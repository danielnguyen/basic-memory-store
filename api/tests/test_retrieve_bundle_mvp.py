import types
import uuid

from fastapi.testclient import TestClient

import main as main_module


class FakePG:
    def __init__(self, *, message_times=None):
        self.message_times = message_times or ["2026-01-01T00:00:00+00:00"]
        self.last_conversation_id = None

    async def open(self):
        return None

    async def close(self):
        return None

    async def ping(self):
        return True

    async def conversation_exists(self, cid):
        return True

    async def get_conversation(self, cid):
        return {
            "conversation_id": str(cid),
            "owner_id": "owner",
            "client_id": "client-a",
            "title": None,
            "created_at": "2026-01-01T00:00:00+00:00",
            "updated_at": "2026-01-01T00:00:00+00:00",
        }

    async def get_message_snippets_by_ids(self, ids):
        out = []
        for idx, item in enumerate(ids):
            out.append(
                {
                    "message_id": str(item),
                    "conversation_id": self.last_conversation_id if idx == 0 and self.last_conversation_id else str(uuid.uuid4()),
                    "role": "assistant",
                    "content": f"semantic result {idx}",
                    "metadata": {},
                    "created_at": self.message_times[min(idx, len(self.message_times) - 1)],
                }
            )
        return out

    async def get_recent_message_items(self, conversation_id, limit):
        self.last_conversation_id = str(conversation_id)
        return [
            {
                "message_id": str(uuid.uuid4()),
                "conversation_id": str(conversation_id),
                "role": "user",
                "content": "recent snippet",
                "created_at": "2026-01-01T00:00:00+00:00",
            }
        ]

    async def get_derived_text_snippets_by_ids(self, ids):
        return [
            {
                "derived_text_id": str(item),
                "artifact_id": str(uuid.uuid4()),
                "text": "def important_helper(): pass",
                "derivation_params": {},
                "created_at": "2026-01-01T00:00:00+00:00",
                "file_path": "api/helpers.py",
                "repo_name": "basic-memory-store",
                "mime": "text/plain",
            }
            for item in ids
        ] if ids else []


class FakeQdrant:
    def __init__(self, *, message_scores=None):
        self.message_scores = message_scores or [0.77]

    def ping(self):
        return True

    async def search(self, **kwargs):
        return [
            types.SimpleNamespace(message_id=str(uuid.uuid4()), score=score)
            for score in self.message_scores
        ]

    async def search_artifact_chunks(self, **kwargs):
        return [
            types.SimpleNamespace(
                derived_text_id=str(uuid.uuid4()),
                artifact_id=str(uuid.uuid4()),
                file_path="api/helpers.py",
                repo_name="basic-memory-store",
                score=0.66,
            ),
            types.SimpleNamespace(
                derived_text_id=str(uuid.uuid4()),
                artifact_id=str(uuid.uuid4()),
                file_path="api/helpers.py",
                repo_name="basic-memory-store",
                score=0.61,
            ),
        ]


def test_retrieve_bundle_shape(monkeypatch):
    fake_pg = FakePG()
    fake_qdrant = FakeQdrant()
    fake_settings = types.SimpleNamespace(
        memory_api_key="testkey",
        require_request_id=True,
        enforce_request_id_header_body_match=True,
        retrieval_k=8,
        retrieval_recent_half_life_days=14,
        retrieval_balanced_half_life_days=45,
        retrieval_historical_half_life_days=365,
        retrieval_conversation_boost=0.08,
        retrieval_pinned_bias=0.12,
        retrieval_missing_penalty_cap=0.15,
        recent_turns=10,
    )
    monkeypatch.setattr(main_module, "settings", fake_settings, raising=True)
    monkeypatch.setattr(main_module, "pg", fake_pg, raising=True)
    monkeypatch.setattr(main_module, "qdrant", fake_qdrant, raising=True)

    client = TestClient(main_module.app)
    try:
        rid = "rid-1"
        conversation_id = str(uuid.uuid4())
        r = client.post(
            f"/v2/conversations/{conversation_id}/retrieve",
            headers={"X-API-Key": "testkey", "X-Request-ID": rid},
            json={"request_id": rid, "owner_id": "owner", "query": "hello"},
        )
        assert r.status_code == 200
        body = r.json()
        assert body["request_id"] == rid
        assert body["conversation_id"] == conversation_id
        assert body["bundle"]["recent"][0]["content"] == "recent snippet"
        assert body["bundle"]["semantic"][0]["content"] == "semantic result 0"
        assert body["bundle"]["semantic"][0]["score"] >= 0.77
        assert body["bundle"]["semantic"][0]["score_details"]["semantic_score"] == 0.77
        assert body["bundle"]["artifact_refs"][0]["file_path"] == "api/helpers.py"
        assert len(body["bundle"]["artifact_refs"]) == 1
        assert body["bundle"]["observed_metadata"] == {
            "mime_types": ["text/plain"],
            "has_artifacts": True,
            "has_code_like_content": True,
            "estimated_chars": len("recent snippetsemantic result 0def important_helper(): passdef important_helper(): pass"),
        }
        assert body["bundle"]["token_estimate_total"] == len("recent snippetsemantic result 0def important_helper(): passdef important_helper(): pass") // 4
        assert body["bundle"]["retrieval_debug"]["time_window"] == "all"
        assert body["bundle"]["retrieval_debug"]["retrieval_mode"] == "balanced"
        assert "pinned memories are not part of the v2 ranked bundle" in body["bundle"]["retrieval_debug"]["pinned_handling"]
    finally:
        client.close()


def test_retrieve_bundle_recent_mode_with_30d_window(monkeypatch):
    fake_pg = FakePG(
        message_times=[
            "2026-03-25T00:00:00+00:00",
            "2025-10-01T00:00:00+00:00",
        ]
    )
    fake_qdrant = FakeQdrant(message_scores=[0.77, 0.74])
    fake_settings = types.SimpleNamespace(
        memory_api_key="testkey",
        require_request_id=True,
        enforce_request_id_header_body_match=True,
        retrieval_k=8,
        retrieval_recent_half_life_days=14,
        retrieval_balanced_half_life_days=45,
        retrieval_historical_half_life_days=365,
        retrieval_conversation_boost=0.08,
        retrieval_pinned_bias=0.12,
        retrieval_missing_penalty_cap=0.15,
        recent_turns=10,
    )
    monkeypatch.setattr(main_module, "settings", fake_settings, raising=True)
    monkeypatch.setattr(main_module, "pg", fake_pg, raising=True)
    monkeypatch.setattr(main_module, "qdrant", fake_qdrant, raising=True)

    client = TestClient(main_module.app)
    try:
        rid = "rid-recent"
        conversation_id = str(uuid.uuid4())
        r = client.post(
            f"/v2/conversations/{conversation_id}/retrieve",
            headers={"X-API-Key": "testkey", "X-Request-ID": rid},
            json={
                "request_id": rid,
                "owner_id": "owner",
                "query": "hello",
                "retrieval": {"time_window": "30d", "retrieval_mode": "recent"},
            },
        )
        assert r.status_code == 200
        body = r.json()
        assert body["bundle"]["retrieval_debug"]["time_window"] == "30d"
        assert body["bundle"]["retrieval_debug"]["retrieval_mode"] == "recent"
        assert body["bundle"]["retrieval_debug"]["semantic_candidates"] == 2
        assert body["bundle"]["retrieval_debug"]["semantic_ranked"] == 1
        assert len(body["bundle"]["semantic"]) == 1
        assert body["bundle"]["semantic"][0]["content"] == "semantic result 0"
    finally:
        client.close()


def test_retrieve_bundle_historical_mode_with_older_content(monkeypatch):
    fake_pg = FakePG(
        message_times=[
            "2026-03-25T00:00:00+00:00",
            "2024-01-01T00:00:00+00:00",
        ]
    )
    fake_qdrant = FakeQdrant(message_scores=[0.77, 0.74])
    fake_settings = types.SimpleNamespace(
        memory_api_key="testkey",
        require_request_id=True,
        enforce_request_id_header_body_match=True,
        retrieval_k=8,
        retrieval_recent_half_life_days=14,
        retrieval_balanced_half_life_days=45,
        retrieval_historical_half_life_days=365,
        retrieval_conversation_boost=0.08,
        retrieval_pinned_bias=0.12,
        retrieval_missing_penalty_cap=0.15,
        recent_turns=10,
    )
    monkeypatch.setattr(main_module, "settings", fake_settings, raising=True)
    monkeypatch.setattr(main_module, "pg", fake_pg, raising=True)
    monkeypatch.setattr(main_module, "qdrant", fake_qdrant, raising=True)

    client = TestClient(main_module.app)
    try:
        rid = "rid-historical"
        conversation_id = str(uuid.uuid4())
        r = client.post(
            f"/v2/conversations/{conversation_id}/retrieve",
            headers={"X-API-Key": "testkey", "X-Request-ID": rid},
            json={
                "request_id": rid,
                "owner_id": "owner",
                "query": "hello",
                "retrieval": {"time_window": "all", "retrieval_mode": "historical"},
            },
        )
        assert r.status_code == 200
        body = r.json()
        assert body["bundle"]["retrieval_debug"]["retrieval_mode"] == "historical"
        assert len(body["bundle"]["semantic"]) == 2
        assert body["bundle"]["semantic"][1]["created_at"] == "2024-01-01T00:00:00+00:00"
        assert body["bundle"]["semantic"][1]["score_details"]["semantic_score"] == 0.74
    finally:
        client.close()
