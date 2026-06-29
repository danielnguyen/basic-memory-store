import types
import uuid

import httpx
import pytest

import main as main_module


class FakePG:
    def __init__(
        self,
        *,
        message_times=None,
        message_metadata=None,
        artifact_metadata=None,
        memory_items_by_ref=None,
        artifact_owner_by_id=None,
        source_lookup_fails=False,
        derived_text_lookup_fails=False,
        unique_derived_snippets=False,
    ):
        self.message_times = message_times or ["2026-01-01T00:00:00+00:00"]
        self.message_metadata = message_metadata or {}
        self.artifact_metadata = artifact_metadata or {}
        self.memory_items_by_ref = memory_items_by_ref or {}
        self.artifact_owner_by_id = artifact_owner_by_id or {}
        self.source_lookup_fails = source_lookup_fails
        self.derived_text_lookup_fails = derived_text_lookup_fails
        self.unique_derived_snippets = unique_derived_snippets
        self.last_conversation_id = None
        self.traces = {}

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

    async def get_message_owner(self, message_id):
        return "owner"

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
        if self.derived_text_lookup_fails and ids:
            raise RuntimeError("derived text store unavailable")
        def _text(idx):
            if self.unique_derived_snippets and idx > 0:
                return f"def important_helper_{idx}(): pass"
            return "def important_helper(): pass"

        def _file_path(idx):
            if self.unique_derived_snippets and idx > 0:
                return f"api/helpers_{idx}.py"
            return "api/helpers.py"

        return (
            [
                {
                    "derived_text_id": str(item),
                    "artifact_id": str(uuid.uuid4()),
                    "owner_id": "owner",
                    "kind": "chunk",
                    "text": _text(idx),
                    "derivation_params": self.artifact_metadata.get(str(item), {}),
                    "created_at": "2026-01-01T00:00:00+00:00",
                    "file_path": _file_path(idx),
                    "repo_name": "basic-memory-store",
                    "mime": "text/plain",
                }
                for idx, item in enumerate(ids)
            ]
            if ids
            else []
        )

    async def get_artifact(self, artifact_id):
        if self.source_lookup_fails:
            raise RuntimeError("source lookup unavailable")
        owner_id = self.artifact_owner_by_id.get(str(artifact_id), "owner")
        if owner_id is None:
            return None
        return {"artifact_id": str(artifact_id), "owner_id": owner_id}

    async def get_event_ingest_log(self, event_log_id):
        return None

    async def get_memory_debug(self, memory_id, owner_id):
        return None

    async def get_derived_text_for_owner(self, *, derived_text_id, owner_id):
        return None

    async def get_memory_items_for_source_refs(self, *, owner_id, source_refs):
        out = {}
        for ref in source_refs:
            key = (ref["ref_type"], ref["ref_id"])
            if key in self.memory_items_by_ref:
                out[key] = self.memory_items_by_ref[key]
        return out

    async def create_trace(self, trace):
        trace_id = str(uuid.uuid4())
        self.traces[trace["request_id"]] = {
            "trace_id": trace_id,
            **trace,
            "conversation_id": str(trace["conversation_id"]),
            "created_at": "2026-01-01T00:00:00+00:00",
        }
        return trace_id

    async def get_trace_by_request_id(self, request_id):
        return self.traces.get(request_id)


