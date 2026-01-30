import asyncio
import os
from uuid import uuid4

from storage.qdrant import QdrantStore, Embedder


class FakeEmbedder(Embedder):
    """
    Deterministic tiny embedder for contract testing.
    Produces an 8-dim vector from text bytes.
    """

    async def embed_texts(self, model: str, texts: list[str]) -> list[list[float]]:
        out = []
        for t in texts:
            b = t.encode("utf-8")
            # simple stable 8-dim "hashy" vector
            v = [(b[i % len(b)] / 255.0) if b else 0.0 for i in range(8)]
            out.append(v)
        return out


async def main() -> None:
    qdrant_url = os.environ.get("QDRANT_URL", "http://127.0.0.1:16333")
    collection = os.environ.get("QDRANT_COLLECTION", "messages_test")

    embedder = FakeEmbedder()
    store = QdrantStore(url=qdrant_url, collection=collection, embedder=embedder, embed_model="fake-8d")

    owner_id = "daniel"
    client_id = "unit"
    conversation_id = uuid4()

    # Insert two messages
    m1 = uuid4()
    m2 = uuid4()

    await store.upsert_message_vector(
        message_id=m1,
        owner_id=owner_id,
        conversation_id=conversation_id,
        role="user",
        content="hello there",
        client_id=client_id,
    )
    await store.upsert_message_vector(
        message_id=m2,
        owner_id=owner_id,
        conversation_id=conversation_id,
        role="assistant",
        content="general kenobi",
        client_id=client_id,
    )

    # Search within same owner + conversation + client_id
    hits = await store.search(
        owner_id=owner_id,
        query="hello",
        k=5,
        conversation_id=conversation_id,
        client_id=client_id,
    )

    print("hits:", hits)
    assert len(hits) >= 1, "expected at least 1 hit"
    assert all(h.message_id for h in hits), "expected message_id in hits"

    # Negative test: wrong client_id should return nothing
    hits_wrong_client = await store.search(
        owner_id=owner_id,
        query="hello",
        k=5,
        conversation_id=conversation_id,
        client_id="other-client",
    )
    print("hits_wrong_client:", hits_wrong_client)
    assert len(hits_wrong_client) == 0, "expected 0 hits for wrong client_id"

    print("Layer 2B OK ✅")


if __name__ == "__main__":
    asyncio.run(main())
