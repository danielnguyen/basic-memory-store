import types
import uuid

from fastapi.testclient import TestClient

import main as main_module


class FakePG:
    def __init__(self):
        self.messages = []
        self.event_logs = {}
        self.event_logs_by_id = {}
        self.conversations_by_key = {}
        self.entities = {}

    async def open(self):
        return None

    async def close(self):
        return None

    async def ping(self):
        return True

    async def create_conversation(self, owner_id: str, client_id: str | None = None, title: str | None = None):
        cid = uuid.uuid4()
        self.conversations_by_key[(owner_id, client_id)] = {
            "conversation_id": str(cid),
            "owner_id": owner_id,
            "client_id": client_id,
            "title": title,
        }
        return cid

    async def get_conversation_by_owner_client(self, owner_id: str, client_id: str):
        return self.conversations_by_key.get((owner_id, client_id))

    async def get_or_create_event_stream_conversation(self, owner_id: str, client_id: str, title: str | None = None):
        existing = await self.get_conversation_by_owner_client(owner_id, client_id)
        if existing is not None:
            return uuid.UUID(existing["conversation_id"])
        return await self.create_conversation(owner_id=owner_id, client_id=client_id, title=title)

    async def claim_event_ingest(
        self,
        *,
        owner_id: str,
        source_type: str,
        source_event_id: str,
        event_type: str,
        event_time: str | None,
        payload_json: dict,
    ):
        key = (owner_id, source_type, source_event_id)
        existing = self.event_logs.get(key)
        if existing is not None:
            return existing.copy(), False
        row = {
            "event_log_id": str(uuid.uuid4()),
            "owner_id": owner_id,
            "source_type": source_type,
            "source_event_id": source_event_id,
            "event_type": event_type,
            "event_time": event_time,
            "payload_json": payload_json,
            "conversation_id": None,
            "message_id": None,
            "created_at": "2026-04-06T00:00:00+00:00",
        }
        self.event_logs[key] = row
        self.event_logs_by_id[row["event_log_id"]] = row
        return row.copy(), True

    async def finalize_event_ingest(self, *, event_log_id, conversation_id, message_id):
        row = self.event_logs_by_id[str(event_log_id)]
        row["conversation_id"] = str(conversation_id)
        row["message_id"] = str(message_id)
        return {
            "event_log_id": row["event_log_id"],
            "conversation_id": row["conversation_id"],
            "message_id": row["message_id"],
        }

    async def add_message(self, conversation_id, owner_id, role, content, client_id=None, metadata=None):
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
                "created_at": "2026-04-06T00:00:00+00:00",
            }
        )
        return mid

    async def get_message_snippets_by_ids(self, ids):
        wanted = {str(item) for item in ids}
        return [
            {
                "message_id": row["message_id"],
                "conversation_id": row["conversation_id"],
                "role": row["role"],
                "content": row["content"],
                "metadata": row["metadata"],
                "created_at": row["created_at"],
            }
            for row in self.messages
            if row["message_id"] in wanted
        ]

    async def upsert_memory_entity(self, *, owner_id: str, entity_type: str, canonical_name: str, metadata=None):
        normalized_key = " ".join(canonical_name.strip().lower().split())
        key = (owner_id, entity_type, normalized_key)
        existing = self.entities.get(key)
        if existing is None:
            existing = {
                "entity_id": str(uuid.uuid4()),
                "owner_id": owner_id,
                "entity_type": entity_type,
                "canonical_name": canonical_name,
                "normalized_key": normalized_key,
                "metadata": metadata or {},
            }
            self.entities[key] = existing
        else:
            existing["canonical_name"] = canonical_name
            existing["metadata"] = {**existing["metadata"], **(metadata or {})}
        return existing.copy()


class FakeQdrant:
    def __init__(self):
        self.upserts = []

    def ping(self):
        return True

    async def upsert_message_vector(self, **kwargs):
        self.upserts.append(kwargs)
        return True

    async def search(self, owner_id, query, k, min_score, conversation_id=None, client_id=None, exclude_message_ids=None):
        query_words = [word.lower() for word in query.split() if word.strip()]
        hits = []
        for item in reversed(self.upserts):
            content = str(item["content"]).lower()
            if all(word in content for word in query_words):
                hits.append(types.SimpleNamespace(message_id=str(item["message_id"]), score=0.95))
        return hits[:k]


def _settings():
    return types.SimpleNamespace(
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
        retrieval_artifact_k=3,
        retrieval_artifact_max_snippet_chars=500,
        object_store_endpoint="",
        object_store_bucket="",
        object_store_access_key="",
        object_store_secret_key="",
        object_store_region="",
        object_store_presign_base_url=None,
        object_store_include_content_type_in_put_signature=False,
    )


def _headers(request_id: str):
    return {"X-API-Key": "testkey", "X-Request-ID": request_id}


def _client(monkeypatch):
    fake_pg = FakePG()
    fake_qdrant = FakeQdrant()
    monkeypatch.setattr(main_module, "settings", _settings(), raising=True)
    monkeypatch.setattr(main_module, "pg", fake_pg, raising=True)
    monkeypatch.setattr(main_module, "qdrant", fake_qdrant, raising=True)
    client = TestClient(main_module.app)
    return client, fake_pg, fake_qdrant