class FakeQdrant:
    def __init__(self, *, message_scores=None):
        self.message_scores = message_scores or [0.77]
        self.artifact_search_calls = []
        self.search_calls = []

    def ping(self):
        return True

    async def search(self, **kwargs):
        self.search_calls.append(kwargs)
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
    assert body["bundle"]["recent"][0]["owner_id"] == "owner"
    assert body["bundle"]["recent"][0]["evidence_role"] == "canonical"
    assert body["bundle"]["recent"][0]["source_availability"] == "not_applicable"
    assert body["bundle"]["recent"][0]["qualification_reasons"] == ["canonical_recent", "effective_unknown_freshness"]
    assert body["bundle"]["recent"][0]["memory_id"] is None
    assert body["bundle"]["recent"][0]["freshness_state"] == "unknown_freshness"
    assert body["bundle"]["semantic"][0]["content"] == "semantic result 0"
    assert body["bundle"]["semantic"][0]["evidence_role"] == "canonical"
    assert body["bundle"]["semantic"][0]["memory_id"] is None
    assert body["bundle"]["semantic"][0]["score"] >= 0.77
    assert body["bundle"]["semantic"][0]["score_details"]["semantic_score"] == 0.77
    assert body["bundle"]["semantic"][0]["source_ref"]["ref_type"] == "message"
    assert body["bundle"]["artifact_refs"][0]["file_path"] == "api/helpers.py"
    assert body["bundle"]["artifact_refs"][0]["owner_id"] == "owner"
    assert body["bundle"]["artifact_refs"][0]["evidence_role"] == "derived"
    assert body["bundle"]["artifact_refs"][0]["source_availability"] == "available"
    assert body["bundle"]["artifact_refs"][0]["source_checks"][0]["availability"] == "available"
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
    truth = body["bundle"]["retrieval_debug"]["truth_qualification"]
    assert truth["canonical_result_count"] == 2
    assert truth["derived_result_count"] == 1
    assert truth["source_available_count"] == 2
    assert truth["source_missing_count"] == 0
    assert truth["vector_retrieval_status"] == "ok"
    assert truth["derivative_retrieval_status"] == "ok"
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


@pytest.mark.asyncio
async def test_retrieve_bundle_omits_missing_derivative_source_but_keeps_canonical(monkeypatch):
    derived_text_id = "55555555-5555-4555-8555-555555555555"
    missing_artifact_id = "99999999-9999-4999-8999-999999999999"
    fake_pg = FakePG(
        artifact_metadata={
            derived_text_id: {
                "source_refs": [
                    {
                        "ref_type": "artifact",
                        "ref_id": missing_artifact_id,
                        "support_kind": "direct",
                    }
                ]
            }
        },
        artifact_owner_by_id={missing_artifact_id: None},
    )
    fake_qdrant = FakeQdrant()

    async def fake_search_artifact_chunks(**kwargs):
        return [types.SimpleNamespace(derived_text_id=derived_text_id, score=0.66)]

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

    rid = "rid-missing-source"
    conversation_id = str(uuid.uuid4())
    r = await _post_retrieve_bundle(
        conversation_id=conversation_id,
        request_id=rid,
        body={"request_id": rid, "owner_id": "owner", "query": "helper"},
    )
    assert r.status_code == 200
    body = r.json()
    assert len(body["bundle"]["recent"]) == 1
    assert len(body["bundle"]["semantic"]) == 1
    assert body["bundle"]["artifact_refs"] == []
    truth = body["bundle"]["retrieval_debug"]["truth_qualification"]
    assert truth["source_missing_count"] == 1
    assert truth["derivative_omissions_by_reason"] == {"missing_derivative_source_record": 1}


