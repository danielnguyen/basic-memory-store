import uuid
import types
import pytest
from fastapi.testclient import TestClient

import main as main_module


# -------------------------
# Fakes
# -------------------------

class FakePG:
    def __init__(self):
        self.conversations = set()
        self.messages = []  # list of dicts

    async def open(self): ...
    async def close(self): ...
    async def ping(self): return True

    async def create_conversation(self, owner_id: str, client_id: str, title=None):
        cid = uuid.uuid4()
        self.conversations.add(cid)
        return cid

    async def conversation_exists(self, cid):
        return cid in self.conversations

    async def resolve_conversation(self, owner_id: str, client_id: str, idle_ttl_s: int, title=None):
        # Always create new for test determinism
        cid = await self.create_conversation(owner_id, client_id, title)
        return cid, False

    async def add_message(self, conversation_id, owner_id, role, content, client_id, metadata=None):
        mid = uuid.uuid4()
        self.messages.append(
            {
                "message_id": str(mid),
                "conversation_id": str(conversation_id),
                "owner_id": owner_id,
                "role": role,
                "content": content,
                "client_id": client_id,
                "created_at": "2026-01-01 00:00:00+00:00",
            }
        )
        return mid

    async def get_recent_messages(self, conversation_id, limit: int):
        # Return minimal structure your prompt assembler expects
        # (Your assemble_messages uses model_dump() from body.messages + recent_messages.)
        out = []
        for m in self.messages[-limit:]:
            if m["conversation_id"] == str(conversation_id):
                out.append({"role": m["role"], "content": m["content"]})
        return out

    async def get_message_snippets_by_ids(self, ids):
        idset = {str(i) for i in ids}
        out = []
        for m in self.messages:
            if m["message_id"] in idset:
                out.append(
                    {
                        "message_id": m["message_id"],
                        "conversation_id": m["conversation_id"],
                        "role": m["role"],
                        "content": m["content"],
                        "created_at": m["created_at"],
                    }
                )
        return out

    async def list_conversations(self, owner_id, client_id=None, limit=20, cursor=None):
        # keep it simple
        return ([], None)


class FakeQdrant:
    def __init__(self):
        self.upserts = []  # record calls

    def ping(self): return True

    async def upsert_message_vector(self, **kwargs):
        # just record; don't error
        self.upserts.append(kwargs)
        return True

    async def search(self, owner_id, query, k, min_score, conversation_id=None, client_id=None, exclude_message_ids=None):
        # Return empty by default (tests can monkeypatch this per-case)
        return []


class FakeLiteLLM:
    def __init__(self):
        self.calls = []

    async def chat(self, model, messages, temperature=None, max_tokens=None, request_id=None):
        self.calls.append(
            {
                "model": model,
                "messages": messages,
                "temperature": temperature,
                "max_tokens": max_tokens,
                "request_id": request_id,
            }
        )
        return "pong"

    async def embeddings(self, model, texts):
        return [[0.0] * 8 for _ in texts]


# -------------------------
# Fixture: patch main.py singletons
# -------------------------

@pytest.fixture()
def client(monkeypatch):
    fake_settings = types.SimpleNamespace(
        memory_api_key="testkey",
        pg_dsn="",
        qdrant_url="",
        qdrant_collection="messages",
        litellm_base_url="http://litellm:4000",
        litellm_api_key=None,
        embed_model="embed",
        chat_model="chat_local_fast",
        chat_temperature=None,
        retrieval_k=5,
        recent_turns=10,
        max_context_chars=4000,
        index_user_questions=False,
        index_assistant_messages=True,
        min_index_chars=12,
    )

    fake_pg = FakePG()
    fake_qdrant = FakeQdrant()
    fake_litellm = FakeLiteLLM()

    # Patch module globals
    monkeypatch.setattr(main_module, "settings", fake_settings, raising=True)
    monkeypatch.setattr(main_module, "pg", fake_pg, raising=True)
    monkeypatch.setattr(main_module, "qdrant", fake_qdrant, raising=True)
    monkeypatch.setattr(main_module, "litellm", fake_litellm, raising=True)

    # TestClient will call startup/shutdown handlers
    with TestClient(main_module.app) as c:
        yield c


def auth_headers():
    return {"X-API-Key": "testkey"}


# -------------------------
# Tests
# -------------------------

def test_healthz_is_public(client):
    r = client.get("/healthz")
    assert r.status_code == 200
    assert r.json()["ok"] is True


def test_readyz_is_public(client):
    r = client.get("/readyz")
    assert r.status_code == 200
    assert r.json()["ok"] is True


def test_v1_chat_requires_auth(client):
    r = client.post("/v1/chat", json={"owner_id": "daniel", "client_id": "smoke", "messages": [{"role": "user", "content": "ping"}]})
    assert r.status_code == 401


def test_v1_chat_happy_path(client):
    r = client.post(
        "/v1/chat",
        headers=auth_headers(),
        json={
            "owner_id": "daniel",
            "client_id": "smoke",
            "messages": [{"role": "user", "content": "ping"}],
        },
    )
    assert r.status_code == 200
    body = r.json()
    assert body["answer"] == "pong"
    assert "conversation_id" in body
    assert isinstance(body["retrieved_count"], int)


def test_v1_retrieve_passes_exclude_ids(client, monkeypatch):
    # Arrange: make qdrant return one fake hit and ensure exclude ids are accepted
    hit_id = str(uuid.uuid4())

    class Hit:
        def __init__(self, message_id, score):
            self.message_id = message_id
            self.score = score

    async def fake_search(**kwargs):
        assert kwargs.get("exclude_message_ids") == ["a", "b"]
        return [Hit(message_id=hit_id, score=0.9)]

    monkeypatch.setattr(main_module.qdrant, "search", fake_search, raising=True)

    # Also stub pg snippet lookup
    async def fake_snips(ids):
        return [{
            "message_id": hit_id,
            "conversation_id": str(uuid.uuid4()),
            "role": "user",
            "content": "Remember that my favorite snack is pretzels.",
            "created_at": "2026-01-01 00:00:00+00:00",
        }]

    monkeypatch.setattr(main_module.pg, "get_message_snippets_by_ids", fake_snips, raising=True)

    r = client.post(
        "/v1/retrieve",
        headers=auth_headers(),
        json={
            "owner_id": "daniel",
            "query": "favorite snack",
            "k": 5,
            "min_score": 0.2,
            "exclude_message_ids": ["a", "b"],
        },
    )
    assert r.status_code == 200
    hits = r.json()["hits"]
    assert len(hits) == 1
    assert hits[0]["message_id"] == hit_id