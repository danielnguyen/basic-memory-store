from __future__ import annotations

import types
from uuid import uuid4

import pytest
from qdrant_client.models import FieldCondition, Filter, IsEmptyCondition, MatchAny

from storage.qdrant import QdrantStore, _policy_payload


class FakeEmbedder:
    async def embed_texts(self, model: str, texts: list[str]) -> list[list[float]]:
        return [[1.0, 0.0, 0.0] for _ in texts]


class FakeQdrantClient:
    def __init__(self, points):
        self.points = points
        self.last_search = None

    def create_collection(self, **kwargs):
        return None

    def search(self, **kwargs):
        self.last_search = kwargs
        query_filter = kwargs["query_filter"]
        limit = kwargs["limit"]
        return [
            point
            for point in sorted(self.points, key=lambda item: item.score, reverse=True)
            if _matches_filter(point.payload, query_filter)
        ][:limit]


def test_policy_payload_exact_validation_rejects_legacy_spoof_and_malformed_shapes():
    valid = {
        "memory_domains": ["technical"],
        "sensitivity": "low",
        "entity_ids": [],
        "relationship_ids": [],
        "relationship_scopes": [],
    }

    assert _policy_payload(valid)["retrieval_policy_valid"] is True
    assert _policy_payload({"retrieval_policy_metadata": valid}) == {"retrieval_policy_valid": False}
    assert _policy_payload({"memory_domains": "technical", "sensitivity": "low"}) == {
        "retrieval_policy_valid": False
    }
    assert _policy_payload({"memory_domains": ["technical", 7], "sensitivity": "low"}) == {
        "retrieval_policy_valid": False
    }


def _point(*, owner_id: str, conversation_id: str | None, score: float = 0.9):
    derived_text_id = str(uuid4())
    payload = {
        "ref_type": "derived_text",
        "derived_text_id": derived_text_id,
        "artifact_id": str(uuid4()),
        "owner_id": owner_id,
        "file_path": "notes.txt",
        "chunk_index": 0,
        "derivation_status": "active",
    }
    if conversation_id is not None:
        payload["conversation_id"] = conversation_id
    return types.SimpleNamespace(score=score, payload=payload)


def _message_point(*, owner_id: str, score: float, domains=None, sensitivity="low"):
    message_id = str(uuid4())
    payload = {
        "ref_type": "message",
        "message_id": message_id,
        "owner_id": owner_id,
        "conversation_id": str(uuid4()),
        "role": "assistant",
        "retrieval_policy_valid": True,
        "memory_domains": domains or ["technical"],
        "sensitivity": sensitivity,
        "entity_ids": [],
        "relationship_ids": [],
        "relationship_scopes": [],
    }
    return types.SimpleNamespace(score=score, payload=payload)


@pytest.mark.asyncio
async def test_artifact_conversation_scope_keeps_same_and_owner_global_only():
    conversation_id = str(uuid4())
    other_conversation_id = str(uuid4())
    same = _point(owner_id="owner-a", conversation_id=conversation_id)
    owner_global = _point(owner_id="owner-a", conversation_id=None)
    other_conversation = _point(owner_id="owner-a", conversation_id=other_conversation_id)
    cross_owner = _point(owner_id="owner-b", conversation_id=conversation_id)

    store = QdrantStore(url="http://unused", collection="messages", embedder=FakeEmbedder(), embed_model="local")
    fake_client = FakeQdrantClient([same, owner_global, other_conversation, cross_owner])
    store.client = fake_client
    store._collection_ready = True

    hits = await store.search_artifact_chunks(
        owner_id="owner-a",
        query="alpha",
        k=10,
        min_score=0.0,
        conversation_id=conversation_id,
    )

    assert [hit.derived_text_id for hit in hits] == [
        same.payload["derived_text_id"],
        owner_global.payload["derived_text_id"],
    ]
    must = fake_client.last_search["query_filter"].must
    assert any(isinstance(item, FieldCondition) and item.key == "owner_id" for item in must)
    scope_filter = next(item for item in must if isinstance(item, Filter))
    assert any(isinstance(item, FieldCondition) and item.key == "conversation_id" for item in scope_filter.should)
    assert any(isinstance(item, IsEmptyCondition) for item in scope_filter.should)
    assert fake_client.last_search["limit"] == 10


@pytest.mark.asyncio
async def test_artifact_owner_scope_keeps_owner_artifacts_across_conversations():
    same_owner_other_conversation = _point(owner_id="owner-a", conversation_id=str(uuid4()))
    owner_global = _point(owner_id="owner-a", conversation_id=None)
    cross_owner = _point(owner_id="owner-b", conversation_id=None)

    store = QdrantStore(url="http://unused", collection="messages", embedder=FakeEmbedder(), embed_model="local")
    fake_client = FakeQdrantClient([same_owner_other_conversation, owner_global, cross_owner])
    store.client = fake_client
    store._collection_ready = True

    hits = await store.search_artifact_chunks(
        owner_id="owner-a",
        query="alpha",
        k=10,
        min_score=0.0,
        conversation_id=None,
    )

    assert [hit.derived_text_id for hit in hits] == [
        same_owner_other_conversation.payload["derived_text_id"],
        owner_global.payload["derived_text_id"],
    ]


