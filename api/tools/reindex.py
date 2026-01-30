from __future__ import annotations

import argparse
import asyncio
import logging
from uuid import UUID

from settings import get_settings
from clients.litellm import LiteLLMClient
from storage.postgres import PostgresStore
from storage.qdrant import QdrantStore

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")


async def main() -> None:
    ap = argparse.ArgumentParser(description="Reindex Postgres messages into Qdrant (derived index).")
    ap.add_argument("--owner-id", required=True)
    ap.add_argument("--since", default=None, help="ISO timestamp (timestamptz), e.g. 2026-01-01T00:00:00Z")
    ap.add_argument("--conversation-id", default=None, help="UUID string (optional)")
    ap.add_argument("--batch-size", type=int, default=500)
    ap.add_argument("--max", type=int, default=5000)
    args = ap.parse_args()

    settings = get_settings()

    pg = PostgresStore(settings.pg_dsn)
    litellm = LiteLLMClient(settings.litellm_base_url, settings.litellm_api_key)
    qdrant = QdrantStore(settings.qdrant_url, settings.qdrant_collection, litellm, settings.embed_model)

    await pg.open()
    try:
        conv_id = UUID(args.conversation_id) if args.conversation_id else None

        total = 0
        offset = 0

        while total < args.max:
            batch = await pg.get_messages_for_reindex(
                owner_id=args.owner_id,
                since=args.since,
                conversation_id=conv_id,
                limit=min(args.batch_size, args.max - total),
                offset=offset,
            )
            if not batch:
                break

            for row in batch:
                try:
                    await qdrant.upsert_message_vector(
                        message_id=row["message_id"],
                        owner_id=row["owner_id"],
                        conversation_id=row["conversation_id"],
                        role=row["role"],
                        content=row["content"],
                        client_id=row["client_id"],
                    )
                except Exception:
                    logging.exception(
                        "reindex upsert failed (continuing)",
                        extra={"message_id": str(row["message_id"])},
                    )

            total += len(batch)
            offset += len(batch)
            logging.info("reindex progress: %s", total)

        logging.info("reindex complete: %s messages processed", total)

    finally:
        await pg.close()


if __name__ == "__main__":
    asyncio.run(main())
