import uuid
import types
from pathlib import Path
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
        self.artifacts = {}
        self.traces = {}

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

    async def create_artifact(
        self,
        artifact_id,
        owner_id: str,
        filename: str,
        mime: str,
        size: int,
        object_uri: str,
        client_id=None,
        conversation_id=None,
        source_surface=None,
    ):
        row = {
            "artifact_id": str(artifact_id),
            "owner_id": owner_id,
            "client_id": client_id,
            "conversation_id": str(conversation_id) if conversation_id else None,
            "filename": filename,
            "mime": mime,
            "size": size,
            "object_uri": object_uri,
            "source_surface": source_surface,
            "status": "pending",
            "sha256": None,
            "created_at": "2026-01-01 00:00:00+00:00",
            "completed_at": None,
        }
        self.artifacts[str(artifact_id)] = row
        return row

    async def complete_artifact(self, artifact_id, status="completed", sha256=None):
        row = self.artifacts.get(str(artifact_id))
        if row is None:
            return None
        row["status"] = status
        row["sha256"] = sha256 or row["sha256"]
        if status == "completed":
            row["completed_at"] = "2026-01-01 00:00:10+00:00"
        return row

    async def get_artifact(self, artifact_id):
        return self.artifacts.get(str(artifact_id))

    async def get_recent_message_snippets(self, conversation_id, limit=10):
        out = []
        for m in self.messages:
            if m["conversation_id"] == str(conversation_id):
                out.append(m)
        return out[-limit:]

    async def get_pinned_memories(self, owner_id: str, conversation_id=None, limit=5):
        return []

    async def get_policy_overlays(self, owner_id: str, surface=None):
        return []

    async def get_persona_overlays(self, owner_id: str, surface=None):
        return []

    async def write_trace(
        self,
        request_id: str,
        conversation_id,
        owner_id,
        surface,
        router_decision,
        retrieval,
        model_calls,
        cost,
        latency_ms,
    ):
        trace = {
            "request_id": request_id,
            "trace_id": str(uuid.uuid4()),
            "conversation_id": str(conversation_id) if conversation_id else None,
            "owner_id": owner_id,
            "surface": surface,
            "router_decision": router_decision or {},
            "retrieval": retrieval or {},
            "model_calls": model_calls or {},
            "cost": cost or {},
            "latency_ms": latency_ms,
            "created_at": "2026-01-01 00:00:00+00:00",
        }
        self.traces[request_id] = trace
        return trace["trace_id"]

    async def get_trace_by_request_id(self, request_id: str):
        return self.traces.get(request_id)


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
        artifacts_object_prefix="artifacts",
        artifacts_upload_base_url="http://localhost:9000",
        artifacts_presign_ttl_s=900,
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

    # Avoid context-manager lifespan startup hang in this dependency set.
    c = TestClient(main_module.app)
    try:
        yield c
    finally:
        c.close()


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
    bad_id = "not-a-uuid"

    class Hit:
        def __init__(self, message_id, score):
            self.message_id = message_id
            self.score = score

    async def fake_search(**kwargs):
        assert kwargs.get("exclude_message_ids") == ["a", "b"]
        return [Hit(message_id=bad_id, score=0.95), Hit(message_id=hit_id, score=0.9)]

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


def test_artifact_init_complete_and_get(client):
    r1 = client.post(
        "/v1/artifacts/init",
        headers=auth_headers(),
        json={
            "owner_id": "daniel",
            "client_id": "vscode",
            "filename": "notes.pdf",
            "mime": "application/pdf",
            "size": 1234,
            "source_surface": "vscode",
        },
    )
    assert r1.status_code == 200
    init_body = r1.json()
    assert init_body["status"] == "pending"
    aid = init_body["artifact_id"]

    r2 = client.post(
        "/v1/artifacts/complete",
        headers=auth_headers(),
        json={
            "artifact_id": aid,
            "sha256": "abc123",
            "status": "completed",
        },
    )
    assert r2.status_code == 200
    complete_body = r2.json()
    assert complete_body["artifact_id"] == aid
    assert complete_body["status"] == "completed"

    r3 = client.get(f"/v1/artifacts/{aid}", headers=auth_headers())
    assert r3.status_code == 200
    get_body = r3.json()
    assert get_body["artifact_id"] == aid
    assert get_body["sha256"] == "abc123"
    assert get_body["object_uri"].endswith("/notes.pdf")


