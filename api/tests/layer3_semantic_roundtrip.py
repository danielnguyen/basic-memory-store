import asyncio
import os
from uuid import UUID

from clients.litellm import LiteLLMClient, LiteLLMEmbedder
from storage.postgres import PostgresStore
from storage.qdrant import QdrantStore


def _env(name: str) -> str:
    v = os.environ.get(name)
    if not v:
        raise SystemExit(f"Missing env var: {name}")
    return v


async def main() -> None:
    # --- Required env vars ---
    pg_dsn = _env("PG_DSN")
    qdrant_url = _env("QDRANT_URL")
    litellm_base = _env("LITELLM_BASE_URL")

    # Optional
    litellm_key = os.environ.get("LITELLM_API_KEY")  # if your LiteLLM requires auth
    qdrant_collection = os.environ.get("QDRANT_COLLECTION", "messages")
    embed_model = os.environ.get("EMBED_MODEL", "text-embedding-3-small")

    owner_id = os.environ.get("OWNER_ID", "daniel")
    client_id = os.environ.get("CLIENT_ID", "unit")

    # --- Clients/stores ---
    pg = PostgresStore(pg_dsn)
    await pg.open()

    litellm = LiteLLMClient(base_url=litellm_base, api_key=litellm_key)
    embedder = LiteLLMEmbedder(litellm)
    qd = QdrantStore(url=qdrant_url, collection=qdrant_collection, embedder=embedder, embed_model=embed_model)

    # --- Write path: Postgres is source of truth ---
    cid = await pg.create_conversation(owner_id=owner_id, client_id=client_id, title="layer3 semantic roundtrip")

    message_specs = [
        ("user", "I drive a Chevrolet Equinox EV."),
        ("assistant", "Got it — you drive an Equinox EV."),
        ("user", "I want persistent conversation memory across my devices."),
        ("assistant", "We can store messages in Postgres and index embeddings in Qdrant."),
        ("user", "I live in Toronto."),
    ]

    inserted_message_ids: list[UUID] = []
    for role, content in message_specs:
        mid = await pg.add_message(
            conversation_id=cid,
            owner_id=owner_id,
            role=role,
            content=content,
            client_id=client_id,
            metadata={"layer": "layer3"},
        )
        inserted_message_ids.append(mid)

        # Index into Qdrant (derivable index)
        await qd.upsert_message_vector(
            message_id=mid,
            owner_id=owner_id,
            conversation_id=cid,
            role=role,
            content=content,
            client_id=client_id,
        )

    print(f"Created conversation: {cid}")
    print(f"Inserted {len(inserted_message_ids)} messages and indexed embeddings into Qdrant collection '{qdrant_collection}'.")

    # --- Read path: semantic retrieval via Qdrant, hydrate via Postgres ---
    query = "What vehicle do I drive?"
    hits = await qd.search(
        owner_id=owner_id,
        query=query,
        k=5,
        conversation_id=cid,
        client_id=client_id,
    )

    print("\n--- Qdrant hits ---")
    for h in hits:
        print(f"  score={h.score:.4f} message_id={h.message_id}")

    hit_ids: list[UUID] = []
    for h in hits:
        try:
            hit_ids.append(UUID(h.message_id))
        except Exception:
            # Should never happen because we store UUID message_id strings
            pass

    snippets = await pg.get_message_snippets_by_ids(hit_ids)

    print("\n--- Hydrated snippets (from Postgres, in hit order) ---")
    for s in snippets:
        print(f"- [{s['created_at']}] {s['role']}: {s['content']} (id={s['message_id']})")

    # Basic sanity assertions
    assert any("Equinox" in s["content"] for s in snippets), "Expected to retrieve the Equinox EV message."
    print("\nLayer 3 semantic round-trip OK ✅")

    await pg.close()


if __name__ == "__main__":
    asyncio.run(main())
