from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol
from uuid import UUID

from qdrant_client import QdrantClient
from qdrant_client.http.exceptions import UnexpectedResponse
from qdrant_client.models import (
    Distance,
    VectorParams,
    PointStruct,
    Filter,
    FieldCondition,
    MatchValue,
)

class Embedder(Protocol):
    async def embed_texts(self, model: str, texts: list[str]) -> list[list[float]]:
        """Return one embedding vector per input text."""
        ...

@dataclass
class RetrievalHit:
    message_id: str
    score: float

class QdrantStore:
    def __init__(
        self,
        url: str,
        collection: str,
        embedder: Embedder,
        embed_model: str,
    ) -> None:
        self.client = QdrantClient(url=url)
        self.collection = collection
        self.embedder = embedder
        self.embed_model = embed_model
        self._collection_ready = False


    def ensure_collection(self, vector_size: int) -> None:
        """
        Ensure collection exists with the expected vector size.

        Strategy:
        - Create if missing
        - Ignore if already exists
        """
        if self._collection_ready:
            return

        try:
            self.client.create_collection(
                collection_name=self.collection,
                vectors_config=VectorParams(
                    size=vector_size,
                    distance=Distance.COSINE,
                ),
            )
        except UnexpectedResponse as e:
            # Qdrant returns 409 if collection already exists
            if "already exists" not in str(e).lower() and "409" not in str(e):
                raise

        self._collection_ready = True


    async def upsert_message_vector(
        self,
        message_id: UUID,
        owner_id: str,
        conversation_id: UUID,
        role: str,
        content: str,
        client_id: str | None = None,
        tags: dict | None = None,
    ) -> None:
        vec = (await self.embedder.embed_texts(self.embed_model, [content]))[0]
        self.ensure_collection(vector_size=len(vec))

        payload: dict[str, Any] = {
            "message_id": str(message_id),
            "owner_id": owner_id,
            "conversation_id": str(conversation_id),
            "role": role,
        }

        if client_id is not None:
            payload["client_id"] = client_id

        if tags:
            payload["tags"] = tags

        point = PointStruct(
            id=str(message_id),   # Qdrant accepts UUID strings
            vector=vec,
            payload=payload,
        )

        self.client.upsert(
            collection_name=self.collection,
            points=[point],
        )


    async def search(
        self,
        owner_id: str,
        query: str,
        k: int = 8,
        conversation_id: UUID | str | None = None,
        client_id: str | None = None,
        min_score: float = 0.25,
    ) -> list[RetrievalHit]:

        qvec = (await self.embedder.embed_texts(self.embed_model, [query]))[0]
        self.ensure_collection(vector_size=len(qvec))

        must = [
            FieldCondition(key="owner_id", match=MatchValue(value=owner_id))
        ]

        if client_id is not None:
            must.append(FieldCondition(key="client_id", match=MatchValue(value=str(client_id))))

        if conversation_id is not None:
            must.append(FieldCondition(key="conversation_id", match=MatchValue(value=str(conversation_id))))

        qfilter = Filter(must=must)

        res = self.client.search(
            collection_name=self.collection,
            query_vector=qvec,
            limit=k,
            query_filter=qfilter,
        )

        hits: list[RetrievalHit] = []
        for p in res:
            if p.score is None or p.score < min_score:
                continue
            if p.payload and "message_id" in p.payload:
                hits.append(RetrievalHit(message_id=p.payload["message_id"], score=float(p.score)))

        return hits

    def ping(self) -> None:
        # Lightest check: server reachable
        self.client.get_collections()

