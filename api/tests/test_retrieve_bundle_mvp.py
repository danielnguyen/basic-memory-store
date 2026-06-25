import types
import uuid

import httpx
import pytest

import main as main_module


class FakePG:
    def __init__(self, *, message_times=None, message_metadata=None, artifact_metadata=None, memory_items_by_ref=None):
        self.message_times = message_times or ["2026-01-01T00:00:00+00:00"]
        self.message_metadata = message_metadata or {}
        self.artifact_metadata = artifact_metadata or {}
        self.memory_items_by_ref = memory_items_by_ref or {}
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
            key = str(item)
            out.append(
                {
                    "message_id": key,
                    "conversation_id": self.last_conversation_id if idx == 0 and self.last_conversation_id else str(uuid.uuid4()),
                    "role": "assistant",
                    "content": f"semantic result {idx}",
                    "metadata": self.message_metadata.get(key, {}),
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
                "metadata": {},
                "created_at": "2026-01-01T00:00:00+00:00",
            }
        ]

    async def get_derived_text_snippets_by_ids(self, ids):
        return (
            [
                {
                    "derived_text_id": str(item),
                    "artifact_id": str(uuid.uuid4()),
                    "owner_id": "owner",
                    "kind": "chunk",
                    "text": "def important_helper(): pass",
                    "derivation_params": self.artifact_metadata.get(str(item), {}),
                    "created_at": "2026-01-01T00:00:00+00:00",
                    "file_path": "api/helpers.py",
                    "repo_name": "basic-memory-store",
                    "mime": "text/plain",
                }
                for item in ids
            ]
            if ids
            else []
        )

    async def get_memory_items_for_source_refs(self, *, owner_id, source_refs):
        out = {}
        for ref in source_refs:
            key = (ref["ref_type"], ref["ref_id"])
            if key in self.memory_items_by_ref:
                out[key] = self.memory_items_by_ref[key]
        return out


class FakeQdrant:
    def __init__(self, *, message_scores=None):
        self.message_scores = message_scores or [0.77]
        self.artifact_search_calls = []

    def ping(self):
        return True

    async def search(self, **kwargs):
        return [
            types.SimpleNamespace(message_id=str(uuid.uuid4()), score=score)
            for score in self.message_scores
        ]

    async def search_artifact_chunks(self, **kwargs):
        self.artifact_search_calls.append(kwargs)
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


async def _post_retrieve_bundle(
    *,
    conversation_id: str,
    request_id: str,
    body: dict[str, object],
):
    async with main_module.app.router.lifespan_context(main_module.app):
        transport = httpx.ASGITransport(app=main_module.app)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://testserver",
        ) as client:
            return await client.post(
                f"/v2/conversations/{conversation_id}/retrieve",
                headers={"X-API-Key": "testkey", "X-Request-ID": request_id},
                json=body,
            )