@pytest.mark.asyncio
async def test_retrieve_bundle_omits_cross_owner_derivative_source(monkeypatch):
    derived_text_id = "55555555-5555-4555-8555-555555555556"
    artifact_id = "99999999-9999-4999-8999-999999999998"
    fake_pg = FakePG(
        artifact_metadata={
            derived_text_id: {
                "source_refs": [
                    {"ref_type": "artifact", "ref_id": artifact_id, "support_kind": "direct"}
                ]
            }
        },
        artifact_owner_by_id={artifact_id: "other-owner"},
    )
    fake_qdrant = FakeQdrant()

    async def fake_search_artifact_chunks(**kwargs):
        return [types.SimpleNamespace(derived_text_id=derived_text_id, score=0.66)]

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

    rid = "rid-cross-source"
    conversation_id = str(uuid.uuid4())
    r = await _post_retrieve_bundle(
        conversation_id=conversation_id,
        request_id=rid,
        body={"request_id": rid, "owner_id": "owner", "query": "helper"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["bundle"]["artifact_refs"] == []
    truth = body["bundle"]["retrieval_debug"]["truth_qualification"]
    assert truth["source_owner_mismatch_count"] == 1
    assert truth["derivative_omissions_by_reason"] == {"cross_owner_derivative_source_ref": 1}


@pytest.mark.asyncio
async def test_retrieve_bundle_omits_derivative_when_source_lookup_fails(monkeypatch):
    derived_text_id = "55555555-5555-4555-8555-555555555557"
    artifact_id = "99999999-9999-4999-8999-999999999997"
    fake_pg = FakePG(
        artifact_metadata={
            derived_text_id: {
                "source_refs": [
                    {"ref_type": "artifact", "ref_id": artifact_id, "support_kind": "direct"}
                ]
            }
        },
        source_lookup_fails=True,
    )
    fake_qdrant = FakeQdrant()

    async def fake_search_artifact_chunks(**kwargs):
        return [types.SimpleNamespace(derived_text_id=derived_text_id, score=0.66)]

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

    rid = "rid-source-unavailable"
    conversation_id = str(uuid.uuid4())
    r = await _post_retrieve_bundle(
        conversation_id=conversation_id,
        request_id=rid,
        body={"request_id": rid, "owner_id": "owner", "query": "helper"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["bundle"]["recent"][0]["evidence_role"] == "canonical"
    assert body["bundle"]["artifact_refs"] == []
    truth = body["bundle"]["retrieval_debug"]["truth_qualification"]
    assert truth["source_unavailable_count"] == 1
    assert truth["derivative_omissions_by_reason"] == {"derivative_source_lookup_unavailable": 1}


@pytest.mark.asyncio
async def test_retrieve_bundle_degrades_lifecycle_restricted_derivative_without_rewriting_canonical(monkeypatch):
    derived_text_source_id = "55555555-5555-4555-8555-555555555558"
    fake_pg = FakePG(
        memory_items_by_ref={
            ("derived_text", derived_text_source_id): {
                "memory_id": "66666666-6666-4666-8666-666666666668",
                "status": "rebuilding",
                "last_reinforced_at": None,
                "updated_at": "2026-06-12T00:00:00+00:00",
                "confidence": 0.41,
                "supersedes_memory_id": None,
                "superseded_by_memory_id": None,
            }
        }
    )
    fake_qdrant = FakeQdrant()

    async def fake_search_artifact_chunks(**kwargs):
        return [types.SimpleNamespace(derived_text_id=derived_text_source_id, score=0.66)]

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

    rid = "rid-lifecycle-degraded"
    conversation_id = str(uuid.uuid4())
    r = await _post_retrieve_bundle(
        conversation_id=conversation_id,
        request_id=rid,
        body={"request_id": rid, "owner_id": "owner", "query": "helper"},
    )
    assert r.status_code == 200
    body = r.json()
    artifact = body["bundle"]["artifact_refs"][0]
    assert body["bundle"]["recent"][0]["evidence_role"] == "canonical"
    assert artifact["evidence_role"] == "derived"
    assert artifact["source_availability"] == "available"
    assert artifact["durable_status"] == "rebuilding"
    assert artifact["freshness_state"] == "unknown_freshness"
    assert artifact["confidence"] == 0.41
    assert artifact["qualification_reasons"] == [
        "effective_unknown_freshness",
        "durable_rebuilding",
    ]
    truth = body["bundle"]["retrieval_debug"]["truth_qualification"]
    assert truth["derived_degraded_count"] == 1
    assert truth["lifecycle_restricted_derived_count"] == 1


@pytest.mark.asyncio
async def test_vector_and_artifact_failure_preserve_canonical_recent_without_fabricating_semantic(monkeypatch):
    fake_pg = FakePG()
    fake_qdrant = FakeQdrant()

    async def failed_search(**kwargs):
        raise RuntimeError("vector unavailable")

    async def failed_artifact_search(**kwargs):
        raise RuntimeError("artifact unavailable")

    fake_qdrant.search = failed_search
    fake_qdrant.search_artifact_chunks = failed_artifact_search
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

    rid = "rid-fallbacks"
    conversation_id = str(uuid.uuid4())
    r = await _post_retrieve_bundle(
        conversation_id=conversation_id,
        request_id=rid,
        body={"request_id": rid, "owner_id": "owner", "query": "helper"},
    )
    assert r.status_code == 200
    body = r.json()
    assert len(body["bundle"]["recent"]) == 1
    assert body["bundle"]["semantic"] == []
    assert body["bundle"]["artifact_refs"] == []
    debug = body["bundle"]["retrieval_debug"]
    assert debug["vector_status"] == "unavailable"
    assert debug["artifact_status"] == "unavailable"
    assert debug["truth_qualification"]["canonical_result_count"] == 1
    assert debug["truth_qualification"]["derived_result_count"] == 0
    assert debug["truth_qualification"]["canonical_fallback_reasons"] == ["vector_unavailable"]


@pytest.mark.asyncio
async def test_raw_mode_excludes_derivative_assistance_through_api(monkeypatch):
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
        enable_trace_storage=True,
    )
    monkeypatch.setattr(main_module, "settings", fake_settings, raising=True)
    monkeypatch.setattr(main_module, "pg", fake_pg, raising=True)
    monkeypatch.setattr(main_module, "qdrant", fake_qdrant, raising=True)

    rid = "rid-raw-mode"
    conversation_id = str(uuid.uuid4())
    r = await _post_retrieve_bundle(
        conversation_id=conversation_id,
        request_id=rid,
        body={"request_id": rid, "owner_id": "owner", "query": "helper", "mode": "raw"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["bundle"]["artifact_refs"] == []
    assert fake_qdrant.artifact_search_calls == []
    assert body["bundle"]["retrieval_debug"]["retrieval_contract_mode"] == "raw"
    assert body["bundle"]["retrieval_debug"]["artifacts_included"] is False
    assert body["diagnostics"]["mode"] == "raw"
    assert body["diagnostics"]["canonical_used"] is True
    assert body["diagnostics"]["derived_used"] is False
    assert "derivative_augmentation_used" not in body["diagnostics"]["reason_codes"]

    trace = await fake_pg.get_trace_by_request_id(rid)
    assert trace["retrieval"]["mode"] == "raw"
    assert trace["retrieval"]["owner_id"] == "owner"
    assert trace["retrieval"]["request_id"] == rid


@pytest.mark.asyncio
async def test_compare_mode_runs_raw_and_augmented_from_same_normalized_request(monkeypatch):
    semantic_id = "11111111-1111-4111-8111-111111111111"
    derived_text_id = "55555555-5555-4555-8555-555555555555"
    private_query = " PRIVATE-QUERY-SENTINEL-2E "
    fake_pg = FakePG()
    fake_qdrant = FakeQdrant()

    async def fixed_search(**kwargs):
        fake_qdrant.search_calls.append(kwargs)
        return [types.SimpleNamespace(message_id=semantic_id, score=0.77)]

    async def fixed_artifact_search(**kwargs):
        fake_qdrant.artifact_search_calls.append(kwargs)
        return [types.SimpleNamespace(derived_text_id=derived_text_id, score=0.66)]

    fake_qdrant.search = fixed_search
    fake_qdrant.search_artifact_chunks = fixed_artifact_search
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
        enable_trace_storage=True,
    )
    monkeypatch.setattr(main_module, "settings", fake_settings, raising=True)
    monkeypatch.setattr(main_module, "pg", fake_pg, raising=True)
    monkeypatch.setattr(main_module, "qdrant", fake_qdrant, raising=True)

    rid = "rid-compare-mode"
    conversation_id = str(uuid.uuid4())
    r = await _post_retrieve_bundle(
        conversation_id=conversation_id,
        request_id=rid,
        body={"request_id": rid, "owner_id": "owner", "query": private_query, "mode": "compare"},
    )
    assert r.status_code == 200
    body = r.json()
    assert len(fake_qdrant.search_calls) == 2
    assert {call["query"] for call in fake_qdrant.search_calls} == {"PRIVATE-QUERY-SENTINEL-2E"}
    assert body["raw_bundle"]["artifact_refs"] == []
    assert len(body["augmented_bundle"]["artifact_refs"]) == 1
    assert body["comparison"]["mode"] == "compare"
    assert body["comparison"]["shared_normalized_input"] is True
    assert body["comparison"]["normalization_applied"] is True
    assert body["comparison"]["raw_order"] == [semantic_id]
    assert body["comparison"]["augmented_order"] == [semantic_id]
    assert body["comparison"]["artifact_delta"] == 1
    assert body["comparison"]["added"][0]["result_type"] == "artifact"
    assert body["diagnostics"]["mode"] == "compare"
    assert "compare_mode_completed" in body["diagnostics"]["reason_codes"]
    serialized_diagnostics = str(body["diagnostics"]) + str(body["comparison"])
    assert "PRIVATE-QUERY-SENTINEL-2E" not in serialized_diagnostics
    assert "semantic result" not in serialized_diagnostics
    assert "def important_helper" not in serialized_diagnostics

    trace = await fake_pg.get_trace_by_request_id(rid)
    assert trace["retrieval"]["comparison"]["artifact_delta"] == 1
    assert trace["retrieval"]["raw_result_ids"] == [semantic_id]
    assert "PRIVATE-QUERY-SENTINEL-2E" not in str(trace["retrieval"])


@pytest.mark.asyncio
async def test_compare_augmented_failure_preserves_raw_bundle_and_trace(monkeypatch):
    semantic_id = "11111111-1111-4111-8111-111111111112"
    fake_pg = FakePG(derived_text_lookup_fails=True)
    fake_qdrant = FakeQdrant()

    async def fixed_search(**kwargs):
        fake_qdrant.search_calls.append(kwargs)
        return [types.SimpleNamespace(message_id=semantic_id, score=0.77)]

    fake_qdrant.search = fixed_search
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
        enable_trace_storage=True,
    )
    monkeypatch.setattr(main_module, "settings", fake_settings, raising=True)
    monkeypatch.setattr(main_module, "pg", fake_pg, raising=True)
    monkeypatch.setattr(main_module, "qdrant", fake_qdrant, raising=True)

    rid = "rid-augmented-failure"
    conversation_id = str(uuid.uuid4())
    r = await _post_retrieve_bundle(
        conversation_id=conversation_id,
        request_id=rid,
        body={"request_id": rid, "owner_id": "owner", "query": "helper", "mode": "compare"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["raw_bundle"]["semantic"][0]["message_id"] == semantic_id
    assert body["bundle"] == body["raw_bundle"]
    assert body["augmented_bundle"] is None
    assert body["diagnostics"]["status"] == "degraded"
    assert body["diagnostics"]["fallback_to_raw"] is True
    assert body["diagnostics"]["fallback_reasons"] == ["augmented_retrieval_failed"]
    assert "canonical_retrieval_failed" not in str(body["diagnostics"])

    trace = await fake_pg.get_trace_by_request_id(rid)
    assert trace["status"] == "degraded"
    assert trace["retrieval"]["fallback_to_raw"] is True
    assert trace["retrieval"]["fallback_reasons"] == ["augmented_retrieval_failed"]
    assert "canonical_retrieval_failed" not in str(trace["retrieval"])


@pytest.mark.asyncio
async def test_doctrine_diagnostics_distinguish_supported_lifecycle_states(monkeypatch):
    derived_ids = [
        "55555555-5555-4555-8555-555555555551",
        "55555555-5555-4555-8555-555555555552",
        "55555555-5555-4555-8555-555555555553",
        "55555555-5555-4555-8555-555555555554",
        "55555555-5555-4555-8555-555555555555",
    ]
    statuses = ["stale", "contradicted", "superseded", "retracted", "rebuilding"]
    fake_pg = FakePG(
        unique_derived_snippets=True,
        memory_items_by_ref={
            ("derived_text", derived_id): {
                "memory_id": str(uuid.uuid4()),
                "status": status,
                "last_reinforced_at": None,
                "updated_at": "2026-06-12T00:00:00+00:00",
                "confidence": 0.4,
                "supersedes_memory_id": None,
                "superseded_by_memory_id": (
                    str(uuid.uuid4()) if status == "superseded" else None
                ),
            }
            for derived_id, status in zip(derived_ids, statuses)
        }
    )
    fake_qdrant = FakeQdrant()

    async def fixed_artifact_search(**kwargs):
        fake_qdrant.artifact_search_calls.append(kwargs)
        return [
            types.SimpleNamespace(derived_text_id=derived_id, score=0.7 - idx * 0.01)
            for idx, derived_id in enumerate(derived_ids)
        ]

    fake_qdrant.search_artifact_chunks = fixed_artifact_search
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
        retrieval_artifact_k=5,
        recent_turns=10,
        enable_trace_storage=True,
    )
    monkeypatch.setattr(main_module, "settings", fake_settings, raising=True)
    monkeypatch.setattr(main_module, "pg", fake_pg, raising=True)
    monkeypatch.setattr(main_module, "qdrant", fake_qdrant, raising=True)

    rid = "rid-state-diagnostics"
    conversation_id = str(uuid.uuid4())
    r = await _post_retrieve_bundle(
        conversation_id=conversation_id,
        request_id=rid,
        body={"request_id": rid, "owner_id": "owner", "query": "states", "mode": "compare"},
    )
    assert r.status_code == 200
    body = r.json()
    state_counts = body["diagnostics"]["validation"]["derivative_state_counts"]
    assert state_counts == {
        "stale": 1,
        "contradicted": 1,
        "superseded": 1,
        "retracted": 1,
        "unsupported_validation_state": 1,
    }
    assert {
        "derivative_stale",
        "derivative_contradicted",
        "derivative_superseded",
        "derivative_retracted",
        "derivative_unsupported_validation_state",
    } <= set(body["diagnostics"]["reason_codes"])
    trace = await fake_pg.get_trace_by_request_id(rid)
    assert trace["retrieval"]["validation"]["derivative_state_counts"] == state_counts


@pytest.mark.asyncio
async def test_retrieve_mode_rejects_unknown_and_identity_mismatch(monkeypatch):
    fake_pg = FakePG()
    fake_settings = types.SimpleNamespace(
        memory_api_key="testkey",
        require_request_id=True,
        enforce_request_id_header_body_match=True,
        retrieval_k=8,
        recent_turns=10,
    )
    monkeypatch.setattr(main_module, "settings", fake_settings, raising=True)
    monkeypatch.setattr(main_module, "pg", fake_pg, raising=True)
    monkeypatch.setattr(main_module, "qdrant", FakeQdrant(), raising=True)

    rid = "rid-invalid-mode"
    conversation_id = str(uuid.uuid4())
    invalid = await _post_retrieve_bundle(
        conversation_id=conversation_id,
        request_id=rid,
        body={"request_id": rid, "owner_id": "owner", "query": "hello", "mode": "debug"},
    )
    assert invalid.status_code == 422

    mismatch = await _post_retrieve_bundle(
        conversation_id=conversation_id,
        request_id="rid-owner-mismatch",
        body={
            "request_id": "rid-owner-mismatch",
            "owner_id": "other-owner",
            "query": "hello",
            "mode": "raw",
        },
    )
    assert mismatch.status_code == 404


@pytest.mark.asyncio
async def test_canonical_retrieval_failure_is_bounded_explicit_failure(monkeypatch):
    class FailingCanonicalPG(FakePG):
        async def get_recent_message_items(self, conversation_id, limit):
            raise RuntimeError("canonical store unavailable")

    fake_pg = FailingCanonicalPG()
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
        enable_trace_storage=True,
    )
    monkeypatch.setattr(main_module, "settings", fake_settings, raising=True)
    monkeypatch.setattr(main_module, "pg", fake_pg, raising=True)
    monkeypatch.setattr(main_module, "qdrant", FakeQdrant(), raising=True)

    rid = "rid-canonical-failure"
    conversation_id = str(uuid.uuid4())
    r = await _post_retrieve_bundle(
        conversation_id=conversation_id,
        request_id=rid,
        body={"request_id": rid, "owner_id": "owner", "query": "hello", "mode": "raw"},
    )
    assert r.status_code == 503
    assert r.json()["detail"] == {
        "error": "canonical_retrieval_failed",
        "request_id": rid,
        "mode": "raw",
    }
    trace = await fake_pg.get_trace_by_request_id(rid)
    assert trace["status"] == "failed"
    assert trace["retrieval"]["status"] == "failed"
    assert "fallback_to_raw" not in trace["retrieval"]["reason_codes"]