@pytest.mark.asyncio
async def test_artifact_conversation_scope_filter_prevents_high_score_crowding():
    conversation_id = str(uuid4())
    hidden_valid = _point(owner_id="owner-a", conversation_id=conversation_id, score=0.1)
    owner_global = _point(owner_id="owner-a", conversation_id=None, score=0.09)
    other_conversation_points = [
        _point(owner_id="owner-a", conversation_id=str(uuid4()), score=0.99 - (idx / 1000))
        for idx in range(150)
    ]

    store = QdrantStore(url="http://unused", collection="messages", embedder=FakeEmbedder(), embed_model="local")
    fake_client = FakeQdrantClient([*other_conversation_points, hidden_valid, owner_global])
    store.client = fake_client
    store._collection_ready = True

    hits = await store.search_artifact_chunks(
        owner_id="owner-a",
        query="alpha",
        k=2,
        min_score=0.0,
        conversation_id=conversation_id,
    )

    assert [hit.derived_text_id for hit in hits] == [
        hidden_valid.payload["derived_text_id"],
        owner_global.payload["derived_text_id"],
    ]
    assert fake_client.last_search["limit"] == 2


@pytest.mark.asyncio
async def test_message_mandatory_policy_filter_prevents_high_score_crowding():
    eligible = _message_point(owner_id="owner-a", score=0.1, domains=["technical"])
    ineligible = [
        _message_point(owner_id="owner-a", score=0.99 - (idx / 1000), domains=["personal"])
        for idx in range(150)
    ]
    store = QdrantStore(url="http://unused", collection="messages", embedder=FakeEmbedder(), embed_model="local")
    fake_client = FakeQdrantClient([*ineligible, eligible])
    store.client = fake_client
    store._collection_ready = True

    hits = await store.search(
        owner_id="owner-a",
        query="alpha",
        k=1,
        min_score=0.0,
        policy_filter={
            "allowed_domains": ["technical"],
            "blocked_domains": [],
            "allowed_sensitivities": ["low", "medium", "high"],
            "relationship_scope": {"applied": False},
        },
    )

    assert [hit.message_id for hit in hits] == [eligible.payload["message_id"]]
    must = fake_client.last_search["query_filter"].must
    assert any(
        isinstance(item, FieldCondition)
        and item.key == "memory_domains"
        and isinstance(item.match, MatchAny)
        and item.match.any == ["technical"]
        for item in must
    )
    assert fake_client.last_search["limit"] == 1


@pytest.mark.asyncio
async def test_artifact_mandatory_policy_filter_prevents_metadata_crowding():
    eligible = _point(owner_id="owner-a", conversation_id=None, score=0.1)
    eligible.payload.update(
        {
            "retrieval_policy_valid": True,
            "memory_domains": ["technical"],
            "sensitivity": "low",
            "content_class": "document",
            "entity_ids": [],
            "relationship_ids": [],
            "relationship_scopes": [],
        }
    )
    ineligible = []
    for idx in range(150):
        point = _point(owner_id="owner-a", conversation_id=None, score=0.99 - (idx / 1000))
        point.payload.update(
            {
                "retrieval_policy_valid": True,
                "memory_domains": ["personal"],
                "sensitivity": "low",
                "content_class": "document",
                "entity_ids": [],
                "relationship_ids": [],
                "relationship_scopes": [],
            }
        )
        ineligible.append(point)
    store = QdrantStore(url="http://unused", collection="messages", embedder=FakeEmbedder(), embed_model="local")
    fake_client = FakeQdrantClient([*ineligible, eligible])
    store.client = fake_client
    store._collection_ready = True

    hits = await store.search_artifact_chunks(
        owner_id="owner-a",
        query="alpha",
        k=1,
        min_score=0.0,
        policy_filter={
            "allowed_domains": ["technical"],
            "blocked_domains": [],
            "allowed_sensitivities": ["low", "medium"],
            "content_classes": ["document"],
            "relationship_scope": {"applied": False},
        },
    )

    assert [hit.derived_text_id for hit in hits] == [eligible.payload["derived_text_id"]]
    assert fake_client.last_search["limit"] == 1


def _matches_filter(payload: dict, query_filter) -> bool:
    must = query_filter.must or []
    if isinstance(must, (FieldCondition, Filter, IsEmptyCondition)):
        must = [must]
    must_not = query_filter.must_not or []
    if isinstance(must_not, (FieldCondition, Filter, IsEmptyCondition)):
        must_not = [must_not]
    should = query_filter.should or []
    if isinstance(should, (FieldCondition, Filter, IsEmptyCondition)):
        should = [should]
    return all(_matches_condition(payload, item) for item in must) and not any(
        _matches_condition(payload, item) for item in must_not
    ) and (
        not should or any(_matches_condition(payload, item) for item in should)
    )


def _matches_condition(payload: dict, condition) -> bool:
    if isinstance(condition, Filter):
        return _matches_filter(payload, condition)
    if isinstance(condition, FieldCondition):
        if condition.match is not None:
            if isinstance(condition.match, MatchAny):
                value = payload.get(condition.key)
                if isinstance(value, list):
                    return bool(set(value) & set(condition.match.any))
                return value in set(condition.match.any)
            return payload.get(condition.key) == condition.match.value
        return False
    if isinstance(condition, IsEmptyCondition):
        return condition.is_empty.key not in payload
    raise AssertionError(f"unsupported fake Qdrant condition: {condition!r}")
