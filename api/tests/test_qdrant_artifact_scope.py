from __future__ import annotations

import types
from uuid import uuid4

import pytest
from qdrant_client.models import FieldCondition

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
        return self.points


def _point(*, owner_id: str, conversation_id: str | None, score: float = 0.9):
    derived_text_id = str(uuid4())
    payload = {
        "ref_type": "derived_text",
        "derived_text_id": derived_text_id,
        "artifact_id": str(uuid4()),
        "owner_id": owner_id,
        "file_path": "notes.txt",
        "chunk_index": 0,
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
    assert not any(isinstance(item, FieldCondition) and item.key == "conversation_id" for item in must)


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
