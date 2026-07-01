import uuid
import types
from pathlib import Path
import anyio
import pytest
from fastapi.testclient import TestClient

import main as main_module
from models import OrchestrateChatRequest
from services.chunking import chunk_text


# -------------------------
# Fakes
# -------------------------

class FakePG:
    def __init__(self):
        self.conversations = set()
        self.messages = []  # list of dicts
        self.artifacts = {}
        self.traces = {}
        self.derived_text = {}
        self.embedding_refs = []
        self.fail_activate_attempt = False

    async def open(self): ...
    async def close(self): ...
    async def ping(self): return True

    async def create_conversation(self, owner_id: str, client_id: str, title=None):
        cid = uuid.uuid4()
        self.conversations.add(cid)
        return cid

    async def conversation_exists(self, cid):
        return cid in self.conversations

    async def get_conversation(self, cid):
        if cid not in self.conversations:
            return None
        return {
            "conversation_id": str(cid),
            "owner_id": "daniel",
            "client_id": "smoke",
            "title": None,
            "created_at": "2026-01-01 00:00:00+00:00",
            "updated_at": "2026-01-01 00:00:00+00:00",
        }

    async def resolve_conversation(self, owner_id: str, client_id: str, idle_ttl_s: int, title=None):
        # Always create new for test determinism
        cid = await self.create_conversation(owner_id, client_id, title)
        return cid, False

    async def add_message(self, conversation_id, owner_id, role, content, client_id, metadata=None, policy_metadata=None):
        mid = uuid.uuid4()
        self.messages.append(
            {
                "message_id": str(mid),
                "conversation_id": str(conversation_id),
                "owner_id": owner_id,
                "role": role,
                "content": content,
                "client_id": client_id,
                "metadata": metadata or {},
                "policy_metadata": policy_metadata,
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
                        "metadata": m.get("metadata", {}),
                        "policy_metadata": m.get("policy_metadata"),
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
        source_kind=None,
        repo_name=None,
        repo_ref=None,
        file_path=None,
        ingestion_id=None,
        sha256=None,
        status="pending",
        policy_metadata=None,
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
            "status": status,
            "sha256": sha256,
            "created_at": "2026-01-01 00:00:00+00:00",
            "completed_at": "2026-01-01 00:00:10+00:00" if status == "completed" else None,
            "source_kind": source_kind,
            "repo_name": repo_name,
            "repo_ref": repo_ref,
            "file_path": file_path,
            "ingestion_id": str(ingestion_id) if ingestion_id else None,
            "policy_metadata": policy_metadata,
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

    async def get_recent_message_items(self, conversation_id, limit=10, policy_filter=None):
        return await self.get_recent_message_snippets(conversation_id, limit=limit)

    async def create_derived_text(self, *, artifact_id, kind, text, language, derivation_params):
        did = uuid.uuid4()
        row = {
            "derived_text_id": str(did),
            "artifact_id": str(artifact_id),
            "owner_id": self.artifacts[str(artifact_id)]["owner_id"],
            "kind": kind,
            "language": language,
            "text": text,
            "derivation_params": derivation_params or {},
            "created_at": "2026-01-01 00:00:00+00:00",
        }
        self.derived_text[str(did)] = row
        return row

    async def create_embedding_ref(self, *, ref_type, ref_id, model, qdrant_point_id):
        row = {
            "embedding_id": str(uuid.uuid4()),
            "ref_type": ref_type,
            "ref_id": str(ref_id),
            "model": model,
            "qdrant_point_id": str(qdrant_point_id),
        }
        self.embedding_refs.append(row)
        return row

    async def get_active_derived_text_for_artifact(self, *, artifact_id, derivation_version):
        return [
            row
            for row in self.derived_text.values()
            if row["artifact_id"] == str(artifact_id)
            and row.get("derivation_params", {}).get("derivation_version") == derivation_version
            and row.get("derivation_params", {}).get("status", "active") == "active"
        ]

    async def get_derived_text_for_artifact_version(self, *, artifact_id, derivation_version):
        return [
            row
            for row in self.derived_text.values()
            if row["artifact_id"] == str(artifact_id)
            and row.get("derivation_params", {}).get("derivation_version") == derivation_version
        ]

    async def update_derived_text_params(self, *, derived_text_id, owner_id, derivation_params):
        row = self.derived_text.get(str(derived_text_id))
        if row is None or row.get("owner_id") != owner_id:
            return None
        row["derivation_params"] = derivation_params or {}
        return row

    async def get_derived_text_snippets_by_ids(self, ids):
        out = []
        for i in ids:
            row = self.derived_text.get(str(i))
            if not row:
                continue
            if row.get("derivation_params", {}).get("status", "active") != "active":
                continue
            params = row.get("derivation_params", {})
            artifact = self.artifacts[row["artifact_id"]]
            if artifact.get("status") != "completed":
                continue
            out.append(
                {
                    **row,
                    "created_at": row.get("created_at", "2026-01-01 00:00:00+00:00"),
                    "file_path": params.get("file_path") or artifact.get("file_path") or artifact.get("filename") or "",
                    "repo_name": params.get("repo_name") or artifact.get("repo_name"),
                    "mime": artifact.get("mime", "text/plain"),
                    "policy_metadata": artifact.get("policy_metadata"),
                }
            )
        return out

    async def activate_derived_text_attempt(
        self,
        *,
        artifact_id,
        owner_id,
        derivation_version,
        attempt_id,
        expected_chunk_count,
    ):
        if self.fail_activate_attempt:
            raise RuntimeError("injected activation failure")
        rows = [
            row
            for row in self.derived_text.values()
            if row["artifact_id"] == str(artifact_id)
            and row["owner_id"] == owner_id
            and row["derivation_params"].get("derivation_version") == derivation_version
            and row["derivation_params"].get("attempt_id") == attempt_id
            and row["derivation_params"].get("status") == "building"
            and row["derivation_params"].get("indexing_status") == "indexed"
        ]
        if len(rows) != expected_chunk_count:
            raise RuntimeError("derived text attempt is incomplete")
        for row in rows:
            row["derivation_params"] = {
                **row["derivation_params"],
                "status": "active",
                "activated_at": "2026-01-01 00:00:11+00:00",
            }
        return rows

    async def get_memory_items_for_source_refs(self, owner_id, source_refs):
        return {}

    async def get_pinned_memories(self, owner_id: str, conversation_id=None, limit=5, policy_filter=None):
        return []

    async def get_pinned_memories_for_hygiene(self, owner_id: str, limit=50):
        return []

    async def get_policy_overlays(self, owner_id: str, surface=None, policy_filter=None):
        return []

    async def get_persona_overlays(self, owner_id: str, surface=None, policy_filter=None):
        return []

    async def create_hygiene_flag(self, *, owner_id: str, subject_type: str, subject_id, flag_type: str, details=None):
        return {
            "flag_id": str(uuid.uuid4()),
            "owner_id": owner_id,
            "subject_type": subject_type,
            "subject_id": str(subject_id) if subject_id else None,
            "flag_type": flag_type,
            "details": details or {},
            "status": "open",
            "created_at": "2026-01-01 00:00:00+00:00",
            "resolved_at": None,
        }

    async def list_hygiene_flags(self, *, owner_id: str, status=None, limit=50):
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

    async def create_trace(self, trace):
        request_id = trace["request_id"]
        out = {
            "trace_id": str(uuid.uuid4()),
            "request_id": request_id,
            "conversation_id": str(trace["conversation_id"]),
            "owner_id": trace["owner_id"],
            "client_id": trace.get("client_id"),
            "surface": trace["surface"],
            "profile": trace.get("profile", {}),
            "retrieval": trace.get("retrieval", {}),
            "router_decision": trace.get("router_decision", {}),
            "manual_override": trace.get("manual_override", {}),
            "model_call": trace.get("model_call", {}),
            "fallback": trace.get("fallback", {}),
            "cost": trace.get("cost", {}),
            "latency_ms": trace.get("latency_ms"),
            "status": trace.get("status", "ok"),
            "error": trace.get("error"),
            "created_at": "2026-01-01 00:00:00+00:00",
        }
        self.traces[request_id] = out
        return uuid.UUID(out["trace_id"])

    async def get_trace_by_request_id(self, request_id: str):
        return self.traces.get(request_id)


class FakeQdrant:
    def __init__(self):
        self.upserts = []  # record calls
        self.derived_upserts = []
        self.derived_points = {}
        self.fail_after_derived_upserts: int | None = None
        self.fail_on_active_publication_at: int | None = None
        self.active_publication_attempts = 0

    def ping(self): return True

    async def upsert_message_vector(self, **kwargs):
        # just record; don't error
        self.upserts.append(kwargs)
        return True

    async def search(
        self,
        owner_id,
        query,
        k,
        min_score,
        conversation_id=None,
        client_id=None,
        exclude_message_ids=None,
        policy_filter=None,
    ):
        # Return empty by default (tests can monkeypatch this per-case)
        return []

    async def upsert_derived_text_vector(self, **kwargs):
        if self.fail_after_derived_upserts is not None and len(self.derived_upserts) >= self.fail_after_derived_upserts:
            raise RuntimeError("injected qdrant failure")
        if kwargs.get("derivation_status", "active") == "active":
            if (
                self.fail_on_active_publication_at is not None
                and self.active_publication_attempts >= self.fail_on_active_publication_at
            ):
                raise RuntimeError("injected active qdrant failure")
            self.active_publication_attempts += 1
        self.derived_upserts.append(kwargs)
        point_id = str(kwargs.get("qdrant_point_id") or kwargs["derived_text_id"])
        self.derived_points[point_id] = kwargs
        return True

    async def mark_derived_text_vector_inactive(self, **kwargs):
        point_id = str(kwargs["qdrant_point_id"])
        if point_id in self.derived_points:
            self.derived_points[point_id] = {
                **self.derived_points[point_id],
                "derivation_status": kwargs.get("derivation_status", "inactive"),
            }
        return True

    async def search_artifact_chunks(self, **kwargs):
        owner_id = kwargs["owner_id"]
        conversation_id = kwargs.get("conversation_id")
        hits = []
        for point in self.derived_points.values():
            if point.get("derivation_status") != "active":
                continue
            if point.get("owner_id") != owner_id:
                continue
            if conversation_id is not None and point.get("conversation_id") not in {None, str(conversation_id)}:
                continue
            hits.append(
                types.SimpleNamespace(
                    derived_text_id=str(point["derived_text_id"]),
                    artifact_id=str(point["artifact_id"]),
                    file_path=point.get("file_path") or "",
                    repo_name=point.get("repo_name"),
                    score=0.9,
                )
            )
        return hits[: kwargs.get("k", len(hits))]


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
        require_request_id=True,
        enforce_request_id_header_body_match=True,
        enable_trace_storage=True,
        retrieval_k=5,
        retrieval_artifact_k=3,
        retrieval_artifact_max_snippet_chars=500,
        retrieval_recent_half_life_days=14,
        retrieval_balanced_half_life_days=45,
        retrieval_historical_half_life_days=365,
        retrieval_conversation_boost=0.08,
        retrieval_pinned_bias=0.12,
        retrieval_missing_penalty_cap=0.15,
        enable_hygiene_scan_api=True,
        enable_graph_retrieval_expansion=False,
        recent_turns=10,
        max_context_chars=4000,
        artifacts_object_prefix="artifacts",
        artifacts_upload_base_url="http://localhost:9000",
        artifacts_presign_ttl_s=900,
        object_store_enabled=False,
        artifacts_max_size_bytes=104857600,
        artifacts_allowed_mime="image/png,image/jpeg,image/webp,application/pdf,text/plain,text/markdown,application/json,application/zip",
        index_user_questions=False,
        index_assistant_messages=True,
        min_index_chars=12,
        ingest_max_file_bytes=262144,
        artifact_text_derivation_max_bytes=262144,
        ingest_max_files_per_request=200,
        ingest_allowed_extensions=".py,.md,.txt,.json",
        ingest_exclude_globs_default=".git/*,node_modules/*",
        ingest_chunk_size_chars=1200,
        ingest_chunk_overlap_chars=150,
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


def _system_prompt_text() -> str:
    return "\n".join(item["content"] for item in main_module.litellm.calls[-1]["messages"] if item["role"] == "system")


def _init_text_artifact(client, *, content: bytes, owner_id: str = "daniel", filename: str = "notes.txt") -> str:
    r = client.post(
        "/v1/artifacts/init",
        headers=auth_headers(),
        json={
            "owner_id": owner_id,
            "client_id": "vscode",
            "filename": filename,
            "mime": "text/plain",
            "size": len(content),
            "source_surface": "vscode",
        },
    )
    assert r.status_code == 200
    return r.json()["artifact_id"]


def _active_rows_for_artifact(artifact_id: str) -> list[dict]:
    return [
        row for row in main_module.pg.derived_text.values()
        if row["artifact_id"] == artifact_id and row["derivation_params"].get("status") == "active"
    ]


def _active_qdrant_points_for_artifact(artifact_id: str) -> list[dict]:
    return [
        point for point in main_module.qdrant.derived_points.values()
        if str(point["artifact_id"]) == artifact_id and point.get("derivation_status") == "active"
    ]


def _assert_active_derivation_complete(artifact_id: str, expected_chunks: list[dict]) -> None:
    active_rows = _active_rows_for_artifact(artifact_id)
    assert len(active_rows) == len(expected_chunks)
    assert sorted(row["derivation_params"]["chunk_index"] for row in active_rows) == list(range(len(expected_chunks)))
    active_point_ids = {
        str(point.get("qdrant_point_id") or point["derived_text_id"])
        for point in _active_qdrant_points_for_artifact(artifact_id)
    }
    assert active_point_ids == {
        row["derivation_params"]["qdrant_point_id"]
        for row in active_rows
    }
    assert len(active_point_ids) == len(expected_chunks)


def _retrieve_artifacts(client, *, conversation_id, request_id: str, query: str = "alpha"):
    async def fake_recent(conversation_id, limit=10):
        return []

    main_module.pg.get_recent_message_items = fake_recent
    return client.post(
        f"/v2/conversations/{conversation_id}/retrieve",
        headers={**auth_headers(), "X-Request-ID": request_id},
        json={
            "request_id": request_id,
            "owner_id": "daniel",
            "query": query,
            "include_artifacts": True,
            "retrieval": {"k": 3, "min_score": 0.0, "scope": "conversation"},
        },
    )


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


def test_v1_chat_trace_profile_remains_unchanged(client):
    rid = "rid-v1-chat"
    r = client.post(
        "/v1/chat",
        headers={**auth_headers(), "X-Request-ID": rid},
        json={
            "owner_id": "daniel",
            "client_id": "smoke",
            "messages": [{"role": "user", "content": "ping"}],
        },
    )
    assert r.status_code == 200

    prompt_text = _system_prompt_text()
    assert "Surface behavior guidance:" not in prompt_text

    trace = client.get(f"/v1/traces/{rid}", headers=auth_headers()).json()
    assert trace["surface"] == "chat"
    assert trace["profile"] == {}


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


def test_artifact_flow_with_object_store_enabled(client, monkeypatch):
    class FakeObjectStore:
        def create_presigned_put_url(self, key: str, content_type: str, expires_s: int) -> str:
            return f"http://minio.local/upload/{key}"

        def create_presigned_get_url(self, key: str, expires_s: int) -> str:
            return f"http://minio.local/download/{key}"

        def head_object(self, key: str):
            return types.SimpleNamespace(size=1234, content_type="application/pdf")

        def read_object_bytes(self, key: str, *, max_bytes: int) -> bytes:
            raise AssertionError("non-text artifacts should not be read for derivation")

    monkeypatch.setattr(main_module.settings, "object_store_enabled", True, raising=False)
    monkeypatch.setattr(main_module, "object_store", FakeObjectStore(), raising=True)

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
    assert init_body["upload_url"].startswith("http://minio.local/upload/")
    aid = init_body["artifact_id"]

    r2 = client.post(
        "/v1/artifacts/complete",
        headers=auth_headers(),
        json={"artifact_id": aid, "status": "completed"},
    )
    assert r2.status_code == 200
    assert r2.json()["download_url"].startswith("http://minio.local/download/")


def test_text_artifact_completion_derives_same_artifact_and_is_idempotent(client, monkeypatch):
    content = b"uploaded artifact alpha beta gamma\n" * 3

    class FakeObjectStore:
        def create_presigned_put_url(self, key: str, content_type: str, expires_s: int) -> str:
            return f"http://client-minio/upload/{key}"

        def create_presigned_get_url(self, key: str, expires_s: int) -> str:
            return f"http://client-minio/download/{key}"

        def head_object(self, key: str):
            return types.SimpleNamespace(size=len(content), content_type="text/plain")

        def read_object_bytes(self, key: str, *, max_bytes: int) -> bytes:
            assert max_bytes == main_module.settings.artifact_text_derivation_max_bytes
            return content

    monkeypatch.setattr(main_module.settings, "object_store_enabled", True, raising=False)
    monkeypatch.setattr(main_module, "object_store", FakeObjectStore(), raising=True)

    r1 = client.post(
        "/v1/artifacts/init",
        headers=auth_headers(),
        json={
            "owner_id": "daniel",
            "client_id": "vscode",
            "filename": "notes.txt",
            "mime": "text/plain",
            "size": len(content),
            "source_surface": "vscode",
        },
    )
    assert r1.status_code == 200
    aid = r1.json()["artifact_id"]

    r2 = client.post(
        "/v1/artifacts/complete",
        headers=auth_headers(),
        json={"artifact_id": aid, "owner_id": "daniel", "status": "completed"},
    )
    assert r2.status_code == 200
    assert len(main_module.pg.derived_text) == 1
    derived = next(iter(main_module.pg.derived_text.values()))
    assert derived["artifact_id"] == aid
    assert derived["derivation_params"]["source_refs"] == [
        {"ref_type": "artifact", "ref_id": aid, "support_kind": "direct"}
    ]
    assert main_module.qdrant.derived_upserts[0]["artifact_id"] == uuid.UUID(aid)

    r3 = client.post(
        "/v1/artifacts/complete",
        headers=auth_headers(),
        json={"artifact_id": aid, "owner_id": "daniel", "status": "completed"},
    )
    assert r3.status_code == 200
    assert len(main_module.pg.derived_text) == 1


def test_text_artifact_retry_repairs_qdrant_failure_after_row_insert(client, monkeypatch):
    content = b"retry repair alpha beta gamma"

    class FakeObjectStore:
        def create_presigned_put_url(self, key: str, content_type: str, expires_s: int) -> str:
            return f"http://client-minio/upload/{key}"

        def create_presigned_get_url(self, key: str, expires_s: int) -> str:
            return f"http://client-minio/download/{key}"

        def head_object(self, key: str):
            return types.SimpleNamespace(size=len(content), content_type="text/plain")

        def read_object_bytes(self, key: str, *, max_bytes: int) -> bytes:
            return content

    monkeypatch.setattr(main_module.settings, "object_store_enabled", True, raising=False)
    monkeypatch.setattr(main_module, "object_store", FakeObjectStore(), raising=True)
    aid = _init_text_artifact(client, content=content)

    main_module.qdrant.fail_after_derived_upserts = 0
    failed = client.post(
        "/v1/artifacts/complete",
        headers=auth_headers(),
        json={"artifact_id": aid, "owner_id": "daniel", "status": "completed"},
    )
    assert failed.status_code == 503
    assert main_module.pg.artifacts[aid]["status"] == "pending"
    assert [row["derivation_params"]["status"] for row in main_module.pg.derived_text.values()] == ["building"]
    assert not [
        row for row in main_module.pg.derived_text.values()
        if row["derivation_params"].get("status") == "active"
    ]
    assert all(point.get("derivation_status") != "active" for point in main_module.qdrant.derived_points.values())

    main_module.qdrant.fail_after_derived_upserts = None
    repaired = client.post(
        "/v1/artifacts/complete",
        headers=auth_headers(),
        json={"artifact_id": aid, "owner_id": "daniel", "status": "completed"},
    )
    assert repaired.status_code == 200
    rows = list(main_module.pg.derived_text.values())
    assert [row["derivation_params"]["status"] for row in rows].count("active") == 1
    assert [row["derivation_params"]["status"] for row in rows].count("failed") == 1
    active = [row for row in rows if row["derivation_params"]["status"] == "active"][0]
    assert active["derivation_params"]["indexing_status"] == "indexed"
    assert main_module.pg.artifacts[aid]["status"] == "completed"


def test_text_artifact_active_publication_failure_first_write_retries_and_retrieves(client, monkeypatch):
    content = b"active publish first failure alpha beta gamma"
    expected_chunks = chunk_text(content.decode("utf-8"), chunk_size=1200, chunk_overlap=150)

    class FakeObjectStore:
        def create_presigned_put_url(self, key: str, content_type: str, expires_s: int) -> str:
            return f"http://client-minio/upload/{key}"

        def create_presigned_get_url(self, key: str, expires_s: int) -> str:
            return f"http://client-minio/download/{key}"

        def head_object(self, key: str):
            return types.SimpleNamespace(size=len(content), content_type="text/plain")

        def read_object_bytes(self, key: str, *, max_bytes: int) -> bytes:
            return content

    monkeypatch.setattr(main_module.settings, "object_store_enabled", True, raising=False)
    monkeypatch.setattr(main_module, "object_store", FakeObjectStore(), raising=True)
    convo = uuid.uuid4()
    main_module.pg.conversations.add(convo)
    aid = _init_text_artifact(client, content=content)

    main_module.qdrant.fail_on_active_publication_at = 0
    failed = client.post(
        "/v1/artifacts/complete",
        headers=auth_headers(),
        json={"artifact_id": aid, "owner_id": "daniel", "status": "completed"},
    )
    assert failed.status_code == 503
    assert failed.json()["detail"] == "artifact text derivation failed"
    assert main_module.pg.artifacts[aid]["status"] == "pending"
    assert len([item for item in main_module.qdrant.derived_upserts if item.get("derivation_status") == "building"]) == len(expected_chunks)
    assert len(main_module.pg.embedding_refs) == len(expected_chunks)
    assert _active_rows_for_artifact(aid) == []
    assert _active_qdrant_points_for_artifact(aid) == []

    before_retry = _retrieve_artifacts(client, conversation_id=convo, request_id="rid-active-first-failed")
    assert before_retry.status_code == 200
    assert before_retry.json()["bundle"]["artifact_refs"] == []

    main_module.qdrant.fail_on_active_publication_at = None
    repaired = client.post(
        "/v1/artifacts/complete",
        headers=auth_headers(),
        json={"artifact_id": aid, "owner_id": "daniel", "status": "completed"},
    )
    assert repaired.status_code == 200
    _assert_active_derivation_complete(aid, expected_chunks)

    after_retry = _retrieve_artifacts(client, conversation_id=convo, request_id="rid-active-first-repaired")
    assert after_retry.status_code == 200
    refs = after_retry.json()["bundle"]["artifact_refs"]
    assert refs and refs[0]["artifact_id"] == aid


def test_text_artifact_active_publication_failure_mid_attempt_retries_without_duplicates(client, monkeypatch):
    content = b"alpha beta gamma delta epsilon zeta eta theta iota kappa lambda mu"
    expected_chunks = chunk_text(content.decode("utf-8"), chunk_size=20, chunk_overlap=0)

    class FakeObjectStore:
        def create_presigned_put_url(self, key: str, content_type: str, expires_s: int) -> str:
            return f"http://client-minio/upload/{key}"

        def create_presigned_get_url(self, key: str, expires_s: int) -> str:
            return f"http://client-minio/download/{key}"

        def head_object(self, key: str):
            return types.SimpleNamespace(size=len(content), content_type="text/plain")

        def read_object_bytes(self, key: str, *, max_bytes: int) -> bytes:
            return content

    monkeypatch.setattr(main_module.settings, "object_store_enabled", True, raising=False)
    monkeypatch.setattr(main_module.settings, "ingest_chunk_size_chars", 20, raising=False)
    monkeypatch.setattr(main_module.settings, "ingest_chunk_overlap_chars", 0, raising=False)
    monkeypatch.setattr(main_module, "object_store", FakeObjectStore(), raising=True)
    convo = uuid.uuid4()
    main_module.pg.conversations.add(convo)
    aid = _init_text_artifact(client, content=content, filename="multi-active.txt")

    main_module.qdrant.fail_on_active_publication_at = 1
    failed = client.post(
        "/v1/artifacts/complete",
        headers=auth_headers(),
        json={"artifact_id": aid, "owner_id": "daniel", "status": "completed"},
    )
    assert failed.status_code == 503
    assert failed.json()["detail"] == "artifact text derivation failed"
    assert main_module.pg.artifacts[aid]["status"] == "pending"
    assert len([item for item in main_module.qdrant.derived_upserts if item.get("derivation_status") == "building"]) == len(expected_chunks)
    assert len(main_module.pg.embedding_refs) == len(expected_chunks)
    assert _active_rows_for_artifact(aid) == []
    partial_active_points = _active_qdrant_points_for_artifact(aid)
    assert len(partial_active_points) == 1

    before_retry = _retrieve_artifacts(client, conversation_id=convo, request_id="rid-active-mid-failed")
    assert before_retry.status_code == 200
    assert before_retry.json()["bundle"]["artifact_refs"] == []

    main_module.qdrant.fail_on_active_publication_at = None
    repaired = client.post(
        "/v1/artifacts/complete",
        headers=auth_headers(),
        json={"artifact_id": aid, "owner_id": "daniel", "status": "completed"},
    )
    assert repaired.status_code == 200
    _assert_active_derivation_complete(aid, expected_chunks)

    after_retry = _retrieve_artifacts(client, conversation_id=convo, request_id="rid-active-mid-repaired")
    assert after_retry.status_code == 200
    refs = after_retry.json()["bundle"]["artifact_refs"]
    assert refs and refs[0]["artifact_id"] == aid

    repeated = client.post(
        "/v1/artifacts/complete",
        headers=auth_headers(),
        json={"artifact_id": aid, "owner_id": "daniel", "status": "completed"},
    )
    assert repeated.status_code == 200
    _assert_active_derivation_complete(aid, expected_chunks)


def test_file_ingestion_active_publication_failure_does_not_expose_completed_artifact(client, tmp_path, monkeypatch):
    src = tmp_path / "partial.txt"
    src.write_text("alpha beta gamma delta epsilon zeta eta theta iota kappa lambda mu", encoding="utf-8")
    expected_chunks = chunk_text(src.read_text(encoding="utf-8"), chunk_size=20, chunk_overlap=0)
    monkeypatch.setattr(main_module.settings, "ingest_chunk_size_chars", 20, raising=False)
    monkeypatch.setattr(main_module.settings, "ingest_chunk_overlap_chars", 0, raising=False)
    main_module.qdrant.fail_on_active_publication_at = 1

    with pytest.raises(RuntimeError, match="injected active qdrant failure"):
        client.post(
            "/v1/ingestion/files",
            headers=auth_headers(),
            json={
                "owner_id": "daniel",
                "client_id": "vscode",
                "source_surface": "vscode",
                "repo_name": "basic-memory-store",
                "paths": [str(src)],
            },
        )

    aid = next(iter(main_module.pg.artifacts))
    assert main_module.pg.artifacts[aid]["status"] == "completed"
    assert len([item for item in main_module.qdrant.derived_upserts if item.get("derivation_status") == "building"]) == len(expected_chunks)
    assert _active_rows_for_artifact(aid) == []
    assert len(_active_qdrant_points_for_artifact(aid)) == 1

    convo = uuid.uuid4()
    main_module.pg.conversations.add(convo)
    retrieval = _retrieve_artifacts(client, conversation_id=convo, request_id="rid-file-active-failed")
    assert retrieval.status_code == 200
    assert retrieval.json()["bundle"]["artifact_refs"] == []


def test_text_artifact_postgres_activation_failure_after_qdrant_publication_retries(client, monkeypatch):
    content = b"alpha beta gamma delta epsilon zeta eta theta iota kappa lambda mu"
    expected_chunks = chunk_text(content.decode("utf-8"), chunk_size=20, chunk_overlap=0)

    class FakeObjectStore:
        def create_presigned_put_url(self, key: str, content_type: str, expires_s: int) -> str:
            return f"http://client-minio/upload/{key}"

        def create_presigned_get_url(self, key: str, expires_s: int) -> str:
            return f"http://client-minio/download/{key}"

        def head_object(self, key: str):
            return types.SimpleNamespace(size=len(content), content_type="text/plain")

        def read_object_bytes(self, key: str, *, max_bytes: int) -> bytes:
            return content

    monkeypatch.setattr(main_module.settings, "object_store_enabled", True, raising=False)
    monkeypatch.setattr(main_module.settings, "ingest_chunk_size_chars", 20, raising=False)
    monkeypatch.setattr(main_module.settings, "ingest_chunk_overlap_chars", 0, raising=False)
    monkeypatch.setattr(main_module, "object_store", FakeObjectStore(), raising=True)
    convo = uuid.uuid4()
    main_module.pg.conversations.add(convo)
    aid = _init_text_artifact(client, content=content, filename="activation.txt")

    main_module.pg.fail_activate_attempt = True
    failed = client.post(
        "/v1/artifacts/complete",
        headers=auth_headers(),
        json={"artifact_id": aid, "owner_id": "daniel", "status": "completed"},
    )
    assert failed.status_code == 503
    assert failed.json()["detail"] == "artifact text derivation failed"
    assert main_module.pg.artifacts[aid]["status"] == "pending"
    assert _active_rows_for_artifact(aid) == []
    published_point_ids = {
        str(point.get("qdrant_point_id") or point["derived_text_id"])
        for point in _active_qdrant_points_for_artifact(aid)
    }
    assert len(published_point_ids) == len(expected_chunks)

    before_retry = _retrieve_artifacts(client, conversation_id=convo, request_id="rid-activation-failed")
    assert before_retry.status_code == 200
    assert before_retry.json()["bundle"]["artifact_refs"] == []

    main_module.pg.fail_activate_attempt = False
    repaired = client.post(
        "/v1/artifacts/complete",
        headers=auth_headers(),
        json={"artifact_id": aid, "owner_id": "daniel", "status": "completed"},
    )
    assert repaired.status_code == 200
    _assert_active_derivation_complete(aid, expected_chunks)
    assert {
        str(point.get("qdrant_point_id") or point["derived_text_id"])
        for point in _active_qdrant_points_for_artifact(aid)
    } == published_point_ids

    after_retry = _retrieve_artifacts(client, conversation_id=convo, request_id="rid-activation-repaired")
    assert after_retry.status_code == 200
    refs = after_retry.json()["bundle"]["artifact_refs"]
    assert refs and refs[0]["artifact_id"] == aid


def test_text_artifact_retry_repairs_partial_multi_chunk_and_retrieves(client, monkeypatch):
    content = b"alpha beta gamma delta epsilon zeta eta theta iota kappa lambda mu"
    expected_chunks = chunk_text(content.decode("utf-8"), chunk_size=20, chunk_overlap=0)

    class FakeObjectStore:
        def create_presigned_put_url(self, key: str, content_type: str, expires_s: int) -> str:
            return f"http://client-minio/upload/{key}"

        def create_presigned_get_url(self, key: str, expires_s: int) -> str:
            return f"http://client-minio/download/{key}"

        def head_object(self, key: str):
            return types.SimpleNamespace(size=len(content), content_type="text/plain")

        def read_object_bytes(self, key: str, *, max_bytes: int) -> bytes:
            return content

    monkeypatch.setattr(main_module.settings, "object_store_enabled", True, raising=False)
    monkeypatch.setattr(main_module.settings, "ingest_chunk_size_chars", 20, raising=False)
    monkeypatch.setattr(main_module.settings, "ingest_chunk_overlap_chars", 0, raising=False)
    monkeypatch.setattr(main_module, "object_store", FakeObjectStore(), raising=True)
    convo = uuid.uuid4()
    main_module.pg.conversations.add(convo)
    real_artifact_search = main_module.qdrant.search_artifact_chunks

    r = client.post(
        "/v1/artifacts/init",
        headers=auth_headers(),
        json={
            "owner_id": "daniel",
            "client_id": "smoke",
            "conversation_id": str(convo),
            "filename": "multi.txt",
            "mime": "text/plain",
            "size": len(content),
        },
    )
    assert r.status_code == 200
    aid = r.json()["artifact_id"]

    for _ in range(3):
        main_module.qdrant.fail_after_derived_upserts = 1
        failed = client.post(
            "/v1/artifacts/complete",
            headers=auth_headers(),
            json={"artifact_id": aid, "owner_id": "daniel", "status": "completed"},
        )
        assert failed.status_code == 503
        assert main_module.pg.artifacts[aid]["status"] == "pending"
        assert not [
            row for row in main_module.pg.derived_text.values()
            if row["artifact_id"] == aid and row["derivation_params"].get("status") == "active"
        ]
        assert all(point.get("derivation_status") != "active" for point in main_module.qdrant.derived_points.values())
        main_module.qdrant.fail_after_derived_upserts = None

        class FailedArtifactHit:
            def __init__(self, row):
                self.derived_text_id = row["derived_text_id"]
                self.artifact_id = aid
                self.file_path = "multi.txt"
                self.repo_name = None
                self.score = 0.9

        failed_rows = list(main_module.pg.derived_text.values())

        async def failed_artifact_search(**kwargs):
            return [FailedArtifactHit(row) for row in failed_rows]

        async def fake_recent_before_retry(conversation_id, limit=10):
            return []

        monkeypatch.setattr(main_module.qdrant, "search_artifact_chunks", failed_artifact_search, raising=True)
        monkeypatch.setattr(main_module.pg, "get_recent_message_items", fake_recent_before_retry, raising=True)
        rid = f"rid-failed-artifact-{len(failed_rows)}"
        before_retry = client.post(
            f"/v2/conversations/{convo}/retrieve",
            headers={**auth_headers(), "X-Request-ID": rid},
            json={
                "request_id": rid,
                "owner_id": "daniel",
                "query": "alpha",
                "include_artifacts": True,
                "retrieval": {"k": 3, "min_score": 0.0, "scope": "conversation"},
            },
        )
        assert before_retry.status_code == 200
        assert before_retry.json()["bundle"]["artifact_refs"] == []
        monkeypatch.setattr(main_module.qdrant, "search_artifact_chunks", real_artifact_search, raising=True)

    main_module.qdrant.fail_after_derived_upserts = None
    repaired = client.post(
        "/v1/artifacts/complete",
        headers=auth_headers(),
        json={"artifact_id": aid, "owner_id": "daniel", "status": "completed"},
    )
    assert repaired.status_code == 200

    active_rows = [
        row for row in main_module.pg.derived_text.values()
        if row["artifact_id"] == aid and row["derivation_params"].get("status") == "active"
    ]
    assert len(active_rows) == len(expected_chunks)
    assert sorted(row["derivation_params"]["chunk_index"] for row in active_rows) == list(range(len(expected_chunks)))
    assert sorted(
        point["chunk_index"]
        for point in main_module.qdrant.derived_points.values()
        if point.get("derivation_status") == "active"
    ) == list(range(len(expected_chunks)))

    repeated = client.post(
        "/v1/artifacts/complete",
        headers=auth_headers(),
        json={"artifact_id": aid, "owner_id": "daniel", "status": "completed"},
    )
    assert repeated.status_code == 200
    assert len([
        row for row in main_module.pg.derived_text.values()
        if row["artifact_id"] == aid and row["derivation_params"].get("status") == "active"
    ]) == len(expected_chunks)

    class ArtifactHit:
        def __init__(self, row):
            self.derived_text_id = row["derived_text_id"]
            self.artifact_id = aid
            self.file_path = "multi.txt"
            self.repo_name = None
            self.score = 0.9

    async def fake_artifact_search(**kwargs):
        assert kwargs["conversation_id"] == convo
        return [ArtifactHit(active_rows[0])]

    async def fake_recent(conversation_id, limit=10):
        return []

    monkeypatch.setattr(main_module.qdrant, "search_artifact_chunks", fake_artifact_search, raising=True)
    monkeypatch.setattr(main_module.pg, "get_recent_message_items", fake_recent, raising=True)
    rid = "rid-repaired-artifact"
    retrieval = client.post(
        f"/v2/conversations/{convo}/retrieve",
        headers={**auth_headers(), "X-Request-ID": rid},
        json={
            "request_id": rid,
            "owner_id": "daniel",
            "query": "alpha",
            "include_artifacts": True,
            "retrieval": {"k": 3, "min_score": 0.0, "scope": "conversation"},
        },
    )
    assert retrieval.status_code == 200
    refs = retrieval.json()["bundle"]["artifact_refs"]
    assert refs and refs[0]["artifact_id"] == aid


def test_text_artifact_invalid_utf8_does_not_complete(client, monkeypatch):
    content = b"\xff\xfe\xfd"

    class FakeObjectStore:
        def create_presigned_put_url(self, key: str, content_type: str, expires_s: int) -> str:
            return f"http://client-minio/upload/{key}"

        def create_presigned_get_url(self, key: str, expires_s: int) -> str:
            return f"http://client-minio/download/{key}"

        def head_object(self, key: str):
            return types.SimpleNamespace(size=len(content), content_type="text/plain")

        def read_object_bytes(self, key: str, *, max_bytes: int) -> bytes:
            return content

    monkeypatch.setattr(main_module.settings, "object_store_enabled", True, raising=False)
    monkeypatch.setattr(main_module, "object_store", FakeObjectStore(), raising=True)
    aid = _init_text_artifact(client, content=content)

    r = client.post(
        "/v1/artifacts/complete",
        headers=auth_headers(),
        json={"artifact_id": aid, "owner_id": "daniel", "status": "completed"},
    )

    assert r.status_code == 422
    assert main_module.pg.artifacts[aid]["status"] == "pending"
    assert main_module.pg.derived_text == {}


def test_oversized_text_artifact_completes_without_derivation(client, monkeypatch):
    content = b"stored but not derivation eligible"

    class FakeObjectStore:
        def create_presigned_put_url(self, key: str, content_type: str, expires_s: int) -> str:
            return f"http://client-minio/upload/{key}"

        def create_presigned_get_url(self, key: str, expires_s: int) -> str:
            return f"http://client-minio/download/{key}"

        def head_object(self, key: str):
            return types.SimpleNamespace(size=len(content), content_type="text/plain")

        def read_object_bytes(self, key: str, *, max_bytes: int) -> bytes:
            raise AssertionError("oversized text should not be read for derivation")

    monkeypatch.setattr(main_module.settings, "object_store_enabled", True, raising=False)
    monkeypatch.setattr(main_module.settings, "artifact_text_derivation_max_bytes", len(content) - 1, raising=False)
    monkeypatch.setattr(main_module, "object_store", FakeObjectStore(), raising=True)
    aid = _init_text_artifact(client, content=content)

    r = client.post(
        "/v1/artifacts/complete",
        headers=auth_headers(),
        json={"artifact_id": aid, "owner_id": "daniel", "status": "completed"},
    )

    assert r.status_code == 200
    assert main_module.pg.artifacts[aid]["status"] == "completed"
    assert main_module.pg.derived_text == {}


def test_artifact_object_store_public_errors_are_bounded(client, monkeypatch):
    sentinel = "X-Amz-Credential=leak minioadmin secret-content http://minio-internal:9000"

    class InitFailureStore:
        def create_presigned_put_url(self, key: str, content_type: str, expires_s: int) -> str:
            raise RuntimeError(sentinel)

    monkeypatch.setattr(main_module.settings, "object_store_enabled", True, raising=False)
    monkeypatch.setattr(main_module, "object_store", InitFailureStore(), raising=True)
    init = client.post(
        "/v1/artifacts/init",
        headers=auth_headers(),
        json={"owner_id": "daniel", "filename": "secret.txt", "mime": "text/plain", "size": 14},
    )
    assert init.status_code == 503
    assert sentinel not in init.text
    assert "X-Amz-" not in init.text
    assert "minioadmin" not in init.text
    assert "secret-content" not in init.text
    assert "minio-internal" not in init.text

    class CompleteFailureStore:
        def create_presigned_put_url(self, key: str, content_type: str, expires_s: int) -> str:
            return f"http://client-minio/upload/{key}"

        def head_object(self, key: str):
            raise RuntimeError(sentinel)

    monkeypatch.setattr(main_module, "object_store", CompleteFailureStore(), raising=True)
    aid = _init_text_artifact(client, content=b"secret-content")
    complete = client.post(
        "/v1/artifacts/complete",
        headers=auth_headers(),
        json={"artifact_id": aid, "owner_id": "daniel", "status": "completed"},
    )
    assert complete.status_code == 503
    assert sentinel not in complete.text
    assert "X-Amz-" not in complete.text
    assert "secret-content" not in complete.text
    assert "minio-internal" not in complete.text

    class DownloadFailureStore:
        def create_presigned_put_url(self, key: str, content_type: str, expires_s: int) -> str:
            return f"http://client-minio/upload/{key}"

        def create_presigned_get_url(self, key: str, expires_s: int) -> str:
            raise RuntimeError(sentinel)

        def head_object(self, key: str):
            return types.SimpleNamespace(size=3, content_type="application/pdf")

        def read_object_bytes(self, key: str, *, max_bytes: int) -> bytes:
            raise AssertionError("pdf should not be read")

    monkeypatch.setattr(main_module, "object_store", DownloadFailureStore(), raising=True)
    pdf = client.post(
        "/v1/artifacts/init",
        headers=auth_headers(),
        json={"owner_id": "daniel", "filename": "secret.pdf", "mime": "application/pdf", "size": 3},
    )
    assert pdf.status_code == 200
    pdf_id = pdf.json()["artifact_id"]
    metadata = client.get(f"/v1/artifacts/{pdf_id}", headers=auth_headers())
    assert metadata.status_code == 503
    assert sentinel not in metadata.text
    assert "X-Amz-" not in metadata.text
    assert "minioadmin" not in metadata.text
    assert "secret-content" not in metadata.text
    assert "minio-internal" not in metadata.text
    assert main_module.pg.traces == {}


def test_artifact_complete_rejects_owner_mismatch(client):
    r1 = client.post(
        "/v1/artifacts/init",
        headers=auth_headers(),
        json={
            "owner_id": "daniel",
            "filename": "notes.txt",
            "mime": "text/plain",
            "size": 12,
        },
    )
    assert r1.status_code == 200

    r2 = client.post(
        "/v1/artifacts/complete",
        headers=auth_headers(),
        json={"artifact_id": r1.json()["artifact_id"], "owner_id": "other", "status": "completed"},
    )

    assert r2.status_code == 404


def test_artifact_key_sanitization_helper():
    assert main_module._sanitize_object_key_component("  weird /\\\\  name?.pdf  ") == "weird ___ name_.pdf"
    assert main_module._sanitize_object_key_component("   ") == "artifact"


def test_file_ingestion_creates_artifacts_and_chunks(client, tmp_path):
    src = tmp_path / "module.py"
    src.write_text("def useful_helper():\n    return 'ok'\n", encoding="utf-8")

    r = client.post(
        "/v1/ingestion/files",
        headers=auth_headers(),
        json={
            "owner_id": "daniel",
            "client_id": "vscode",
            "source_surface": "vscode",
            "repo_name": "basic-memory-store",
            "paths": [str(src)],
        },
    )
    assert r.status_code == 200
    body = r.json()
    assert body["files_ingested"] == 1
    assert body["chunks_created"] >= 1
    assert body["artifacts_created"] == 1
    assert main_module.qdrant.derived_upserts[0]["file_path"] == "module.py"


def test_file_ingestion_derives_per_file_policy_classes_and_rejects_incompatible_class(client, tmp_path):
    src_dir = tmp_path / "repo"
    src_dir.mkdir()
    py_file = src_dir / "module.py"
    md_file = src_dir / "README.md"
    py_file.write_text("def useful_helper():\n    return 'ok'\n", encoding="utf-8")
    md_file.write_text("# Notes\n\nalpha beta gamma\n", encoding="utf-8")

    incompatible = client.post(
        "/v1/ingestion/files",
        headers=auth_headers(),
        json={
            "owner_id": "daniel",
            "client_id": "vscode",
            "source_surface": "vscode",
            "repo_name": "basic-memory-store",
            "paths": [str(src_dir)],
            "policy_metadata": {
                "memory_domains": ["technical"],
                "sensitivity": "low",
                "content_class": "image",
            },
        },
    )
    assert incompatible.status_code == 422

    r = client.post(
        "/v1/ingestion/files",
        headers=auth_headers(),
        json={
            "owner_id": "daniel",
            "client_id": "vscode",
            "source_surface": "vscode",
            "repo_name": "basic-memory-store",
            "paths": [str(src_dir)],
            "policy_metadata": {
                "memory_domains": ["technical"],
                "sensitivity": "low",
            },
        },
    )

    assert r.status_code == 200
    body = r.json()
    assert body["files_ingested"] == 2
    policy_by_file = {
        row["file_path"]: row["policy_metadata"]
        for row in main_module.pg.artifacts.values()
    }
    assert policy_by_file["module.py"]["content_class"] == "code"
    assert policy_by_file["README.md"]["content_class"] == "document"
    qdrant_policy_by_file = {
        point["file_path"]: point["policy_metadata"]
        for point in main_module.qdrant.derived_upserts
        if point.get("derivation_status") == "active"
    }
    assert qdrant_policy_by_file["module.py"]["content_class"] == "code"
    assert qdrant_policy_by_file["README.md"]["content_class"] == "document"

    convo = uuid.uuid4()
    main_module.pg.conversations.add(convo)
    retrieval = client.post(
        f"/v2/conversations/{convo}/retrieve",
        headers={**auth_headers(), "X-Request-ID": "rid-file-policy-retrieval"},
        json={
            "request_id": "rid-file-policy-retrieval",
            "owner_id": "daniel",
            "query": "helper notes",
            "include_artifacts": True,
            "retrieval": {"k": 3, "min_score": 0.0, "scope": "conversation"},
            "containment_policy": {
                "enforcement_mode": "mandatory",
                "allowed_memory_domains": ["technical"],
                "blocked_memory_domains": [],
                "artifact_access_policy": {
                    "enforcement_mode": "mandatory",
                    "allowed_content_classes": ["document", "code"],
                    "allowed_domains": ["technical"],
                    "maximum_sensitivity": "medium",
                    "surface_content_capabilities": ["document", "code"],
                    "reason_codes": ["artifact_policy_applied"],
                },
            },
        },
    )
    assert retrieval.status_code == 200
    refs = retrieval.json()["bundle"]["artifact_refs"]
    assert {ref["file_path"] for ref in refs} == {"module.py", "README.md"}
    assert {ref["policy_metadata"]["content_class"] for ref in refs} == {"code", "document"}


def test_v2_retrieval_returns_same_uploaded_artifact_source_metadata(client, monkeypatch):
    convo = uuid.uuid4()
    main_module.pg.conversations.add(convo)
    artifact_id = uuid.uuid4()
    content = "uploaded artifact bounded snippet alpha beta gamma"

    artifact = {
        "artifact_id": str(artifact_id),
        "owner_id": "daniel",
        "client_id": "smoke",
        "conversation_id": str(convo),
        "filename": "notes.txt",
        "mime": "text/plain",
        "size": len(content),
        "object_uri": "artifacts/daniel/notes.txt",
        "source_surface": "vscode",
        "status": "completed",
        "sha256": None,
        "created_at": "2026-01-01 00:00:00+00:00",
        "completed_at": "2026-01-01 00:00:10+00:00",
        "source_kind": None,
        "repo_name": None,
        "repo_ref": None,
        "file_path": "notes.txt",
        "ingestion_id": None,
    }
    main_module.pg.artifacts[str(artifact_id)] = artifact

    async def fake_recent(conversation_id, limit=10):
        return []

    monkeypatch.setattr(main_module.pg, "get_recent_message_items", fake_recent, raising=True)
    monkeypatch.setattr(main_module.settings, "retrieval_artifact_max_snippet_chars", 100, raising=False)
    import anyio

    async def derive():
        await main_module.derive_text_chunks_for_artifact(
            pg=main_module.pg,
            qdrant=main_module.qdrant,
            settings=main_module.settings,
            artifact=artifact,
            text=content,
            derivation_version=main_module.TEXT_ARTIFACT_DERIVATION_VERSION,
            file_path="notes.txt",
        )

    anyio.run(derive)
    derived_id = next(iter(main_module.pg.derived_text))

    class ArtifactHit:
        def __init__(self):
            self.derived_text_id = derived_id
            self.artifact_id = str(artifact_id)
            self.file_path = "notes.txt"
            self.repo_name = None
            self.score = 0.92

    async def fake_artifact_search(**kwargs):
        assert kwargs["owner_id"] == "daniel"
        assert kwargs["conversation_id"] == convo
        return [ArtifactHit()]

    monkeypatch.setattr(main_module.qdrant, "search_artifact_chunks", fake_artifact_search, raising=True)

    r = client.post(
        f"/v2/conversations/{convo}/retrieve",
        headers={**auth_headers(), "X-Request-ID": "rid-artifact-v2"},
        json={
            "request_id": "rid-artifact-v2",
            "owner_id": "daniel",
            "query": "alpha",
            "include_artifacts": True,
            "retrieval": {"k": 3, "min_score": 0.0, "scope": "conversation"},
        },
    )

    assert r.status_code == 200
    body = r.json()
    assert body["request_id"] == "rid-artifact-v2"
    assert body["conversation_id"] == str(convo)
    refs = body["bundle"]["artifact_refs"]
    assert len(refs) == 1
    ref = refs[0]
    assert ref["artifact_id"] == str(artifact_id)
    assert "uploaded artifact bounded snippet" in ref["snippet"]
    assert ref["source_ref"] == {"ref_type": "derived_text", "ref_id": derived_id}
    assert ref["provenance"]["source_refs"] == [
        {"ref_type": "artifact", "ref_id": str(artifact_id), "support_kind": "direct"}
    ]
    assert ref["source_availability"] == "available"


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


def test_tiered_retrieve_mandatory_policy_filters_all_tiers(client, monkeypatch):
    convo = str(uuid.uuid4())
    main_module.pg.conversations.add(uuid.UUID(convo))
    spoof_id = str(uuid.uuid4())
    eligible_id = str(uuid.uuid4())
    policy = {
        "memory_domains": ["technical"],
        "sensitivity": "low",
        "entity_ids": [],
        "relationship_ids": [],
        "relationship_scopes": [],
    }
    containment = {
        "enforcement_mode": "mandatory",
        "allowed_memory_domains": ["technical"],
        "blocked_memory_domains": ["finance"],
        "artifact_access_policy": {
            "enforcement_mode": "mandatory",
            "allowed_content_classes": ["document", "code"],
            "allowed_domains": ["technical"],
            "maximum_sensitivity": "medium",
            "surface_content_capabilities": ["document", "code"],
            "reason_codes": ["artifact_policy_applied"],
        },
    }
    calls = {}

    class Hit:
        def __init__(self, message_id, score):
            self.message_id = message_id
            self.score = score

    async def fake_search(**kwargs):
        calls["semantic_policy_filter"] = kwargs.get("policy_filter")
        return [Hit(message_id=spoof_id, score=0.99), Hit(message_id=eligible_id, score=0.2)]

    async def fake_snips(ids):
        return [
            {
                "message_id": spoof_id,
                "conversation_id": convo,
                "role": "user",
                "content": "spoof semantic",
                "metadata": {"retrieval_policy_metadata": policy},
                "policy_metadata": None,
                "created_at": "2026-01-01 00:00:00+00:00",
            },
            {
                "message_id": eligible_id,
                "conversation_id": convo,
                "role": "user",
                "content": "eligible semantic",
                "metadata": {},
                "policy_metadata": policy,
                "created_at": "2026-01-01 00:00:00+00:00",
            },
        ]

    async def fake_recent(conversation_id, limit=10, policy_filter=None):
        calls["working_policy_filter"] = policy_filter
        calls["working_limit"] = limit
        return [
            {
                "message_id": str(uuid.uuid4()),
                "conversation_id": str(conversation_id),
                "role": "assistant",
                "content": "malformed working",
                "metadata": {},
                "policy_metadata": {"memory_domains": "technical", "sensitivity": "low"},
                "created_at": "2026-01-01 00:00:00+00:00",
            },
            {
                "message_id": str(uuid.uuid4()),
                "conversation_id": str(conversation_id),
                "role": "assistant",
                "content": "eligible working",
                "metadata": {},
                "policy_metadata": policy,
                "created_at": "2026-01-01 00:00:00+00:00",
            },
        ]

    async def fake_pinned(owner_id, conversation_id=None, limit=5, policy_filter=None):
        calls["pinned_policy_filter"] = policy_filter
        calls["pinned_limit"] = limit
        return [
            {"id": "pin-spoof", "content": "spoof pin", "metadata": {}, "policy_metadata": None},
            {"id": "pin-ok", "content": "eligible pin", "metadata": {}, "policy_metadata": policy},
        ]

    async def fake_policy(owner_id, surface=None, policy_filter=None):
        calls["policy_overlay_filter"] = policy_filter
        return [
            {"id": "policy-blocked", "content": "blocked", "metadata": {}, "policy_metadata": {**policy, "memory_domains": ["finance"]}},
            {"id": "policy-ok", "content": "eligible policy", "metadata": {}, "policy_metadata": policy},
        ]

    async def fake_persona(owner_id, surface=None, policy_filter=None):
        calls["persona_overlay_filter"] = policy_filter
        return [
            {"id": "persona-malformed", "content": "bad", "metadata": {}, "policy_metadata": {"memory_domains": ["technical"], "sensitivity": "restricted"}},
            {"id": "persona-ok", "content": "eligible persona", "metadata": {}, "policy_metadata": policy},
        ]

    monkeypatch.setattr(main_module.qdrant, "search", fake_search, raising=True)
    monkeypatch.setattr(main_module.pg, "get_message_snippets_by_ids", fake_snips, raising=True)
    monkeypatch.setattr(main_module.pg, "get_recent_message_items", fake_recent, raising=True)
    monkeypatch.setattr(main_module.pg, "get_pinned_memories", fake_pinned, raising=True)
    monkeypatch.setattr(main_module.pg, "get_policy_overlays", fake_policy, raising=True)
    monkeypatch.setattr(main_module.pg, "get_persona_overlays", fake_persona, raising=True)

    r = client.post(
        f"/v1/conversations/{convo}/retrieve",
        headers=auth_headers(),
        json={
            "owner_id": "daniel",
            "query": "note",
            "k": 4,
            "working_limit": 2,
            "pinned_limit": 2,
            "containment_policy": containment,
        },
    )

    assert r.status_code == 200
    body = r.json()
    assert [item["message_id"] for item in body["semantic"]] == [eligible_id]
    assert [item["content"] for item in body["working"]] == ["eligible working"]
    assert [item["id"] for item in body["pinned"]] == ["pin-ok"]
    assert [item["id"] for item in body["policy"]] == ["policy-ok"]
    assert [item["id"] for item in body["persona"]] == ["persona-ok"]
    assert calls["semantic_policy_filter"]["allowed_domains"] == ["technical"]
    assert calls["working_policy_filter"]["blocked_domains"] == ["finance"]
    assert calls["working_limit"] == 2
    assert calls["pinned_policy_filter"]["allowed_domains"] == ["technical"]
    assert calls["pinned_limit"] == 2
    assert calls["policy_overlay_filter"]["allowed_domains"] == ["technical"]
    assert calls["persona_overlay_filter"]["allowed_domains"] == ["technical"]


def test_orchestrate_chat_and_trace_read(client):
    rid = "rid-orchestrate-test"
    r = client.post(
        "/v1/orchestrate/chat",
        headers={**auth_headers(), "X-Request-ID": rid},
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
    assert request_id == rid

    r2 = client.get(f"/v1/traces/{request_id}", headers=auth_headers())
    assert r2.status_code == 200
    trace = r2.json()
    assert trace["request_id"] == request_id
    assert trace["surface"] == "vscode"
    assert trace["profile"]["surface_context"]["surface_type"] == "vscode"
    assert trace["profile"]["surface_context"]["spoken_output"] is False


def test_orchestrate_request_surface_context_is_optional():
    req = OrchestrateChatRequest(
        owner_id="owner",
        client_id="client",
        messages=[{"role": "user", "content": "ping"}],
    )
    assert req.surface_context is None
    assert req.surface == "unknown"


def test_resolve_surface_behavior_prefers_surface_context_surface_type():
    req = OrchestrateChatRequest(
        owner_id="owner",
        client_id="client",
        surface="vscode",
        surface_context={"surface_type": "telegram", "spoken_output": False},
        messages=[{"role": "user", "content": "ping"}],
    )

    resolved = main_module._resolve_surface_behavior(req.surface, req.surface_context)

    assert resolved["surface_type"] == "telegram"
    assert resolved["compatibility_note"] == {
        "kind": "surface_type_override",
        "top_level_surface": "vscode",
        "surface_context_surface_type": "telegram",
        "resolved_surface_type": "telegram",
    }


def test_orchestrate_chat_without_surface_context_keeps_default_prompt_shape(client):
    r = client.post(
        "/v1/orchestrate/chat",
        headers=auth_headers(),
        json={
            "owner_id": "daniel",
            "client_id": "vscode",
            "messages": [{"role": "user", "content": "ping"}],
        },
    )
    assert r.status_code == 200

    prompt_text = _system_prompt_text()
    assert "Surface behavior guidance:" not in prompt_text
    assert "spoken delivery" not in prompt_text


def test_orchestrate_chat_telegram_surface_stays_text_first(client):
    rid = "rid-telegram-surface"
    r = client.post(
        "/v1/orchestrate/chat",
        headers={**auth_headers(), "X-Request-ID": rid},
        json={
            "owner_id": "daniel",
            "client_id": "telegram",
            "messages": [{"role": "user", "content": "ping"}],
            "surface_context": {
                "surface_type": "telegram",
                "interaction_mode": "text",
                "spoken_output": False,
                "output_format": "plain_text",
            },
        },
    )
    assert r.status_code == 200

    prompt_text = _system_prompt_text()
    assert "Surface behavior guidance:" not in prompt_text
    assert "spoken delivery" not in prompt_text

    trace = client.get(f"/v1/traces/{rid}", headers=auth_headers()).json()
    assert trace["surface"] == "telegram"
    assert trace["profile"]["surface_context"]["surface_type"] == "telegram"
    assert trace["profile"]["surface_context"]["spoken_output"] is False
    assert trace["profile"]["surface_context"]["interaction_mode"] == "text"


def test_orchestrate_chat_voice_surface_keeps_default_prompt_shape_and_preserves_trace(client):
    rid = "rid-voice-surface"
    r = client.post(
        "/v1/orchestrate/chat",
        headers={**auth_headers(), "X-Request-ID": rid},
        json={
            "owner_id": "daniel",
            "client_id": "car",
            "messages": [{"role": "user", "content": "ping"}],
            "surface_context": {
                "surface_type": "car",
                "interaction_mode": "voice_mediated",
                "spoken_output": True,
                "active_task_mode": True,
                "allows_expansion": False,
                "output_format": "speech",
                "latency_preference": "low",
            },
        },
    )
    assert r.status_code == 200

    prompt_text = _system_prompt_text()
    assert "Surface behavior guidance:" not in prompt_text
    assert "spoken delivery" not in prompt_text

    trace = client.get(f"/v1/traces/{rid}", headers=auth_headers()).json()
    assert trace["surface"] == "car"
    assert trace["profile"]["surface_context"]["surface_type"] == "car"
    assert trace["profile"]["surface_context"]["interaction_mode"] == "voice_mediated"
    assert trace["profile"]["surface_context"]["spoken_output"] is True
    assert trace["profile"]["surface_context"]["active_task_mode"] is True
    assert trace["profile"]["surface_context"]["allows_expansion"] is False
    assert trace["profile"]["surface_context"]["output_format"] == "speech"
    assert trace["profile"]["surface_context"]["latency_preference"] == "low"


def test_orchestrate_chat_surface_context_overrides_legacy_surface(client):
    rid = "rid-surface-override"
    r = client.post(
        "/v1/orchestrate/chat",
        headers={**auth_headers(), "X-Request-ID": rid},
        json={
            "owner_id": "daniel",
            "client_id": "vscode",
            "surface": "vscode",
            "messages": [{"role": "user", "content": "ping"}],
            "surface_context": {
                "surface_type": "telegram",
                "interaction_mode": "text",
                "spoken_output": False,
            },
        },
    )
    assert r.status_code == 200

    trace = client.get(f"/v1/traces/{rid}", headers=auth_headers()).json()
    assert trace["surface"] == "telegram"
    assert trace["profile"]["surface_context"]["surface_type"] == "telegram"
    assert trace["profile"]["surface_compatibility_note"] == {
        "kind": "surface_type_override",
        "top_level_surface": "vscode",
        "surface_context_surface_type": "telegram",
        "resolved_surface_type": "telegram",
    }


def test_v1_chat_includes_artifact_snippets_in_prompt(client, monkeypatch):
    derived_id = str(uuid.uuid4())

    class ArtifactHit:
        def __init__(self):
            self.derived_text_id = derived_id
            self.artifact_id = str(uuid.uuid4())
            self.file_path = "api/main.py"
            self.repo_name = "basic-memory-store"
            self.score = 0.72

    async def fake_artifact_search(**kwargs):
        return [ArtifactHit()]

    async def fake_derived(ids):
        return [{
            "derived_text_id": derived_id,
            "artifact_id": str(uuid.uuid4()),
            "text": "def build_context_block(): pass",
            "file_path": "api/main.py",
            "repo_name": "basic-memory-store",
        }]

    monkeypatch.setattr(main_module.qdrant, "search_artifact_chunks", fake_artifact_search, raising=True)
    monkeypatch.setattr(main_module.pg, "get_derived_text_snippets_by_ids", fake_derived, raising=True)

    r = client.post(
        "/v1/chat",
        headers=auth_headers(),
        json={
            "owner_id": "daniel",
            "client_id": "smoke",
            "messages": [{"role": "user", "content": "Where is context built?"}],
        },
    )
    assert r.status_code == 200
    prompt_messages = main_module.litellm.calls[-1]["messages"]
    assert any("Relevant ingested file excerpts:" in item["content"] for item in prompt_messages if item["role"] == "system")


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
    migration_path = (
        Path(__file__).resolve().parents[2]
        / "db"
        / "migrations"
        / "legacy"
        / "20260214_pinned_memories_nullable.sql"
    )
    sql = migration_path.read_text()
    assert "ALTER COLUMN conversation_id DROP NOT NULL" in sql
    assert "ON DELETE SET NULL" in sql
