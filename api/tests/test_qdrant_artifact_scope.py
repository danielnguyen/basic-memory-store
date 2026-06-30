from __future__ import annotations

import types
from uuid import uuid4

import pytest
from qdrant_client.models import FieldCondition, Filter, IsEmptyCondition

from storage.qdrant import QdrantStore


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


def _matches_filter(payload: dict, query_filter) -> bool:
    must = query_filter.must or []
    if isinstance(must, (FieldCondition, Filter, IsEmptyCondition)):
        must = [must]
    should = query_filter.should or []
    if isinstance(should, (FieldCondition, Filter, IsEmptyCondition)):
        should = [should]
    return all(_matches_condition(payload, item) for item in must) and (
        not should or any(_matches_condition(payload, item) for item in should)
    )


def _matches_condition(payload: dict, condition) -> bool:
    if isinstance(condition, Filter):
        return _matches_filter(payload, condition)
    if isinstance(condition, FieldCondition):
        if condition.match is not None:
            return payload.get(condition.key) == condition.match.value
        return False
    if isinstance(condition, IsEmptyCondition):
        return condition.is_empty.key not in payload
    raise AssertionError(f"unsupported fake Qdrant condition: {condition!r}")
