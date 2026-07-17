from datetime import UTC, datetime, timedelta
import types
import uuid

import httpx
import pytest

import main as main_module
from models import ArtifactInitRequest, MessageCreateRequest, RetrievalRecordPolicyMetadata


class FakePG:
    def __init__(
        self,
        *,
        message_times=None,
        message_metadata=None,
        message_policy_metadata=None,
        artifact_metadata=None,
        artifact_rows_by_id=None,
        memory_items_by_ref=None,
        artifact_owner_by_id=None,
        artifact_lookup_fail_ids=None,
        source_lookup_fails=False,
        derived_text_lookup_fails=False,
        unique_derived_snippets=False,
    ):
        self.message_times = message_times or ["2026-01-01T00:00:00+00:00"]
        self.message_metadata = message_metadata or {}
        self.message_policy_metadata = message_policy_metadata or {}
        self.artifact_metadata = artifact_metadata or {}
        self.artifact_rows_by_id = artifact_rows_by_id or {}
        self.memory_items_by_ref = memory_items_by_ref or {}
        self.artifact_owner_by_id = artifact_owner_by_id or {}
        self.artifact_lookup_fail_ids = set(artifact_lookup_fail_ids or [])
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
                    "policy_metadata": self.message_policy_metadata.get(key),
                    "created_at": self.message_times[min(idx, len(self.message_times) - 1)],
                }
            )
        return out

    async def get_message_owner(self, message_id):
        return "owner"

    async def get_recent_message_items(self, conversation_id, limit, policy_filter=None):
        self.last_conversation_id = str(conversation_id)
        if policy_filter is not None:
            return []
        return [
            {
                "message_id": str(uuid.uuid4()),
                "conversation_id": str(conversation_id),
                "role": "user",
                "content": "recent snippet",
                "metadata": {},
                "policy_metadata": None,
                "created_at": "2026-01-01T00:00:00+00:00",
            }
        ]

    async def get_derived_text_snippets_by_ids(self, ids):
        if self.derived_text_lookup_fails and ids:
            raise RuntimeError("derived text store unavailable")
        def _trusted_policy(raw):
            if not isinstance(raw, dict):
                return None
            policy = {
                key: raw[key]
                for key in (
                    "memory_domains",
                    "sensitivity",
                    "content_class",
                    "entity_ids",
                    "relationship_ids",
                    "relationship_scopes",
                )
                if key in raw
            }
            return policy or None

        def _text(idx):
            if self.unique_derived_snippets and idx > 0:
                return f"def important_helper_{idx}(): pass"
            return "def important_helper(): pass"

        def _file_path(idx):
            if self.unique_derived_snippets and idx > 0:
                return f"api/helpers_{idx}.py"
            return "api/helpers.py"

        out = []
        for idx, item in enumerate(ids):
            key = str(item)
            base = {
                "derived_text_id": str(item),
                "artifact_id": str(uuid.uuid4()),
                "owner_id": "owner",
                "kind": "chunk",
                "text": _text(idx),
                "derivation_params": self.artifact_metadata.get(key, {}),
                "policy_metadata": _trusted_policy(self.artifact_metadata.get(key)),
                "created_at": "2026-01-01T00:00:00+00:00",
                "file_path": _file_path(idx),
                "repo_name": "basic-memory-store",
                "mime": "text/plain",
            }
            base.update(self.artifact_rows_by_id.get(key, {}))
            out.append(base)
        return out

    async def get_artifact(self, artifact_id):
        if self.source_lookup_fails or str(artifact_id) in self.artifact_lookup_fail_ids:
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
    def __init__(self, *, message_scores=None, message_ids=None, artifact_ids=None, artifact_scores=None):
        self.message_scores = message_scores or [0.77]
        self.message_ids = message_ids
        self.artifact_ids = artifact_ids
        self.artifact_scores = artifact_scores
        self.artifact_search_calls = []
        self.search_calls = []

    def ping(self):
        return True

    async def search(self, **kwargs):
        self.search_calls.append(kwargs)
        ids = self.message_ids or [str(uuid.uuid4()) for _ in self.message_scores]
        return [
            types.SimpleNamespace(message_id=ids[idx], score=score)
            for idx, score in enumerate(self.message_scores)
        ]

    async def search_artifact_chunks(self, **kwargs):
        self.artifact_search_calls.append(kwargs)
        scores = self.artifact_scores or [0.66, 0.61]
        ids = self.artifact_ids or [str(uuid.uuid4()) for _ in scores]
        return [
            types.SimpleNamespace(
                derived_text_id=derived_id,
                artifact_id=str(uuid.uuid4()),
                file_path=f"api/helpers_{idx}.py" if idx else "api/helpers.py",
                repo_name="basic-memory-store",
                score=scores[min(idx, len(scores) - 1)],
            )
            for idx, derived_id in enumerate(ids)
        ]


