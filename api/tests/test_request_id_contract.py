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

    async def get_message_snippets_by_ids(self, ids):
        return [
            {
                "message_id": str(ids[0]) if ids else str(uuid.uuid4()),
                "conversation_id": str(uuid.uuid4()),
                "role": "user",
                "content": "example",
                "created_at": "2026-01-01T00:00:00+00:00",
            }
        ] if ids else []

    async def get_recent_message_items(self, conversation_id, limit):
        return [
            {
                "message_id": str(uuid.uuid4()),
                "conversation_id": str(conversation_id),
                "role": "user",
                "content": "recent",
                "created_at": "2026-01-01T00:00:00+00:00",
            }
        ]

    async def create_trace(self, trace):
        return uuid.uuid4()

    async def get_trace_by_request_id(self, request_id):
        return None

    async def resolve_profile(self, **kwargs):
        return {
            "profile_name": "dev",
            "source": "global_default",
            "profile_version": 1,
            "effective_profile_ref": "owner:dev:1",
            "prompt_overlay": "",
            "retrieval_policy": {},
            "routing_policy": {},
            "response_style": {},
            "safety_policy": {},
            "tool_policy": {},
        }


class FakeQdrant:
    def ping(self):
        return True

    async def search(self, **kwargs):
        hit = types.SimpleNamespace(message_id=str(uuid.uuid4()), score=0.8)
        return [hit]


def _headers():
    return {"X-API-Key": "testkey"}


def _settings():
    return types.SimpleNamespace(
        memory_api_key="testkey",
        require_request_id=True,
        enforce_request_id_header_body_match=True,
        enable_trace_storage=True,
        enable_profile_resolve=True,
        default_profile_name="dev",
        retrieval_k=8,
        retrieval_recent_half_life_days=14,
        retrieval_balanced_half_life_days=45,
        retrieval_historical_half_life_days=365,
        retrieval_conversation_boost=0.08,
        retrieval_pinned_bias=0.12,
        retrieval_missing_penalty_cap=0.15,
        enable_hygiene_scan_api=True,
        enable_graph_retrieval_expansion=False,
        recent_turns=10,
        qdrant_url="",
        qdrant_collection="messages",
        pg_dsn="",
        litellm_base_url="",
        litellm_api_key=None,
        embed_model="embed",
        chat_model="chat",
        chat_temperature=None,
        min_index_chars=1,
        index_assistant_messages=False,
        index_user_questions=False,
        max_context_chars=1000,
        object_store_endpoint="",
        object_store_bucket="",
        object_store_access_key="",
        object_store_secret_key="",
        object_store_region="",
        object_store_presign_base_url=None,
        object_store_include_content_type_in_put_signature=False,
    )


def test_retrieve_requires_matching_request_id(monkeypatch):
    monkeypatch.setattr(main_module, "settings", _settings(), raising=True)
    monkeypatch.setattr(main_module, "pg", FakePG(), raising=True)
    monkeypatch.setattr(main_module, "qdrant", FakeQdrant(), raising=True)

    client = TestClient(main_module.app)
    try:
        conversation_id = str(uuid.uuid4())
        body = {"request_id": "body-rid", "owner_id": "owner", "query": "hello"}

        r = client.post(
            f"/v2/conversations/{conversation_id}/retrieve",
            headers={**_headers(), "X-Request-ID": "header-rid"},
            json=body,
        )
        assert r.status_code == 400
    finally:
        client.close()


def test_traces_requires_matching_request_id(monkeypatch):
    monkeypatch.setattr(main_module, "settings", _settings(), raising=True)
    monkeypatch.setattr(main_module, "pg", FakePG(), raising=True)
    monkeypatch.setattr(main_module, "qdrant", FakeQdrant(), raising=True)

    client = TestClient(main_module.app)
    try:
        body = {
            "request_id": "body-rid",
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
            headers={**_headers(), "X-Request-ID": "header-rid"},
            json=body,
        )
        assert r.status_code == 400
    finally:
        client.close()