def test_artifact_key_sanitization_helper():
    assert main_module._sanitize_object_key_component("  weird /\\\\  name?.pdf  ") == "weird ___ name_.pdf"
    assert main_module._sanitize_object_key_component("   ") == "artifact"


def test_tiered_retrieve_endpoint(client, monkeypatch):
    convo = str(uuid.uuid4())
    main_module.pg.conversations.add(uuid.UUID(convo))
    msg_id = str(uuid.uuid4())
    bad_id = "still-not-uuid"

    class Hit:
        def __init__(self, message_id, score):
            self.message_id = message_id
            self.score = score

    async def fake_search(**kwargs):
        return [Hit(message_id=bad_id, score=0.99), Hit(message_id=msg_id, score=0.88)]

    async def fake_snips(ids):
        return [{
            "message_id": msg_id,
            "conversation_id": convo,
            "role": "user",
            "content": "Pinned note",
            "created_at": "2026-01-01 00:00:00+00:00",
        }]

    monkeypatch.setattr(main_module.qdrant, "search", fake_search, raising=True)
    monkeypatch.setattr(main_module.pg, "get_message_snippets_by_ids", fake_snips, raising=True)

    r = client.post(
        f"/v1/conversations/{convo}/retrieve",
        headers=auth_headers(),
        json={
            "owner_id": "daniel",
            "query": "note",
            "k": 4,
        },
    )
    assert r.status_code == 200
    body = r.json()
    assert body["conversation_id"] == convo
    assert len(body["semantic"]) == 1
    assert "working" in body
    assert "pinned" in body
    assert "policy" in body
    assert "persona" in body


def test_orchestrate_chat_and_trace_read(client):
    r = client.post(
        "/v1/orchestrate/chat",
        headers=auth_headers(),
        json={
            "owner_id": "daniel",
            "client_id": "vscode",
            "surface": "vscode",
            "artifact_ids": [str(uuid.uuid4())],
            "messages": [{"role": "user", "content": "ping"}],
        },
    )
    assert r.status_code == 200
    body = r.json()
    assert body["answer"] == "pong"
    request_id = body["request_id"]
    assert request_id

    r2 = client.get(f"/v1/traces/{request_id}", headers=auth_headers())
    assert r2.status_code == 200
    trace = r2.json()
    assert trace["request_id"] == request_id
    assert trace["surface"] == "vscode"


def test_metrics_exposes_skipped_qdrant_counter(client, monkeypatch):
    convo = str(uuid.uuid4())
    main_module.pg.conversations.add(uuid.UUID(convo))
    valid_id = str(uuid.uuid4())

    class Hit:
        def __init__(self, message_id, score):
            self.message_id = message_id
            self.score = score

    async def fake_search(**kwargs):
        return [Hit(message_id="bad-id", score=0.99), Hit(message_id=valid_id, score=0.8)]

    async def fake_snips(ids):
        return [{
            "message_id": valid_id,
            "conversation_id": convo,
            "role": "user",
            "content": "hello",
            "created_at": "2026-01-01 00:00:00+00:00",
        }]

    monkeypatch.setattr(main_module.qdrant, "search", fake_search, raising=True)
    monkeypatch.setattr(main_module.pg, "get_message_snippets_by_ids", fake_snips, raising=True)

    r = client.post(
        f"/v1/conversations/{convo}/retrieve",
        headers=auth_headers(),
        json={"owner_id": "daniel", "query": "hello"},
    )
    assert r.status_code == 200

    m = client.get("/metrics")
    assert m.status_code == 200
    assert 'memory_skipped_qdrant_ids_total{kind="semantic"}' in m.text


def test_pinned_memories_migration_mentions_set_null_fk():
    migration_path = Path(__file__).resolve().parents[2] / "db" / "migrations" / "20260214_pinned_memories_nullable.sql"
    sql = migration_path.read_text()
    assert "ALTER COLUMN conversation_id DROP NOT NULL" in sql
    assert "ON DELETE SET NULL" in sql
