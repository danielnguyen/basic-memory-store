from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional
from uuid import UUID

from psycopg_pool import AsyncConnectionPool
from psycopg.types.json import Json


@dataclass
class Conversation:
    id: UUID
    owner_id: str
    client_id: Optional[str]
    title: Optional[str]


@dataclass
class MessageRow:
    id: UUID
    conversation_id: UUID
    owner_id: str
    client_id: Optional[str]
    role: str
    content: str
    metadata: Optional[dict]
    created_at: str


class PostgresStore:
    def __init__(self, dsn: str) -> None:
        self.pool = AsyncConnectionPool(conninfo=dsn, min_size=1, max_size=10, open=False)

    async def open(self) -> None:
        await self.pool.open()

    async def close(self) -> None:
        await self.pool.close()

    async def create_conversation(
        self,
        owner_id: str,
        client_id: str | None = None,
        title: str | None = None,
    ) -> UUID:
        q = """
        INSERT INTO conversations (owner_id, client_id, title)
        VALUES (%s, %s, %s)
        RETURNING id;
        """
        async with self.pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(q, (owner_id, client_id, title))
                row = await cur.fetchone()
                return row[0]

    async def add_message(
        self,
        conversation_id: UUID,
        owner_id: str,
        role: str,
        content: str,
        client_id: str | None = None,
        metadata: dict | None = None,
    ) -> UUID:
        q = """
        INSERT INTO messages (conversation_id, owner_id, client_id, role, content, metadata)
        VALUES (%s, %s, %s, %s, %s, %s)
        RETURNING id;
        """
        q_touch = """
        UPDATE conversations
        SET updated_at = now()
        WHERE id = %s;
        """
        meta_param = Json(metadata) if metadata is not None else None
        async with self.pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(q, (conversation_id, owner_id, client_id, role, content, meta_param))
                row = await cur.fetchone()
                # bump conversation activity timestamp
                await cur.execute(q_touch, (conversation_id,))
                return row[0]


    async def get_recent_messages(self, conversation_id: UUID, limit: int = 10) -> list[dict[str, Any]]:
        """
        Returns messages in chronological order (oldest -> newest) for prompt assembly.
        Includes created_at for debugging and future ordering guarantees.
        """
        q = """
        SELECT role, content, created_at
        FROM messages
        WHERE conversation_id = %s
        ORDER BY created_at DESC
        LIMIT %s;
        """
        async with self.pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(q, (conversation_id, limit))
                rows = await cur.fetchall()

        # We queried newest-first; reverse to get oldest-first for LLM context.
        rows.reverse()
        return [{"role": r[0], "content": r[1], "created_at": str(r[2])} for r in rows]

    async def get_message_snippets_by_ids(self, ids: list[UUID]) -> list[dict[str, Any]]:
        """
        Fetch message snippets by id.

        Important behaviors:
        - Works reliably with psycopg3 by passing a text[] to ANY(%s)
        - Preserves the original order of `ids` (Qdrant search order matters)
        """
        if not ids:
            return []

        id_strs = [str(i) for i in ids]

        q = """
        SELECT id, conversation_id, role, content, created_at
        FROM messages
        WHERE id = ANY(%s);
        """
        async with self.pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(q, (id_strs,))
                rows = await cur.fetchall()

        by_id: dict[str, dict[str, Any]] = {}
        for (mid, cid, role, content, created_at) in rows:
            by_id[str(mid)] = {
                "message_id": str(mid),
                "conversation_id": str(cid),
                "role": role,
                "content": content,
                "created_at": str(created_at),
            }

        # Preserve input order
        return [by_id[mid] for mid in id_strs if mid in by_id]

    async def list_conversations(
        self,
        owner_id: str,
        client_id: str | None = None,
        limit: int = 20,
        cursor: str | None = None,
    ) -> tuple[list[dict[str, Any]], str | None]:
        """
        List conversations for an owner (optionally per client_id), ordered by updated_at desc.

        Cursor is an opaque string you pass back from next_cursor.
        Format: "{updated_at_iso}|{conversation_uuid}"
        """
        params: list[Any] = [owner_id]
        where = "WHERE owner_id = %s"

        if client_id is not None:
            where += " AND client_id = %s"
            params.append(client_id)

        # Pagination: fetch rows strictly "before" cursor in (updated_at, id) ordering
        cursor_clause = ""
        if cursor:
            try:
                ts_str, id_str = cursor.split("|", 1)
                cursor_clause = " AND (updated_at, id) < (%s::timestamptz, %s::uuid)"
                params.extend([ts_str, id_str])
            except ValueError:
                # bad cursor -> treat as no cursor
                cursor_clause = ""

        q = f"""
        SELECT id, owner_id, client_id, title, created_at, updated_at
        FROM conversations
        {where}
        {cursor_clause}
        ORDER BY updated_at DESC, id DESC
        LIMIT %s;
        """
        params.append(limit)

        async with self.pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(q, tuple(params))
                rows = await cur.fetchall()

        out: list[dict[str, Any]] = []
        next_cursor: str | None = None

        for (cid, owner, c_id, title, created_at, updated_at) in rows:
            out.append(
                {
                    "conversation_id": str(cid),
                    "owner_id": owner,
                    "client_id": c_id,
                    "title": title,
                    "created_at": str(created_at),
                    "updated_at": str(updated_at),
                }
            )

        if rows:
            last = rows[-1]
            last_updated_at = str(last[5])  # updated_at
            last_id = str(last[0])
            next_cursor = f"{last_updated_at}|{last_id}"

        return out, next_cursor

    async def resolve_conversation(
        self,
        owner_id: str,
        client_id: str | None,
        idle_ttl_s: int = 1800,
        title: str | None = None,
    ) -> tuple[UUID, bool]:
        """
        Rolling session:
        - If most recent conversation for (owner_id, client_id) has updated_at within idle_ttl_s, reuse it.
        - Else create a new one.

        Returns (conversation_id, reused).
        """
        # If no client_id, just always create (keeps semantics unambiguous)
        if client_id is None:
            cid = await self.create_conversation(owner_id=owner_id, client_id=None, title=title)
            return cid, False

        q_find = """
        SELECT id
        FROM conversations
        WHERE owner_id = %s AND client_id = %s
          AND updated_at >= (now() - (%s || ' seconds')::interval)
        ORDER BY updated_at DESC
        LIMIT 1;
        """
        async with self.pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(q_find, (owner_id, client_id, idle_ttl_s))
                row = await cur.fetchone()

        if row:
            return row[0], True

        cid = await self.create_conversation(owner_id=owner_id, client_id=client_id, title=title)
        return cid, False
    
    async def conversation_exists(self, conversation_id: UUID) -> bool:
        q = "SELECT 1 FROM conversations WHERE id = %s"
        async with self.pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(q, (conversation_id,))
                return (await cur.fetchone()) is not None


    async def get_messages_for_reindex(
        self,
        owner_id: str,
        since: str | None = None,            # ISO timestamp string, optional
        conversation_id: UUID | None = None, # optional
        limit: int = 1000,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        where = ["owner_id = %s", "role IN ('user','assistant')"]
        params: list[Any] = [owner_id]

        if since is not None:
            where.append("created_at >= %s::timestamptz")
            params.append(since)

        if conversation_id is not None:
            where.append("conversation_id = %s")
            params.append(conversation_id)

        params.extend([limit, offset])

        q = f"""
        SELECT id, conversation_id, owner_id, client_id, role, content, created_at
        FROM messages
        WHERE {' AND '.join(where)}
        ORDER BY created_at ASC, id ASC
        LIMIT %s OFFSET %s;
        """

        async with self.pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(q, tuple(params))
                rows = await cur.fetchall()

        out: list[dict[str, Any]] = []
        for (mid, cid, owner, client_id, role, content, created_at) in rows:
            out.append(
                {
                    "message_id": mid,
                    "conversation_id": cid,
                    "owner_id": owner,
                    "client_id": client_id,
                    "role": role,
                    "content": content,
                    "created_at": str(created_at),
                }
            )
        return out


    async def ping(self) -> None:
        async with self.pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute("SELECT 1;")
                await cur.fetchone()
