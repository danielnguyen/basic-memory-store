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
        return [
            {
                "message_id": str(ids[0]),
                "conversation_id": str(uuid.uuid4()),
                "role": "assistant",
                "content": "semantic result",
                "created_at": "2026-01-01T00:00:00+00:00",
            }
        ] if ids else []

    async def get_recent_message_items(self, conversation_id, limit):
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
                "file_path": "api/helpers.py",
                "repo_name": "basic-memory-store",
            }
            for item in ids
        ] if ids else []


class FakeQdrant:
    def ping(self):
        return True

    async def search(self, **kwargs):
        hit = types.SimpleNamespace(message_id=str(uuid.uuid4()), score=0.77)
        return [hit]

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
    fake_settings = types.SimpleNamespace(
        memory_api_key="testkey",
        require_request_id=True,
        enforce_request_id_header_body_match=True,
        retrieval_k=8,
        recent_turns=10,
    )
    monkeypatch.setattr(main_module, "settings", fake_settings, raising=True)
    monkeypatch.setattr(main_module, "pg", FakePG(), raising=True)
    monkeypatch.setattr(main_module, "qdrant", FakeQdrant(), raising=True)

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
        assert body["bundle"]["semantic"][0]["content"] == "semantic result"
        assert body["bundle"]["semantic"][0]["score"] == 0.77
        assert body["bundle"]["artifact_refs"][0]["file_path"] == "api/helpers.py"
        assert len(body["bundle"]["artifact_refs"]) == 1
        assert body["bundle"]["observed_metadata"] == {
            "mime_types": ["text/plain"],
            "has_artifacts": True,
            "has_code_like_content": True,
            "estimated_chars": len("recent snippetsemantic resultdef important_helper(): passdef important_helper(): pass"),
        }
        assert body["bundle"]["token_estimate_total"] == len("recent snippetsemantic resultdef important_helper(): passdef important_helper(): pass") // 4
    finally:
        client.close()
