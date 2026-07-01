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
    IsEmptyCondition,
    MatchAny,
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


@dataclass
class ArtifactChunkHit:
    derived_text_id: str
    artifact_id: str
    file_path: str
    repo_name: str | None
    score: float


def _policy_payload(policy_metadata: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(policy_metadata, dict):
        return {"retrieval_policy_valid": False}
    payload: dict[str, Any] = {
        "retrieval_policy_valid": True,
        "memory_domains": list(policy_metadata.get("memory_domains") or []),
        "sensitivity": policy_metadata.get("sensitivity"),
        "entity_ids": list(policy_metadata.get("entity_ids") or []),
        "relationship_ids": list(policy_metadata.get("relationship_ids") or []),
        "relationship_scopes": list(policy_metadata.get("relationship_scopes") or []),
    }
    if policy_metadata.get("content_class") is not None:
        payload["content_class"] = policy_metadata.get("content_class")
    return payload


def _append_policy_filter(must: list[Any], must_not: list[Any], policy_filter: dict[str, Any] | None) -> bool:
    if policy_filter is None:
        return True
    allowed_domains = list(policy_filter.get("allowed_domains") or [])
    if not allowed_domains:
        return False
    must.extend(
        [
            FieldCondition(key="retrieval_policy_valid", match=MatchValue(value=True)),
            FieldCondition(key="memory_domains", match=MatchAny(any=allowed_domains)),
            FieldCondition(
                key="sensitivity",
                match=MatchAny(any=list(policy_filter.get("allowed_sensitivities") or ["low", "medium", "high"])),
            ),
        ]
    )
    blocked_domains = list(policy_filter.get("blocked_domains") or [])
    if blocked_domains:
        must_not.append(FieldCondition(key="memory_domains", match=MatchAny(any=blocked_domains)))
    content_classes = list(policy_filter.get("content_classes") or [])
    if content_classes:
        must.append(FieldCondition(key="content_class", match=MatchAny(any=content_classes)))
    relationship = policy_filter.get("relationship_scope") or {}
    if relationship.get("applied"):
        relationship_ids = list(relationship.get("relationship_ids") or [])
        entity_ids = list(relationship.get("entity_ids") or [])
        must.append(
            Filter(
                should=[
                    FieldCondition(key="relationship_ids", match=MatchAny(any=relationship_ids)),
                    FieldCondition(key="entity_ids", match=MatchAny(any=entity_ids)),
                ]
            )
        )
        relationship_scopes = list(relationship.get("relationship_scopes") or [])
        if relationship_scopes:
            must.append(
                Filter(
                    should=[
                        IsEmptyCondition(is_empty={"key": "relationship_scopes"}),
                        FieldCondition(key="relationship_scopes", match=MatchAny(any=relationship_scopes)),
                    ]
                )
            )
    return True


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
        policy_metadata: dict[str, Any] | None = None,
    ) -> None:
        vec = (await self.embedder.embed_texts(self.embed_model, [content]))[0]
        self.ensure_collection(vector_size=len(vec))

        payload: dict[str, Any] = {
            "ref_type": "message",
            "message_id": str(message_id),
            "owner_id": owner_id,
            "conversation_id": str(conversation_id),
            "role": role,
        }

        if client_id is not None:
            payload["client_id"] = client_id

        if tags:
            payload["tags"] = tags
        payload.update(_policy_payload(policy_metadata))

        point = PointStruct(
            id=str(message_id),   # Qdrant accepts UUID strings
            vector=vec,
            payload=payload,
        )

        self.client.upsert(
            collection_name=self.collection,
            points=[point],
        )

    async def upsert_derived_text_vector(
        self,
        *,
        derived_text_id: UUID,
        artifact_id: UUID,
        owner_id: str,
        content: str,
        client_id: str | None = None,
        conversation_id: UUID | str | None = None,
        qdrant_point_id: UUID | str | None = None,
        derivation_status: str = "active",
        derivation_attempt_id: UUID | str | None = None,
        derivation_version: str | None = None,
        file_path: str,
        repo_name: str | None,
        chunk_index: int,
        policy_metadata: dict[str, Any] | None = None,
    ) -> None:
        vec = (await self.embedder.embed_texts(self.embed_model, [content]))[0]
        self.ensure_collection(vector_size=len(vec))

        payload: dict[str, Any] = {
            "ref_type": "derived_text",
            "derived_text_id": str(derived_text_id),
            "artifact_id": str(artifact_id),
            "owner_id": owner_id,
            "file_path": file_path,
            "chunk_index": chunk_index,
            "derivation_status": derivation_status,
        }
        if client_id is not None:
            payload["client_id"] = client_id
        if conversation_id is not None:
            payload["conversation_id"] = str(conversation_id)
        if repo_name is not None:
            payload["repo_name"] = repo_name
        if derivation_attempt_id is not None:
            payload["derivation_attempt_id"] = str(derivation_attempt_id)
        if derivation_version is not None:
            payload["derivation_version"] = derivation_version
        payload.update(_policy_payload(policy_metadata))

        point = PointStruct(
            id=str(qdrant_point_id or derived_text_id),
            vector=vec,
            payload=payload,
        )
        self.client.upsert(
            collection_name=self.collection,
            points=[point],
        )

    async def mark_derived_text_vector_inactive(
        self,
        *,
        qdrant_point_id: UUID | str,
        derivation_status: str = "inactive",
    ) -> None:
        self.client.set_payload(
            collection_name=self.collection,
            payload={"derivation_status": derivation_status},
            points=[str(qdrant_point_id)],
        )

    async def search(
        self,
        owner_id: str,
        query: str,
        k: int = 8,
        conversation_id: UUID | str | None = None,
        client_id: str | None = None,
        min_score: float = 0.25,
        exclude_message_ids: list[str] | None = None,
        policy_filter: dict[str, Any] | None = None,
    ) -> list[RetrievalHit]:

        qvec = (await self.embedder.embed_texts(self.embed_model, [query]))[0]
        self.ensure_collection(vector_size=len(qvec))

        must = [
            FieldCondition(key="owner_id", match=MatchValue(value=owner_id)),
            FieldCondition(key="ref_type", match=MatchValue(value="message")),
        ]

        if client_id is not None:
            must.append(FieldCondition(key="client_id", match=MatchValue(value=str(client_id))))

        if conversation_id is not None:
            must.append(FieldCondition(key="conversation_id", match=MatchValue(value=str(conversation_id))))

        must_not: list[Any] = []

        if exclude_message_ids:
            # Exclude exact message ids (common case: query message or freshly inserted ids)
            for mid in exclude_message_ids:
                must_not.append(FieldCondition(key="message_id", match=MatchValue(value=str(mid))))
        if not _append_policy_filter(must, must_not, policy_filter):
            return []

        qfilter = Filter(must=must, must_not=must_not or None)

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

    async def search_artifact_chunks(
        self,
        *,
        owner_id: str,
        query: str,
        k: int,
        min_score: float,
        client_id: str | None = None,
        conversation_id: UUID | str | None = None,
        policy_filter: dict[str, Any] | None = None,
    ) -> list[ArtifactChunkHit]:
        qvec = (await self.embedder.embed_texts(self.embed_model, [query]))[0]
        self.ensure_collection(vector_size=len(qvec))

        must: list[Any] = [
            FieldCondition(key="owner_id", match=MatchValue(value=owner_id)),
            FieldCondition(key="ref_type", match=MatchValue(value="derived_text")),
            FieldCondition(key="derivation_status", match=MatchValue(value="active")),
        ]
        must_not: list[Any] = []
        if client_id is not None:
            must.append(FieldCondition(key="client_id", match=MatchValue(value=str(client_id))))
        if conversation_id is not None:
            must.append(
                Filter(
                    should=[
                        FieldCondition(key="conversation_id", match=MatchValue(value=str(conversation_id))),
                        IsEmptyCondition(is_empty={"key": "conversation_id"}),
                    ]
                )
            )
        if not _append_policy_filter(must, must_not, policy_filter):
            return []
        res = self.client.search(
            collection_name=self.collection,
            query_vector=qvec,
            limit=k,
            query_filter=Filter(must=must, must_not=must_not or None),
        )

        hits: list[ArtifactChunkHit] = []
        for p in res:
            if p.score is None or p.score < min_score or not p.payload:
                continue
            if p.payload.get("owner_id") != owner_id:
                continue
            hits.append(
                ArtifactChunkHit(
                    derived_text_id=str(p.payload["derived_text_id"]),
                    artifact_id=str(p.payload["artifact_id"]),
                    file_path=str(p.payload["file_path"]),
                    repo_name=p.payload.get("repo_name"),
                    score=float(p.score),
                )
            )
            if len(hits) >= k:
                break
        return hits

    def ping(self) -> None:
        # Lightest check: server reachable
        self.client.get_collections()