@pytest.mark.asyncio
async def test_retrieve_bundle_shape(monkeypatch):
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

    rid = "rid-1"
    conversation_id = str(uuid.uuid4())
    r = await _post_retrieve_bundle(
        conversation_id=conversation_id,
        request_id=rid,
        body={"request_id": rid, "owner_id": "owner", "query": "hello"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["request_id"] == rid
    assert body["conversation_id"] == conversation_id
    assert body["bundle"]["recent"][0]["content"] == "recent snippet"
    assert body["bundle"]["recent"][0]["memory_id"] is None
    assert body["bundle"]["recent"][0]["freshness_state"] == "unknown_freshness"
    assert body["bundle"]["semantic"][0]["content"] == "semantic result 0"
    assert body["bundle"]["semantic"][0]["memory_id"] is None
    assert body["bundle"]["semantic"][0]["score"] >= 0.77
    assert body["bundle"]["semantic"][0]["score_details"]["semantic_score"] == 0.77
    assert body["bundle"]["semantic"][0]["source_ref"]["ref_type"] == "message"
    assert body["bundle"]["artifact_refs"][0]["file_path"] == "api/helpers.py"
    assert body["bundle"]["artifact_refs"][0]["memory_id"] is None
    assert body["bundle"]["artifact_refs"][0]["freshness_state"] == "unknown_freshness"
    assert len(body["bundle"]["artifact_refs"]) == 1
    assert body["bundle"]["observed_metadata"] == {
        "mime_types": ["text/plain"],
        "has_artifacts": True,
        "has_code_like_content": True,
        "estimated_chars": len("recent snippetsemantic result 0def important_helper(): pass"),
    }
    assert body["bundle"]["token_estimate_total"] == len("recent snippetsemantic result 0def important_helper(): pass") // 4
    assert body["bundle"]["retrieval_debug"]["time_window"] == "all"
    assert body["bundle"]["retrieval_debug"]["retrieval_mode"] == "balanced"
    assert body["bundle"]["retrieval_debug"]["artifacts_included"] is True
    assert body["bundle"]["retrieval_debug"]["domain_filters_requested"] is False
    assert "pinned memories are not part of the v2 ranked bundle" in body["bundle"]["retrieval_debug"]["pinned_handling"]


@pytest.mark.asyncio
async def test_retrieve_bundle_recent_mode_with_30d_window(monkeypatch):
    fake_pg = FakePG(
        message_times=[
            "2026-06-10T00:00:00+00:00",
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

    rid = "rid-recent"
    conversation_id = str(uuid.uuid4())
    r = await _post_retrieve_bundle(
        conversation_id=conversation_id,
        request_id=rid,
        body={
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


@pytest.mark.asyncio
async def test_retrieve_bundle_historical_mode_with_older_content(monkeypatch):
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

    rid = "rid-historical"
    conversation_id = str(uuid.uuid4())
    r = await _post_retrieve_bundle(
        conversation_id=conversation_id,
        request_id=rid,
        body={
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


@pytest.mark.asyncio
async def test_retrieve_bundle_skips_artifact_search_when_include_artifacts_false(monkeypatch):
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

    rid = "rid-no-artifacts"
    conversation_id = str(uuid.uuid4())
    r = await _post_retrieve_bundle(
        conversation_id=conversation_id,
        request_id=rid,
        body={
            "request_id": rid,
            "owner_id": "owner",
            "query": "hello",
            "include_artifacts": False,
        },
    )
    assert r.status_code == 200
    body = r.json()
    assert body["bundle"]["semantic"][0]["content"] == "semantic result 0"
    assert body["bundle"]["artifact_refs"] == []
    assert body["bundle"]["observed_metadata"]["has_artifacts"] is False
    assert body["bundle"]["retrieval_debug"]["artifacts_included"] is False
    assert fake_qdrant.artifact_search_calls == []


@pytest.mark.asyncio
async def test_retrieve_bundle_explicit_include_artifacts_true_keeps_artifact_search(monkeypatch):
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

    rid = "rid-explicit-artifacts"
    conversation_id = str(uuid.uuid4())
    r = await _post_retrieve_bundle(
        conversation_id=conversation_id,
        request_id=rid,
        body={
            "request_id": rid,
            "owner_id": "owner",
            "query": "hello",
            "include_artifacts": True,
        },
    )
    assert r.status_code == 200
    body = r.json()
    assert len(body["bundle"]["artifact_refs"]) == 1
    assert body["bundle"]["retrieval_debug"]["artifacts_included"] is True
    assert len(fake_qdrant.artifact_search_calls) == 1


@pytest.mark.asyncio
async def test_retrieve_bundle_omitted_include_artifacts_remains_backward_compatible(monkeypatch):
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

    rid = "rid-default-artifacts"
    conversation_id = str(uuid.uuid4())
    r = await _post_retrieve_bundle(
        conversation_id=conversation_id,
        request_id=rid,
        body={
            "request_id": rid,
            "owner_id": "owner",
            "query": "hello",
        },
    )
    assert r.status_code == 200
    body = r.json()
    assert len(body["bundle"]["artifact_refs"]) == 1
    assert body["bundle"]["retrieval_debug"]["artifacts_included"] is True
    assert len(fake_qdrant.artifact_search_calls) == 1


def test_retrieve_bundle_request_accepts_omitted_domain_filters():
    request = main_module.RetrieveBundleRequest(
        request_id="rid-shape",
        owner_id="owner",
        query="hello",
    )
    assert request.allowed_memory_domains is None
    assert request.blocked_memory_domains is None


@pytest.mark.asyncio
async def test_retrieve_bundle_allows_explicitly_tagged_allowed_items(monkeypatch):
    allowed_id = str(uuid.uuid4())
    fake_pg = FakePG(
        message_metadata={
            allowed_id: {"memory_domain": "technical"},
        }
    )
    fake_qdrant = FakeQdrant()

    async def fake_search(**kwargs):
        return [types.SimpleNamespace(message_id=allowed_id, score=0.77)]

    fake_qdrant.search = fake_search
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

    rid = "rid-allowed-domain"
    conversation_id = str(uuid.uuid4())
    r = await _post_retrieve_bundle(
        conversation_id=conversation_id,
        request_id=rid,
        body={
            "request_id": rid,
            "owner_id": "owner",
            "query": "hello",
            "allowed_memory_domains": ["technical"],
        },
    )
    assert r.status_code == 200
    body = r.json()
    assert len(body["bundle"]["semantic"]) == 1
    assert body["bundle"]["semantic"][0]["policy_metadata"]["memory_domains"] == ["technical"]
    assert body["bundle"]["retrieval_debug"]["tagged_records_evaluated"] == 1
    assert body["bundle"]["retrieval_debug"]["tagged_records_filtered"] == 0


@pytest.mark.asyncio
async def test_retrieve_bundle_blocks_explicitly_tagged_blocked_items(monkeypatch):
    blocked_id = str(uuid.uuid4())
    fake_pg = FakePG(
        message_metadata={
            blocked_id: {"memory_domain": "finance"},
        }
    )
    fake_qdrant = FakeQdrant()

    async def fake_search(**kwargs):
        return [types.SimpleNamespace(message_id=blocked_id, score=0.77)]

    fake_qdrant.search = fake_search
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

    rid = "rid-blocked-domain"
    conversation_id = str(uuid.uuid4())
    r = await _post_retrieve_bundle(
        conversation_id=conversation_id,
        request_id=rid,
        body={
            "request_id": rid,
            "owner_id": "owner",
            "query": "hello",
            "blocked_memory_domains": ["finance"],
        },
    )
    assert r.status_code == 200
    body = r.json()
    assert body["bundle"]["semantic"] == []
    assert body["bundle"]["retrieval_debug"]["tagged_records_evaluated"] == 1
    assert body["bundle"]["retrieval_debug"]["tagged_records_filtered"] == 1


@pytest.mark.asyncio
async def test_retrieve_bundle_reports_untagged_items_truthfully(monkeypatch):
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

    rid = "rid-untagged-domain"
    conversation_id = str(uuid.uuid4())
    r = await _post_retrieve_bundle(
        conversation_id=conversation_id,
        request_id=rid,
        body={
            "request_id": rid,
            "owner_id": "owner",
            "query": "hello",
            "allowed_memory_domains": ["technical"],
        },
    )
    assert r.status_code == 200
    body = r.json()
    assert len(body["bundle"]["semantic"]) == 1
    assert body["bundle"]["retrieval_debug"]["untagged_records_not_domain_enforced"] >= 1
    assert body["bundle"]["retrieval_debug"]["tagged_domain_enforcement_applied"] is False


@pytest.mark.asyncio
async def test_retrieve_bundle_returns_supersession_freshness_metadata(monkeypatch):
    semantic_source_id = "11111111-1111-4111-8111-111111111111"
    memory_id = "22222222-2222-4222-8222-222222222222"
    replacement_id = "33333333-3333-4333-8333-333333333333"
    fake_pg = FakePG(
        memory_items_by_ref={
            ("message", semantic_source_id): {
                "memory_id": memory_id,
                "status": "superseded",
                "last_reinforced_at": "2026-06-10T00:00:00+00:00",
                "updated_at": "2026-06-12T00:00:00+00:00",
                "confidence": 0.91,
                "supersedes_memory_id": "44444444-4444-4444-8444-444444444444",
                "superseded_by_memory_id": replacement_id,
            }
        }
    )
    fake_qdrant = FakeQdrant()

    async def fake_search(**kwargs):
        return [types.SimpleNamespace(message_id=semantic_source_id, score=0.77)]

    fake_qdrant.search = fake_search
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

    rid = "rid-freshness"
    conversation_id = str(uuid.uuid4())
    r = await _post_retrieve_bundle(
        conversation_id=conversation_id,
        request_id=rid,
        body={"request_id": rid, "owner_id": "owner", "query": "hello"},
    )
    assert r.status_code == 200
    body = r.json()
    semantic_item = body["bundle"]["semantic"][0]
    assert semantic_item["source_ref"]["ref_id"] == semantic_source_id
    assert semantic_item["memory_id"] == memory_id
    assert semantic_item["freshness_state"] == "superseded"
    assert semantic_item["last_verified_at"] == "2026-06-10T00:00:00+00:00"
    assert semantic_item["supersedes"] == "44444444-4444-4444-8444-444444444444"
    assert semantic_item["superseded_by"] == replacement_id
    assert semantic_item["confidence"] == 0.91


@pytest.mark.asyncio
async def test_retrieve_bundle_returns_artifact_memory_identity_from_same_selected_record(monkeypatch):
    derived_text_source_id = "55555555-5555-4555-8555-555555555555"
    memory_id = "66666666-6666-4666-8666-666666666666"
    supersedes_id = "77777777-7777-4777-8777-777777777777"
    fake_pg = FakePG(
        memory_items_by_ref={
            ("derived_text", derived_text_source_id): {
                "memory_id": memory_id,
                "status": "corrected",
                "last_reinforced_at": "2026-06-11T00:00:00+00:00",
                "updated_at": "2026-06-12T00:00:00+00:00",
                "confidence": 0.82,
                "supersedes_memory_id": supersedes_id,
                "superseded_by_memory_id": None,
            }
        }
    )
    fake_qdrant = FakeQdrant()

    async def fake_search_artifact_chunks(**kwargs):
        return [
            types.SimpleNamespace(
                derived_text_id=derived_text_source_id,
                artifact_id=str(uuid.uuid4()),
                file_path="api/helpers.py",
                repo_name="basic-memory-store",
                score=0.66,
            )
        ]

    fake_qdrant.search_artifact_chunks = fake_search_artifact_chunks
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

    rid = "rid-artifact-memory-id"
    conversation_id = str(uuid.uuid4())
    r = await _post_retrieve_bundle(
        conversation_id=conversation_id,
        request_id=rid,
        body={"request_id": rid, "owner_id": "owner", "query": "helper"},
    )
    assert r.status_code == 200
    body = r.json()
    artifact_item = body["bundle"]["artifact_refs"][0]
    assert artifact_item["source_ref"]["ref_id"] == derived_text_source_id
    assert artifact_item["memory_id"] == memory_id
    assert artifact_item["freshness_state"] == "corrected"
    assert artifact_item["last_verified_at"] == "2026-06-11T00:00:00+00:00"
    assert artifact_item["confidence"] == 0.82
    assert artifact_item["supersedes"] == supersedes_id
    assert artifact_item["superseded_by"] is None
    assert artifact_item["snippet"] == "def important_helper(): pass"