def _mandatory_policy(*, domains=None, blocked=None, relationship=None, artifact_classes=None, surface_classes=None):
    domains = domains if domains is not None else ["technical"]
    return {
        "enforcement_mode": "mandatory",
        "allowed_memory_domains": domains,
        "blocked_memory_domains": blocked or [],
        "artifact_access_policy": {
            "enforcement_mode": "mandatory",
            "allowed_content_classes": artifact_classes or ["document", "code"],
            "allowed_domains": domains,
            "maximum_sensitivity": "medium",
            "surface_content_capabilities": surface_classes or ["document", "code"],
            "reason_codes": ["artifact_policy_applied"],
        },
        "relationship_scope_projection": relationship,
    }


def _record_policy(*, domains=None, sensitivity="low", content_class=None, entity_ids=None, relationship_ids=None, scopes=None):
    payload = {
        "memory_domains": domains if domains is not None else ["technical"],
        "sensitivity": sensitivity,
        "entity_ids": entity_ids or [],
        "relationship_ids": relationship_ids or [],
        "relationship_scopes": scopes or [],
    }
    if content_class is not None:
        payload["content_class"] = content_class
    return payload


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
    now = datetime.now(UTC)
    inside_window = (now - timedelta(days=7)).isoformat()
    outside_window = (now - timedelta(days=60)).isoformat()
    fake_pg = FakePG(
        message_times=[
            inside_window,
            outside_window,
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
async def test_artifact_source_validity_filter_does_not_let_invalid_hits_consume_final_slots(monkeypatch):
    invalid_ids = [str(uuid.uuid4()) for _ in range(3)]
    valid_id = str(uuid.uuid4())
    missing_source_ids = [str(uuid.uuid4()) for _ in invalid_ids]
    valid_source_id = str(uuid.uuid4())
    artifact_metadata = {
        derived_id: {
            "source_refs": [
                {"ref_type": "artifact", "ref_id": source_id, "support_kind": "direct"}
            ]
        }
        for derived_id, source_id in zip(invalid_ids, missing_source_ids)
    }
    artifact_metadata[valid_id] = {
        "source_refs": [
            {"ref_type": "artifact", "ref_id": valid_source_id, "support_kind": "direct"}
        ]
    }
    fake_pg = FakePG(
        artifact_metadata=artifact_metadata,
        artifact_owner_by_id={
            **{source_id: None for source_id in missing_source_ids},
            valid_source_id: "owner",
        },
        unique_derived_snippets=True,
    )
    fake_qdrant = FakeQdrant()

    async def fake_search(**kwargs):
        return []

    async def fake_search_artifact_chunks(**kwargs):
        scores = [0.99, 0.98, 0.97, 0.2]
        return [
            types.SimpleNamespace(
                derived_text_id=derived_id,
                artifact_id=str(uuid.uuid4()),
                file_path=f"api/helpers_{idx}.py",
                repo_name="basic-memory-store",
                score=scores[idx],
            )
            for idx, derived_id in enumerate([*invalid_ids, valid_id])
        ]

    fake_qdrant.search = fake_search
    fake_qdrant.search_artifact_chunks = fake_search_artifact_chunks
    fake_settings = types.SimpleNamespace(
        memory_api_key="testkey",
        require_request_id=True,
        enforce_request_id_header_body_match=True,
        retrieval_k=8,
        retrieval_artifact_k=1,
        retrieval_artifact_max_snippet_chars=500,
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

    rid = "rid-artifact-source-final-slot"
    r = await _post_retrieve_bundle(
        conversation_id=str(uuid.uuid4()),
        request_id=rid,
        body={
            "request_id": rid,
            "owner_id": "owner",
            "query": "helper",
            "include_artifacts": True,
            "retrieval": {"k": 3, "min_score": 0.0, "scope": "conversation"},
        },
    )

    assert r.status_code == 200
    body = r.json()
    refs = body["bundle"]["artifact_refs"]
    assert len(refs) == 1
    assert refs[0]["source_ref"]["ref_id"] == valid_id
    truth = body["bundle"]["retrieval_debug"]["truth_qualification"]
    assert truth["source_missing_count"] == 3
    assert truth["derivative_omissions_by_reason"] == {"missing_derivative_source_record": 3}


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
        async def get_recent_message_items(self, conversation_id, limit, policy_filter=None):
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


@pytest.mark.asyncio
async def test_mandatory_containment_excludes_untagged_and_uses_qdrant_policy_filter(monkeypatch):
    ineligible_id = str(uuid.uuid4())
    eligible_id = str(uuid.uuid4())
    fake_pg = FakePG(
        message_policy_metadata={
            eligible_id: _record_policy(domains=["technical"], sensitivity="low"),
        }
    )
    fake_qdrant = FakeQdrant(message_ids=[ineligible_id, eligible_id], message_scores=[0.99, 0.2])
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

    rid = "rid-mandatory-filter"
    conversation_id = str(uuid.uuid4())
    r = await _post_retrieve_bundle(
        conversation_id=conversation_id,
        request_id=rid,
        body={
            "request_id": rid,
            "owner_id": "owner",
            "query": "hello",
            "include_artifacts": False,
            "containment_policy": _mandatory_policy(domains=["technical"]),
        },
    )

    assert r.status_code == 200
    body = r.json()
    assert [item["message_id"] for item in body["bundle"]["semantic"]] == [eligible_id]
    qdrant_filter = fake_qdrant.search_calls[0]["policy_filter"]
    assert qdrant_filter["allowed_domains"] == ["technical"]
    assert qdrant_filter["allowed_sensitivities"] == ["low", "medium", "high"]
    containment = body["bundle"]["retrieval_debug"]["containment_policy"]
    assert containment["enforcement_mode"] == "mandatory"
    assert containment["pre_limit_policy_filter_applied"] is True
    assert containment["omitted_counts_by_reason"]["missing_policy_metadata"] == 1
    assert "mandatory_containment_applied" in body["diagnostics"]["reason_codes"]


@pytest.mark.asyncio
async def test_mandatory_containment_rejects_legacy_freeform_spoof(monkeypatch):
    spoof_id = str(uuid.uuid4())
    eligible_id = str(uuid.uuid4())
    fake_pg = FakePG(
        message_metadata={
            spoof_id: {"retrieval_policy_metadata": _record_policy(domains=["technical"], sensitivity="low")},
        },
        message_policy_metadata={
            eligible_id: _record_policy(domains=["technical"], sensitivity="low"),
        },
    )
    fake_qdrant = FakeQdrant(message_ids=[spoof_id, eligible_id], message_scores=[0.99, 0.2])
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

    rid = "rid-legacy-spoof"
    r = await _post_retrieve_bundle(
        conversation_id=str(uuid.uuid4()),
        request_id=rid,
        body={
            "request_id": rid,
            "owner_id": "owner",
            "query": "hello",
            "include_artifacts": False,
            "containment_policy": _mandatory_policy(domains=["technical"]),
        },
    )

    assert r.status_code == 200
    body = r.json()
    assert [item["message_id"] for item in body["bundle"]["semantic"]] == [eligible_id]
    omitted = body["bundle"]["retrieval_debug"]["containment_policy"]["omitted_counts_by_reason"]
    assert omitted["missing_policy_metadata"] == 1


@pytest.mark.asyncio
async def test_mandatory_containment_rejects_malformed_policy_shapes(monkeypatch):
    string_domain_id = str(uuid.uuid4())
    mixed_array_id = str(uuid.uuid4())
    eligible_id = str(uuid.uuid4())
    fake_pg = FakePG(
        message_policy_metadata={
            string_domain_id: {"memory_domains": "technical", "sensitivity": "low"},
            mixed_array_id: {"memory_domains": ["technical", 7], "sensitivity": "low"},
            eligible_id: _record_policy(domains=["technical"], sensitivity="low"),
        }
    )
    fake_qdrant = FakeQdrant(
        message_ids=[string_domain_id, mixed_array_id, eligible_id],
        message_scores=[0.99, 0.98, 0.2],
    )
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

    rid = "rid-malformed-policy"
    r = await _post_retrieve_bundle(
        conversation_id=str(uuid.uuid4()),
        request_id=rid,
        body={
            "request_id": rid,
            "owner_id": "owner",
            "query": "hello",
            "include_artifacts": False,
            "containment_policy": _mandatory_policy(domains=["technical"]),
        },
    )

    assert r.status_code == 200
    body = r.json()
    assert [item["message_id"] for item in body["bundle"]["semantic"]] == [eligible_id]
    omitted = body["bundle"]["retrieval_debug"]["containment_policy"]["omitted_counts_by_reason"]
    assert omitted["malformed_policy_metadata"] == 2


@pytest.mark.asyncio
async def test_retrieve_bundle_filters_message_candidates_before_limit(monkeypatch):
    eligible_id = str(uuid.uuid4())
    blocked_id = str(uuid.uuid4())
    outside_id = str(uuid.uuid4())
    spoofed_id = str(uuid.uuid4())
    untagged_id = str(uuid.uuid4())
    extra_outside_ids = [str(uuid.uuid4()) for _ in range(4)]
    fake_pg = FakePG(
        message_metadata={
            spoofed_id: {"retrieval_policy_metadata": _record_policy(domains=["technical"])},
        },
        message_policy_metadata={
            eligible_id: _record_policy(domains=["technical"], sensitivity="low"),
            blocked_id: _record_policy(domains=["technical", "finance"], sensitivity="low"),
            outside_id: _record_policy(domains=["personal"], sensitivity="low"),
            **{
                item_id: _record_policy(domains=["personal"], sensitivity="low")
                for item_id in extra_outside_ids
            },
        },
    )
    ordered_ids = [
        blocked_id,
        outside_id,
        spoofed_id,
        untagged_id,
        *extra_outside_ids,
        eligible_id,
    ]
    fake_qdrant = FakeQdrant(
        message_ids=ordered_ids,
        message_scores=[0.99, 0.98, 0.97, 0.96, 0.95, 0.94, 0.93, 0.92, 0.2],
    )
    fake_settings = types.SimpleNamespace(
        memory_api_key="testkey",
        require_request_id=True,
        enforce_request_id_header_body_match=True,
        retrieval_k=1,
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

    rid = "rid-message-containment-crowding"
    r = await _post_retrieve_bundle(
        conversation_id=str(uuid.uuid4()),
        request_id=rid,
        body={
            "request_id": rid,
            "owner_id": "owner",
            "query": "eligible-memory-note",
            "include_artifacts": False,
            "containment_policy": _mandatory_policy(domains=["technical"], blocked=["finance"]),
        },
    )

    assert r.status_code == 200
    body = r.json()
    assert [item["message_id"] for item in body["bundle"]["semantic"]] == [eligible_id]
    returned_ids = {item["message_id"] for item in body["bundle"]["semantic"]}
    assert returned_ids.isdisjoint({blocked_id, outside_id, spoofed_id, untagged_id, *extra_outside_ids})
    assert fake_qdrant.search_calls[0]["k"] == 1
    assert fake_qdrant.search_calls[0]["policy_filter"]["allowed_domains"] == ["technical"]
    containment = body["bundle"]["retrieval_debug"]["containment_policy"]
    assert containment["pre_limit_policy_filter_applied"] is True
    assert containment["post_fetch_validation_count"] == len(ordered_ids)
    assert containment["retained_count"] == 1
    omitted = containment["omitted_counts_by_reason"]
    assert omitted["blocked_domain"] == 1
    assert omitted["outside_allowed_domain"] == 1 + len(extra_outside_ids)
    assert omitted["missing_policy_metadata"] == 2
    assert "mandatory_containment_applied" in body["diagnostics"]["reason_codes"]


@pytest.mark.asyncio
async def test_retrieve_bundle_filters_artifact_candidates_before_limit(monkeypatch):
    selected_rel = str(uuid.uuid4())

    def artifact_params(source_id, *, domains=None, sensitivity="low", content_class="code", relationship=True):
        return {
            **_record_policy(
                domains=domains,
                sensitivity=sensitivity,
                content_class=content_class,
                relationship_ids=[selected_rel] if relationship else [],
                scopes=["project"] if relationship else [],
            ),
            "source_refs": [{"ref_type": "artifact", "ref_id": source_id, "support_kind": "direct"}],
            "derivation_type": "chunk",
            "derivation_version": "file-chunk-v1",
            "status": "active",
            "confidence": 0.9,
            "generation_trace_id": "rid-artifact-containment-crowding",
        }

    derived_ids = {
        name: str(uuid.uuid4())
        for name in (
            "blocked-domain-decoy",
            "outside-domain-decoy",
            "sensitive-artifact-decoy",
            "unsupported-class-decoy",
            "malformed-policy-decoy",
            "incomplete-lifecycle-decoy",
            "unavailable-source-decoy",
            "irrelevant-record-decoy",
            "eligible-code-artifact",
            "eligible-document-artifact",
        )
    }
    source_ids = {name: str(uuid.uuid4()) for name in derived_ids}
    artifact_metadata = {
        derived_ids["blocked-domain-decoy"]: artifact_params(
            source_ids["blocked-domain-decoy"],
            domains=["technical", "finance"],
        ),
        derived_ids["outside-domain-decoy"]: artifact_params(
            source_ids["outside-domain-decoy"],
            domains=["personal"],
        ),
        derived_ids["sensitive-artifact-decoy"]: artifact_params(
            source_ids["sensitive-artifact-decoy"],
            sensitivity="high",
        ),
        derived_ids["unsupported-class-decoy"]: artifact_params(
            source_ids["unsupported-class-decoy"],
            content_class="image",
        ),
        derived_ids["malformed-policy-decoy"]: {
            **artifact_params(source_ids["malformed-policy-decoy"]),
            "memory_domains": "technical",
        },
        derived_ids["incomplete-lifecycle-decoy"]: {
            **_record_policy(
                domains=["technical"],
                sensitivity="low",
                content_class="code",
                relationship_ids=[selected_rel],
                scopes=["project"],
            ),
            "derivation_type": "chunk",
            "derivation_version": "file-chunk-v1",
            "status": "active",
        },
        derived_ids["unavailable-source-decoy"]: artifact_params(
            source_ids["unavailable-source-decoy"],
        ),
        derived_ids["irrelevant-record-decoy"]: artifact_params(
            source_ids["irrelevant-record-decoy"],
            relationship=False,
        ),
        derived_ids["eligible-code-artifact"]: artifact_params(
            source_ids["eligible-code-artifact"],
            content_class="code",
        ),
        derived_ids["eligible-document-artifact"]: artifact_params(
            source_ids["eligible-document-artifact"],
            content_class="document",
        ),
    }
    fake_pg = FakePG(
        artifact_metadata=artifact_metadata,
        artifact_rows_by_id={
            derived_ids["incomplete-lifecycle-decoy"]: {"artifact_id": None},
        },
        artifact_owner_by_id={source_id: "owner" for source_id in source_ids.values()},
        artifact_lookup_fail_ids=[source_ids["unavailable-source-decoy"]],
        unique_derived_snippets=True,
    )
    ordered_ids = [
        derived_ids["blocked-domain-decoy"],
        derived_ids["outside-domain-decoy"],
        derived_ids["sensitive-artifact-decoy"],
        derived_ids["unsupported-class-decoy"],
        derived_ids["malformed-policy-decoy"],
        derived_ids["incomplete-lifecycle-decoy"],
        derived_ids["unavailable-source-decoy"],
        derived_ids["irrelevant-record-decoy"],
        derived_ids["eligible-code-artifact"],
        derived_ids["eligible-document-artifact"],
    ]
    fake_qdrant = FakeQdrant(
        artifact_ids=ordered_ids,
        artifact_scores=[0.99, 0.98, 0.97, 0.96, 0.95, 0.94, 0.93, 0.92, 0.21, 0.2],
    )

    async def empty_message_search(**kwargs):
        fake_qdrant.search_calls.append(kwargs)
        return []

    fake_qdrant.search = empty_message_search
    fake_settings = types.SimpleNamespace(
        memory_api_key="testkey",
        require_request_id=True,
        enforce_request_id_header_body_match=True,
        retrieval_k=1,
        retrieval_artifact_k=2,
        retrieval_artifact_max_snippet_chars=500,
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

    relationship = {
        "applied": True,
        "relationship_ids": [selected_rel],
        "entity_ids": [],
        "relationship_scopes": ["project"],
        "reason_codes": ["eligible_relationship_scope_selected"],
    }
    rid = "rid-artifact-containment-crowding"
    r = await _post_retrieve_bundle(
        conversation_id=str(uuid.uuid4()),
        request_id=rid,
        body={
            "request_id": rid,
            "owner_id": "owner",
            "query": "eligible-code-artifact",
            "include_artifacts": True,
            "containment_policy": _mandatory_policy(
                domains=["technical"],
                blocked=["finance"],
                relationship=relationship,
            ),
        },
    )

    assert r.status_code == 200
    body = r.json()
    refs = body["bundle"]["artifact_refs"]
    returned_ids = [item["source_ref"]["ref_id"] for item in refs]
    assert returned_ids == [
        derived_ids["eligible-code-artifact"],
        derived_ids["eligible-document-artifact"],
    ]
    assert set(returned_ids).isdisjoint(set(ordered_ids[:-2]))
    assert fake_qdrant.artifact_search_calls[0]["k"] == 40
    assert fake_qdrant.artifact_search_calls[0]["policy_filter"]["content_classes"] == ["code", "document"]
    containment = body["bundle"]["retrieval_debug"]["containment_policy"]
    assert containment["pre_limit_policy_filter_applied"] is True
    assert containment["relationship_narrowing_applied"] is True
    assert containment["retained_count"] == 2
    omitted = containment["omitted_counts_by_reason"]
    assert omitted["blocked_domain"] == 1
    assert omitted["outside_allowed_domain"] == 1
    assert omitted["sensitivity_ceiling_exceeded"] == 1
    assert omitted["content_class_not_allowed"] == 1
    assert omitted["malformed_policy_metadata"] == 1
    assert omitted["relationship_scope_mismatch"] == 1
    truth = body["bundle"]["retrieval_debug"]["truth_qualification"]
    assert truth["source_malformed_count"] == 1
    assert truth["source_unavailable_count"] == 1
    assert truth["source_available_count"] == 2
    assert truth["derivative_omissions_by_reason"]["malformed_derivative_provenance"] == 1
    assert truth["derivative_omissions_by_reason"]["derivative_source_lookup_unavailable"] == 1
    for item in refs:
        assert item["source_availability"] == "available"
        assert len(item["source_ref"]["ref_id"]) <= 160
        assert all(len(check["ref_id"]) <= 160 for check in item["source_checks"])


@pytest.mark.asyncio
async def test_mandatory_domain_and_legacy_domain_mismatch_is_rejected(monkeypatch):
    fake_pg = FakePG()
    fake_settings = types.SimpleNamespace(memory_api_key="testkey", require_request_id=True, enforce_request_id_header_body_match=True)
    monkeypatch.setattr(main_module, "settings", fake_settings, raising=True)
    monkeypatch.setattr(main_module, "pg", fake_pg, raising=True)
    monkeypatch.setattr(main_module, "qdrant", FakeQdrant(), raising=True)

    rid = "rid-domain-conflict"
    r = await _post_retrieve_bundle(
        conversation_id=str(uuid.uuid4()),
        request_id=rid,
        body={
            "request_id": rid,
            "owner_id": "owner",
            "query": "hello",
            "allowed_memory_domains": ["personal"],
            "containment_policy": _mandatory_policy(domains=["technical"]),
        },
    )

    assert r.status_code == 422
    assert "legacy allowed_memory_domains" in str(r.json()["detail"])


@pytest.mark.asyncio
async def test_relationship_scope_narrows_without_broadening_domain_or_sensitivity(monkeypatch):
    selected_rel = str(uuid.uuid4())
    selected_entity = "entity-project"
    rel_id = str(uuid.uuid4())
    entity_id = str(uuid.uuid4())
    scope_only_id = str(uuid.uuid4())
    missing_id = str(uuid.uuid4())
    blocked_id = str(uuid.uuid4())
    fake_pg = FakePG(
        message_policy_metadata={
            rel_id: _record_policy(relationship_ids=[selected_rel], scopes=["project"]),
            entity_id: _record_policy(entity_ids=[selected_entity], scopes=["project"]),
            scope_only_id: _record_policy(scopes=["project"]),
            missing_id: _record_policy(),
            blocked_id: _record_policy(
                domains=["technical", "finance"],
                relationship_ids=[selected_rel],
            ),
        }
    )
    fake_qdrant = FakeQdrant(
        message_ids=[rel_id, entity_id, scope_only_id, missing_id, blocked_id],
        message_scores=[0.9, 0.8, 0.7, 0.6, 0.5],
    )
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

    rid = "rid-relationship-scope"
    relationship = {
        "applied": True,
        "relationship_ids": [selected_rel],
        "entity_ids": [selected_entity],
        "relationship_scopes": ["project"],
        "reason_codes": ["eligible_relationship_scope_selected"],
    }
    r = await _post_retrieve_bundle(
        conversation_id=str(uuid.uuid4()),
        request_id=rid,
        body={
            "request_id": rid,
            "owner_id": "owner",
            "query": "hello",
            "containment_policy": _mandatory_policy(domains=["technical"], blocked=["finance"], relationship=relationship),
        },
    )

    assert r.status_code == 200
    body = r.json()
    assert [item["message_id"] for item in body["bundle"]["semantic"]] == [rel_id, entity_id]
    omitted = body["bundle"]["retrieval_debug"]["containment_policy"]["omitted_counts_by_reason"]
    assert omitted["relationship_scope_mismatch"] == 2
    assert omitted["blocked_domain"] == 1
    assert fake_qdrant.search_calls[0]["policy_filter"]["relationship_scope"]["applied"] is True


@pytest.mark.asyncio
async def test_artifact_policy_uses_intersections_and_post_fetch_validation(monkeypatch):
    allowed_id = str(uuid.uuid4())
    image_id = str(uuid.uuid4())
    policy_base = {
        "source_refs": [{"ref_type": "artifact", "ref_id": str(uuid.uuid4()), "support_kind": "direct"}],
        "derivation_type": "chunk",
        "derivation_version": "file-chunk-v1",
        "status": "active",
    }
    fake_pg = FakePG(
        artifact_metadata={
            allowed_id: {**policy_base, **_record_policy(content_class="document", sensitivity="low")},
            image_id: {**policy_base, **_record_policy(content_class="image", sensitivity="low")},
        },
        unique_derived_snippets=True,
    )
    fake_qdrant = FakeQdrant(artifact_ids=[allowed_id, image_id])
    fake_settings = types.SimpleNamespace(
        memory_api_key="testkey",
        require_request_id=True,
        enforce_request_id_header_body_match=True,
        retrieval_k=8,
        retrieval_artifact_k=1,
        retrieval_artifact_max_snippet_chars=500,
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

    rid = "rid-artifact-policy"
    r = await _post_retrieve_bundle(
        conversation_id=str(uuid.uuid4()),
        request_id=rid,
        body={
            "request_id": rid,
            "owner_id": "owner",
            "query": "hello",
            "containment_policy": _mandatory_policy(
                domains=["technical"],
                artifact_classes=["document", "image"],
                surface_classes=["document"],
            ),
        },
    )

    assert r.status_code == 200
    body = r.json()
    assert [item["source_ref"]["ref_id"] for item in body["bundle"]["artifact_refs"]] == [allowed_id]
    assert fake_qdrant.artifact_search_calls[0]["k"] == 20
    assert fake_qdrant.artifact_search_calls[0]["policy_filter"]["content_classes"] == ["document"]
    omitted = body["bundle"]["retrieval_debug"]["containment_policy"]["omitted_counts_by_reason"]
    assert omitted["content_class_not_allowed"] == 1


@pytest.mark.asyncio
async def test_empty_effective_artifact_policy_skips_artifact_search(monkeypatch):
    fake_pg = FakePG()
    fake_qdrant = FakeQdrant()
    fake_settings = types.SimpleNamespace(
        memory_api_key="testkey",
        require_request_id=True,
        enforce_request_id_header_body_match=True,
        retrieval_k=8,
        retrieval_artifact_k=3,
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

    rid = "rid-empty-artifact-policy"
    r = await _post_retrieve_bundle(
        conversation_id=str(uuid.uuid4()),
        request_id=rid,
        body={
            "request_id": rid,
            "owner_id": "owner",
            "query": "hello",
            "containment_policy": _mandatory_policy(
                domains=["technical"],
                artifact_classes=["image"],
                surface_classes=["document"],
            ),
        },
    )

    assert r.status_code == 200
    assert fake_qdrant.artifact_search_calls == []
    containment = r.json()["bundle"]["retrieval_debug"]["containment_policy"]
    assert containment["artifact_search_skipped_reason"] == "artifact_policy_empty"


@pytest.mark.asyncio
async def test_mandatory_raw_and_compare_do_not_bypass_policy(monkeypatch):
    ineligible_id = str(uuid.uuid4())
    eligible_id = str(uuid.uuid4())
    fake_pg = FakePG(
        message_policy_metadata={
            eligible_id: _record_policy(domains=["technical"], sensitivity="low"),
        }
    )
    fake_qdrant = FakeQdrant(message_ids=[ineligible_id, eligible_id], message_scores=[0.99, 0.2])
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

    for mode in ("raw", "compare"):
        rid = f"rid-{mode}-mandatory"
        r = await _post_retrieve_bundle(
            conversation_id=str(uuid.uuid4()),
            request_id=rid,
            body={
                "request_id": rid,
                "owner_id": "owner",
                "query": "hello",
                "mode": mode,
                "containment_policy": _mandatory_policy(domains=["technical"]),
            },
        )
        assert r.status_code == 200
        body = r.json()
        assert [item["message_id"] for item in body["bundle"]["semantic"]] == [eligible_id]
        if mode == "compare":
            assert [item["message_id"] for item in body["raw_bundle"]["semantic"]] == [eligible_id]
            assert [item["message_id"] for item in body["augmented_bundle"]["semantic"]] == [eligible_id]


def test_structured_policy_metadata_rejects_reserved_spoofing_and_mime_contradiction():
    with pytest.raises(ValueError):
        MessageCreateRequest(
            owner_id="owner",
            role="user",
            content="hello",
            metadata={"retrieval_policy_metadata": {"memory_domains": ["technical"], "sensitivity": "low"}},
        )
    with pytest.raises(ValueError):
        ArtifactInitRequest(
            owner_id="owner",
            filename="photo.png",
            mime="image/png",
            size=10,
            policy_metadata=_record_policy(content_class="document"),
        )
    with pytest.raises(ValueError):
        ArtifactInitRequest(
            owner_id="owner",
            filename="photo.png",
            mime="image/png",
            size=10,
        )
    with pytest.raises(ValueError):
        ArtifactInitRequest(
            owner_id="owner",
            filename="notes.txt",
            mime="text/plain",
            size=10,
            policy_metadata=_record_policy(content_class="image"),
        )
    with pytest.raises(ValueError):
        ArtifactInitRequest(
            owner_id="owner",
            filename="notes.md",
            mime="text/markdown",
            size=10,
            policy_metadata=_record_policy(content_class="audio"),
        )
    with pytest.raises(ValueError):
        ArtifactInitRequest(
            owner_id="owner",
            filename="script.py",
            mime="text/plain",
            size=10,
            policy_metadata=_record_policy(content_class="video"),
        )
    with pytest.raises(ValueError):
        ArtifactInitRequest(
            owner_id="owner",
            filename="blob.bin",
            mime="application/octet-stream",
            size=10,
            policy_metadata=_record_policy(content_class="screenshot"),
        )
    ArtifactInitRequest(
        owner_id="owner",
        filename="blob.bin",
        mime="application/octet-stream",
        size=10,
        policy_metadata=_record_policy(content_class="other"),
    )
    request = MessageCreateRequest(
        owner_id="owner",
        role="user",
        content="hello",
        metadata={"domain": "personal"},
    )
    assert request.policy_metadata is None


def test_retrieval_record_policy_metadata_rejects_falsey_non_list_shapes_exactly():
    valid = {
        "memory_domains": ["technical"],
        "sensitivity": "low",
    }

    for field_name, malformed in (
        ("entity_ids", ""),
        ("entity_ids", None),
        ("relationship_ids", 0),
        ("relationship_ids", False),
        ("relationship_scopes", {}),
        ("relationship_scopes", ""),
        ("memory_domains", "technical"),
        ("memory_domains", []),
    ):
        with pytest.raises(ValueError):
            RetrievalRecordPolicyMetadata.model_validate({**valid, field_name: malformed})

    with pytest.raises(ValueError):
        RetrievalRecordPolicyMetadata.model_validate({**valid, "entity_ids": ["entity", 7]})
    with pytest.raises(ValueError):
        RetrievalRecordPolicyMetadata.model_validate({**valid, "unexpected": "nope"})

    omitted = RetrievalRecordPolicyMetadata.model_validate(valid)
    assert omitted.memory_domains == ["technical"]
    assert omitted.entity_ids == []
    assert omitted.relationship_ids == []
    assert omitted.relationship_scopes == []