def test_git_event_ingest_is_idempotent(monkeypatch):
    client, fake_pg, _ = _client(monkeypatch)
    try:
        body = {
            "request_id": "git-r12-1",
            "owner_id": "owner",
            "source_type": "git",
            "source_event_id": "push-123",
            "event_type": "push",
            "payload_json": {"summary": "Merged memory ingest patch"},
        }
        first = client.post("/v1/events/ingest", headers=_headers("git-r12-1"), json=body)
        assert first.status_code == 200
        second = client.post(
            "/v1/events/ingest",
            headers=_headers("git-r12-2"),
            json={**body, "request_id": "git-r12-2"},
        )
        assert second.status_code == 200
        assert first.json()["created"] is True
        assert second.json()["created"] is False
        assert len(fake_pg.messages) == 1
        assert len(fake_pg.event_logs) == 1
    finally:
        client.close()


def test_calendar_event_creates_message_and_log(monkeypatch):
    client, fake_pg, _ = _client(monkeypatch)
    try:
        body = {
            "request_id": "cal-r12-1",
            "owner_id": "owner",
            "source_type": "calendar",
            "source_event_id": "evt-456",
            "event_type": "upcoming_event",
            "event_time": "2026-04-07T13:00:00Z",
            "payload_json": {"title": "1:1 with Alice", "location": "Zoom"},
        }
        response = client.post("/v1/events/ingest", headers=_headers("cal-r12-1"), json=body)
        assert response.status_code == 200
        payload = response.json()
        assert payload["created"] is True
        assert len(fake_pg.messages) == 1
        message = fake_pg.messages[0]
        assert message["role"] == "tool"
        assert message["metadata"]["event_memory"] is True
        log = next(iter(fake_pg.event_logs.values()))
        assert log["message_id"] == message["message_id"]
        assert log["conversation_id"] == message["conversation_id"]
    finally:
        client.close()


def test_incomplete_prior_claim_continues_and_finalizes(monkeypatch):
    client, fake_pg, fake_qdrant = _client(monkeypatch)
    try:
        key = ("owner", "git", "push-stuck")
        fake_pg.event_logs[key] = {
            "event_log_id": str(uuid.uuid4()),
            "owner_id": "owner",
            "source_type": "git",
            "source_event_id": "push-stuck",
            "event_type": "push",
            "event_time": None,
            "payload_json": {"summary": "Recovered from incomplete claim"},
            "conversation_id": None,
            "message_id": None,
            "created_at": "2026-04-06T00:00:00+00:00",
        }
        fake_pg.event_logs_by_id[fake_pg.event_logs[key]["event_log_id"]] = fake_pg.event_logs[key]

        response = client.post(
            "/v1/events/ingest",
            headers=_headers("git-r12-retry"),
            json={
                "request_id": "git-r12-retry",
                "owner_id": "owner",
                "source_type": "git",
                "source_event_id": "push-stuck",
                "event_type": "push",
                "payload_json": {"summary": "Recovered from incomplete claim"},
            },
        )
        assert response.status_code == 200
        payload = response.json()
        assert payload["created"] is True
        assert len(fake_pg.messages) == 1
        message = fake_pg.messages[0]
        assert message["role"] == "tool"
        assert fake_pg.event_logs[key]["message_id"] == message["message_id"]
        assert fake_pg.event_logs[key]["conversation_id"] == message["conversation_id"]
        assert len(fake_qdrant.upserts) == 1
    finally:
        client.close()


def test_portfolio_event_is_retrievable_via_existing_api_path_and_upsert(monkeypatch):
    client, fake_pg, fake_qdrant = _client(monkeypatch)
    try:
        ingest = client.post(
            "/v1/events/ingest",
            headers=_headers("fin-r12-1"),
            json={
                "request_id": "fin-r12-1",
                "owner_id": "owner",
                "source_type": "finance",
                "source_event_id": "txn-789",
                "event_type": "transaction_import",
                "payload_json": {
                    "summary": "Bought 15 NVDA shares in taxable account",
                    "account": "taxable account",
                    "symbol": "NVDA",
                },
            },
        )
        assert ingest.status_code == 200
        assert len(fake_qdrant.upserts) == 1
        assert fake_qdrant.upserts[0]["role"] == "tool"
        message = fake_pg.messages[0]
        assert message["client_id"] == "event-stream:portfolio"
        assert message["metadata"]["source_type"] == "portfolio"
        assert message["metadata"]["source_type_original"] == "finance"

        retrieve = client.post(
            "/v1/retrieve",
            headers={"X-API-Key": "testkey"},
            json={"owner_id": "owner", "query": "NVDA taxable account", "k": 5, "min_score": 0.2},
        )
        assert retrieve.status_code == 200
        hits = retrieve.json()["hits"]
        assert len(hits) == 1
        assert "NVDA shares in taxable account" in hits[0]["content"]
    finally:
        client.close()


def test_event_ingest_upserts_entities_when_provided(monkeypatch):
    client, fake_pg, _ = _client(monkeypatch)
    try:
        response = client.post(
            "/v1/events/ingest",
            headers=_headers("entity-r12-1"),
            json={
                "request_id": "entity-r12-1",
                "owner_id": "owner",
                "source_type": "git",
                "source_event_id": "pr-456",
                "event_type": "pr_merged",
                "payload_json": {"summary": "Merged PR for project Equinox"},
                "entities": [
                    {"entity_type": "project", "canonical_name": "Equinox", "metadata": {"repo": "basic-memory-store"}},
                    {"entity_type": "person", "canonical_name": "Alice Nguyen", "metadata": {"role": "reviewer"}},
                ],
            },
        )
        assert response.status_code == 200
        assert len(fake_pg.entities) == 2
        normalized_keys = {row["normalized_key"] for row in fake_pg.entities.values()}
        assert "equinox" in normalized_keys
        assert "alice nguyen" in normalized_keys
    finally:
        client.close()
