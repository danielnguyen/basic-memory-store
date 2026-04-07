from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any, Optional
from uuid import UUID, uuid4

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

    async def get_conversation_by_owner_client(self, owner_id: str, client_id: str) -> dict[str, Any] | None:
        q = """
        SELECT id, owner_id, client_id, title, created_at, updated_at
        FROM conversations
        WHERE owner_id = %s AND client_id = %s
        ORDER BY created_at ASC, id ASC
        LIMIT 1;
        """
        async with self.pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(q, (owner_id, client_id))
                row = await cur.fetchone()
        if row is None:
            return None
        return {
            "conversation_id": str(row[0]),
            "owner_id": row[1],
            "client_id": row[2],
            "title": row[3],
            "created_at": str(row[4]),
            "updated_at": str(row[5]),
        }

    async def get_or_create_event_stream_conversation(
        self,
        owner_id: str,
        client_id: str,
        title: str | None = None,
    ) -> UUID:
        existing = await self.get_conversation_by_owner_client(owner_id=owner_id, client_id=client_id)
        if existing is not None:
            return UUID(existing["conversation_id"])
        return await self.create_conversation(owner_id=owner_id, client_id=client_id, title=title)

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
        SELECT id, conversation_id, role, content, metadata, created_at
        FROM messages
        WHERE id = ANY(%s);
        """
        async with self.pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(q, (id_strs,))
                rows = await cur.fetchall()

        by_id: dict[str, dict[str, Any]] = {}
        for (mid, cid, role, content, metadata, created_at) in rows:
            by_id[str(mid)] = {
                "message_id": str(mid),
                "conversation_id": str(cid),
                "role": role,
                "content": content,
                "metadata": metadata or {},
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

    async def get_conversation(self, conversation_id: UUID) -> dict[str, Any] | None:
        q = """
        SELECT id, owner_id, client_id, title, created_at, updated_at
        FROM conversations
        WHERE id = %s
        LIMIT 1;
        """
        async with self.pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(q, (conversation_id,))
                row = await cur.fetchone()
        if row is None:
            return None
        return {
            "conversation_id": str(row[0]),
            "owner_id": row[1],
            "client_id": row[2],
            "title": row[3],
            "created_at": str(row[4]),
            "updated_at": str(row[5]),
        }

    async def claim_event_ingest(
        self,
        *,
        owner_id: str,
        source_type: str,
        source_event_id: str,
        event_type: str,
        event_time: str | None,
        payload_json: dict[str, Any] | None,
    ) -> tuple[dict[str, Any], bool]:
        insert_q = """
        INSERT INTO event_ingest_log (
            owner_id, source_type, source_event_id, event_type, event_time, payload_json
        )
        VALUES (%s, %s, %s, %s, %s, %s)
        ON CONFLICT (owner_id, source_type, source_event_id) DO NOTHING
        RETURNING id, owner_id, source_type, source_event_id, event_type, event_time, payload_json,
                  conversation_id, message_id, created_at;
        """
        select_q = """
        SELECT id, owner_id, source_type, source_event_id, event_type, event_time, payload_json,
               conversation_id, message_id, created_at
        FROM event_ingest_log
        WHERE owner_id = %s AND source_type = %s AND source_event_id = %s
        LIMIT 1;
        """
        params = (
            owner_id,
            source_type,
            source_event_id,
            event_type,
            event_time,
            Json(payload_json or {}),
        )
        async with self.pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(insert_q, params)
                row = await cur.fetchone()
                created = row is not None
                if row is None:
                    await cur.execute(select_q, (owner_id, source_type, source_event_id))
                    row = await cur.fetchone()
        return {
            "event_log_id": str(row[0]),
            "owner_id": row[1],
            "source_type": row[2],
            "source_event_id": row[3],
            "event_type": row[4],
            "event_time": str(row[5]) if row[5] else None,
            "payload_json": row[6] or {},
            "conversation_id": str(row[7]) if row[7] else None,
            "message_id": str(row[8]) if row[8] else None,
            "created_at": str(row[9]),
        }, created

    async def finalize_event_ingest(
        self,
        *,
        event_log_id: UUID,
        conversation_id: UUID,
        message_id: UUID,
    ) -> dict[str, Any]:
        q = """
        UPDATE event_ingest_log
        SET conversation_id = %s,
            message_id = %s
        WHERE id = %s
        RETURNING id, conversation_id, message_id;
        """
        async with self.pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(q, (conversation_id, message_id, event_log_id))
                row = await cur.fetchone()
        return {
            "event_log_id": str(row[0]),
            "conversation_id": str(row[1]) if row[1] else None,
            "message_id": str(row[2]) if row[2] else None,
        }

    async def get_event_ingest_log(self, event_log_id: UUID) -> dict[str, Any] | None:
        q = """
        SELECT id, owner_id, source_type, source_event_id, event_type, event_time, payload_json,
               conversation_id, message_id, created_at
        FROM event_ingest_log
        WHERE id = %s
        LIMIT 1;
        """
        async with self.pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(q, (event_log_id,))
                row = await cur.fetchone()
        if row is None:
            return None
        return {
            "event_log_id": str(row[0]),
            "owner_id": row[1],
            "source_type": row[2],
            "source_event_id": row[3],
            "event_type": row[4],
            "event_time": str(row[5]) if row[5] else None,
            "payload_json": row[6] or {},
            "conversation_id": str(row[7]) if row[7] else None,
            "message_id": str(row[8]) if row[8] else None,
            "created_at": str(row[9]),
        }

    async def get_proactive_prefs(self, owner_id: str) -> dict[str, Any] | None:
        q = """
        SELECT owner_id, enabled, allowed_surfaces_json, rule_prefs_json, created_at, updated_at
        FROM proactive_prefs
        WHERE owner_id = %s
        LIMIT 1;
        """
        async with self.pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(q, (owner_id,))
                row = await cur.fetchone()
        if row is None:
            return None
        return {
            "owner_id": row[0],
            "enabled": bool(row[1]),
            "allowed_surfaces_json": row[2] or [],
            "rule_prefs_json": row[3] or {},
            "created_at": str(row[4]),
            "updated_at": str(row[5]),
        }

    async def upsert_proactive_prefs(
        self,
        *,
        owner_id: str,
        enabled: bool,
        allowed_surfaces_json: list[str],
        rule_prefs_json: dict[str, Any],
    ) -> dict[str, Any]:
        q = """
        INSERT INTO proactive_prefs (owner_id, enabled, allowed_surfaces_json, rule_prefs_json)
        VALUES (%s, %s, %s, %s)
        ON CONFLICT (owner_id) DO UPDATE
            SET enabled = EXCLUDED.enabled,
                allowed_surfaces_json = EXCLUDED.allowed_surfaces_json,
                rule_prefs_json = EXCLUDED.rule_prefs_json,
                updated_at = now()
        RETURNING owner_id, enabled, allowed_surfaces_json, rule_prefs_json, created_at, updated_at;
        """
        async with self.pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(q, (owner_id, enabled, Json(allowed_surfaces_json), Json(rule_prefs_json)))
                row = await cur.fetchone()
        return {
            "owner_id": row[0],
            "enabled": bool(row[1]),
            "allowed_surfaces_json": row[2] or [],
            "rule_prefs_json": row[3] or {},
            "created_at": str(row[4]),
            "updated_at": str(row[5]),
        }

    async def create_proactive_suggestion(
        self,
        *,
        owner_id: str,
        source_event_log_id: UUID | None,
        source_type: str,
        kind: str,
        title: str,
        body: str,
        explanation_json: dict[str, Any],
        evidence_json: dict[str, Any],
        target_surface: str | None,
    ) -> tuple[dict[str, Any], bool]:
        q = """
        INSERT INTO proactive_suggestions (
            owner_id, source_event_log_id, source_type, kind, title, body,
            explanation_json, evidence_json, target_surface
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (owner_id, source_event_log_id, kind) DO UPDATE
            SET explanation_json = EXCLUDED.explanation_json,
                evidence_json = EXCLUDED.evidence_json,
                target_surface = EXCLUDED.target_surface,
                updated_at = now()
        RETURNING id, owner_id, source_event_log_id, source_type, kind, status, title, body,
                  explanation_json, evidence_json, target_surface, delivery_surface,
                  delivery_status, delivery_external_id, delivery_error, delivered_at,
                  created_at, updated_at, (xmax = 0) AS inserted;
        """
        async with self.pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    q,
                    (
                        owner_id,
                        source_event_log_id,
                        source_type,
                        kind,
                        title,
                        body,
                        Json(explanation_json),
                        Json(evidence_json),
                        target_surface,
                    ),
                )
                row = await cur.fetchone()
        return ({
            "suggestion_id": str(row[0]),
            "owner_id": row[1],
            "source_event_log_id": str(row[2]) if row[2] else None,
            "source_type": row[3],
            "kind": row[4],
            "status": row[5],
            "title": row[6],
            "body": row[7],
            "explanation_json": row[8] or {},
            "evidence_json": row[9] or {},
            "target_surface": row[10],
            "delivery_surface": row[11],
            "delivery_status": row[12],
            "delivery_external_id": row[13],
            "delivery_error": row[14],
            "delivered_at": str(row[15]) if row[15] else None,
            "created_at": str(row[16]),
            "updated_at": str(row[17]),
        }, bool(row[18]))

    async def list_proactive_suggestions(
        self,
        *,
        owner_id: str,
        status: str | None = None,
        surface: str | None = None,
        delivery_status: str | None = None,
    ) -> list[dict[str, Any]]:
        params: list[Any] = [owner_id]
        where = ["owner_id = %s"]
        if status is not None:
            where.append("status = %s")
            params.append(status)
        if surface is not None:
            where.append("target_surface = %s")
            params.append(surface)
        if delivery_status is not None:
            where.append("delivery_status = %s")
            params.append(delivery_status)
        q = f"""
        SELECT id, owner_id, source_event_log_id, source_type, kind, status, title, body,
               explanation_json, evidence_json, target_surface, delivery_surface,
               delivery_status, delivery_external_id, delivery_error, delivered_at,
               created_at, updated_at
        FROM proactive_suggestions
        WHERE {' AND '.join(where)}
        ORDER BY created_at DESC, id DESC;
        """
        async with self.pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(q, tuple(params))
                rows = await cur.fetchall()
        return [
            {
                "suggestion_id": str(row[0]),
                "owner_id": row[1],
                "source_event_log_id": str(row[2]) if row[2] else None,
                "source_type": row[3],
                "kind": row[4],
                "status": row[5],
                "title": row[6],
                "body": row[7],
                "explanation_json": row[8] or {},
                "evidence_json": row[9] or {},
                "target_surface": row[10],
                "delivery_surface": row[11],
                "delivery_status": row[12],
                "delivery_external_id": row[13],
                "delivery_error": row[14],
                "delivered_at": str(row[15]) if row[15] else None,
                "created_at": str(row[16]),
                "updated_at": str(row[17]),
            }
            for row in rows
        ]

    async def get_proactive_suggestion(self, suggestion_id: UUID) -> dict[str, Any] | None:
        q = """
        SELECT id, owner_id, source_event_log_id, source_type, kind, status, title, body,
               explanation_json, evidence_json, target_surface, delivery_surface,
               delivery_status, delivery_external_id, delivery_error, delivered_at,
               created_at, updated_at
        FROM proactive_suggestions
        WHERE id = %s
        LIMIT 1;
        """
        async with self.pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(q, (suggestion_id,))
                row = await cur.fetchone()
        if row is None:
            return None
        return {
            "suggestion_id": str(row[0]),
            "owner_id": row[1],
            "source_event_log_id": str(row[2]) if row[2] else None,
            "source_type": row[3],
            "kind": row[4],
            "status": row[5],
            "title": row[6],
            "body": row[7],
            "explanation_json": row[8] or {},
            "evidence_json": row[9] or {},
            "target_surface": row[10],
            "delivery_surface": row[11],
            "delivery_status": row[12],
            "delivery_external_id": row[13],
            "delivery_error": row[14],
            "delivered_at": str(row[15]) if row[15] else None,
            "created_at": str(row[16]),
            "updated_at": str(row[17]),
        }

    async def record_proactive_feedback(
        self,
        *,
        suggestion_id: UUID,
        owner_id: str,
        feedback_type: str,
        reason: str | None,
    ) -> dict[str, Any]:
        next_status = None
        if feedback_type == "dismissed":
            next_status = "dismissed"
        elif feedback_type == "accepted":
            next_status = "accepted"

        insert_q = """
        INSERT INTO proactive_feedback (suggestion_id, owner_id, feedback_type, reason)
        VALUES (%s, %s, %s, %s)
        RETURNING id, created_at;
        """
        async with self.pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(insert_q, (suggestion_id, owner_id, feedback_type, reason))
                feedback_row = await cur.fetchone()
                if next_status is not None:
                    await cur.execute(
                        """
                        UPDATE proactive_suggestions
                        SET status = %s,
                            updated_at = now()
                        WHERE id = %s AND owner_id = %s
                        """,
                        (next_status, suggestion_id, owner_id),
                    )
                await cur.execute(
                    """
                    SELECT status
                    FROM proactive_suggestions
                    WHERE id = %s AND owner_id = %s
                    LIMIT 1;
                    """,
                    (suggestion_id, owner_id),
                )
                status_row = await cur.fetchone()
        if status_row is None:
            raise KeyError("suggestion not found")
        return {
            "feedback_id": str(feedback_row[0]),
            "suggestion_id": str(suggestion_id),
            "owner_id": owner_id,
            "feedback_type": feedback_type,
            "reason": reason,
            "status": status_row[0],
            "created_at": str(feedback_row[1]),
        }

    async def record_proactive_delivery_attempt(
        self,
        *,
        suggestion_id: UUID,
        owner_id: str,
        surface: str,
        delivery_status: str,
        external_id: str | None,
        error: str | None,
    ) -> dict[str, Any] | None:
        delivered_at_clause = "now()" if delivery_status == "delivered" else "NULL"
        q = f"""
        UPDATE proactive_suggestions
        SET delivery_surface = %s,
            delivery_status = %s,
            delivery_external_id = %s,
            delivery_error = %s,
            delivered_at = {delivered_at_clause},
            updated_at = now()
        WHERE id = %s AND owner_id = %s
        RETURNING id, owner_id, status, delivery_status, delivery_surface,
                  delivery_external_id, delivery_error, delivered_at, updated_at;
        """
        async with self.pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(q, (surface, delivery_status, external_id, error, suggestion_id, owner_id))
                row = await cur.fetchone()
        if row is None:
            return None
        return {
            "suggestion_id": str(row[0]),
            "owner_id": row[1],
            "status": row[2],
            "delivery_status": row[3],
            "delivery_surface": row[4],
            "delivery_external_id": row[5],
            "delivery_error": row[6],
            "delivered_at": str(row[7]) if row[7] else None,
            "updated_at": str(row[8]),
        }

    async def upsert_memory_entity(
        self,
        *,
        owner_id: str,
        entity_type: str,
        canonical_name: str,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        normalized_key = re.sub(r"\s+", " ", canonical_name.strip().lower())
        q = """
        INSERT INTO memory_entities (
            owner_id, entity_type, canonical_name, normalized_key, metadata_json
        )
        VALUES (%s, %s, %s, %s, %s)
        ON CONFLICT (owner_id, entity_type, normalized_key) DO UPDATE
            SET canonical_name = EXCLUDED.canonical_name,
                metadata_json = memory_entities.metadata_json || EXCLUDED.metadata_json,
                updated_at = now()
        RETURNING id, owner_id, entity_type, canonical_name, normalized_key, metadata_json, created_at, updated_at;
        """
        async with self.pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    q,
                    (
                        owner_id,
                        entity_type,
                        canonical_name,
                        normalized_key,
                        Json(metadata or {}),
                    ),
                )
                row = await cur.fetchone()
        return {
            "entity_id": str(row[0]),
            "owner_id": row[1],
            "entity_type": row[2],
            "canonical_name": row[3],
            "normalized_key": row[4],
            "metadata": row[5] or {},
            "created_at": str(row[6]),
            "updated_at": str(row[7]),
        }


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

    async def create_artifact(
        self,
        artifact_id: UUID,
        owner_id: str,
        filename: str,
        mime: str,
        size: int,
        object_uri: str,
        client_id: str | None = None,
        conversation_id: UUID | None = None,
        source_surface: str | None = None,
        source_kind: str | None = None,
        repo_name: str | None = None,
        repo_ref: str | None = None,
        file_path: str | None = None,
        ingestion_id: UUID | None = None,
        sha256: str | None = None,
        status: str = "pending",
    ) -> dict[str, Any]:
        q = """
        INSERT INTO artifacts (
            id, owner_id, client_id, conversation_id, filename, mime, size, object_uri, source_surface,
            status, sha256, source_kind, repo_name, repo_ref, file_path, ingestion_id, completed_at
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                CASE WHEN %s = 'completed' THEN now() ELSE NULL END)
        RETURNING id, owner_id, client_id, conversation_id, filename, mime, size, object_uri, source_surface,
                  status, sha256, created_at, completed_at, source_kind, repo_name, repo_ref, file_path, ingestion_id;
        """
        async with self.pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    q,
                    (
                        artifact_id,
                        owner_id,
                        client_id,
                        conversation_id,
                        filename,
                        mime,
                        size,
                        object_uri,
                        source_surface,
                        status,
                        sha256,
                        source_kind,
                        repo_name,
                        repo_ref,
                        file_path,
                        ingestion_id,
                        status,
                    ),
                )
                (
                    aid,
                    owner,
                    c_id,
                    convo_id,
                    name,
                    kind,
                    byte_size,
                    uri,
                    surface,
                    status_out,
                    sha256_out,
                    created_at,
                    completed_at,
                    source_kind_out,
                    repo_name_out,
                    repo_ref_out,
                    file_path_out,
                    ingestion_id_out,
                ) = await cur.fetchone()

        return {
            "artifact_id": str(aid),
            "owner_id": owner,
            "client_id": c_id,
            "conversation_id": str(convo_id) if convo_id else None,
            "filename": name,
            "mime": kind,
            "size": int(byte_size),
            "object_uri": uri,
            "source_surface": surface,
            "status": status_out,
            "sha256": sha256_out,
            "created_at": str(created_at),
            "completed_at": str(completed_at) if completed_at else None,
            "source_kind": source_kind_out,
            "repo_name": repo_name_out,
            "repo_ref": repo_ref_out,
            "file_path": file_path_out,
            "ingestion_id": str(ingestion_id_out) if ingestion_id_out else None,
        }

    async def complete_artifact(
        self,
        artifact_id: UUID,
        status: str = "completed",
        sha256: str | None = None,
    ) -> dict[str, Any] | None:
        q = """
        UPDATE artifacts
        SET
          status = %s,
          sha256 = COALESCE(%s, sha256),
          completed_at = CASE WHEN %s = 'completed' THEN now() ELSE completed_at END
        WHERE id = %s
        RETURNING id, owner_id, client_id, conversation_id, filename, mime, size, object_uri, source_surface, status, sha256, created_at, completed_at;
        """
        async with self.pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(q, (status, sha256, status, artifact_id))
                row = await cur.fetchone()

        if row is None:
            return None

        (aid, owner, c_id, convo_id, name, kind, byte_size, uri, surface, status_out, sha256_out, created_at, completed_at) = row
        return {
            "artifact_id": str(aid),
            "owner_id": owner,
            "client_id": c_id,
            "conversation_id": str(convo_id) if convo_id else None,
            "filename": name,
            "mime": kind,
            "size": int(byte_size),
            "object_uri": uri,
            "source_surface": surface,
            "status": status_out,
            "sha256": sha256_out,
            "created_at": str(created_at),
            "completed_at": str(completed_at) if completed_at else None,
        }

    async def get_artifact(self, artifact_id: UUID) -> dict[str, Any] | None:
        q = """
        SELECT id, owner_id, client_id, conversation_id, filename, mime, size, object_uri, source_surface,
               status, sha256, created_at, completed_at, source_kind, repo_name, repo_ref, file_path, ingestion_id
        FROM artifacts
        WHERE id = %s;
        """
        async with self.pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(q, (artifact_id,))
                row = await cur.fetchone()

        if row is None:
            return None

        (
            aid,
            owner,
            c_id,
            convo_id,
            name,
            kind,
            byte_size,
            uri,
            surface,
            status,
            sha256,
            created_at,
            completed_at,
            source_kind,
            repo_name,
            repo_ref,
            file_path,
            ingestion_id,
        ) = row
        return {
            "artifact_id": str(aid),
            "owner_id": owner,
            "client_id": c_id,
            "conversation_id": str(convo_id) if convo_id else None,
            "filename": name,
            "mime": kind,
            "size": int(byte_size),
            "object_uri": uri,
            "source_surface": surface,
            "status": status,
            "sha256": sha256,
            "created_at": str(created_at),
            "completed_at": str(completed_at) if completed_at else None,
            "source_kind": source_kind,
            "repo_name": repo_name,
            "repo_ref": repo_ref,
            "file_path": file_path,
            "ingestion_id": str(ingestion_id) if ingestion_id else None,
        }

    async def create_derived_text(
        self,
        *,
        artifact_id: UUID,
        kind: str,
        text: str,
        language: str | None,
        derivation_params: dict[str, Any] | None,
    ) -> dict[str, Any]:
        q = """
        INSERT INTO derived_text (artifact_id, kind, language, text, derivation_params)
        VALUES (%s, %s, %s, %s, %s::jsonb)
        RETURNING id, artifact_id, kind, language, text, derivation_params, created_at;
        """
        async with self.pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(q, (artifact_id, kind, language, text, Json(derivation_params or {})))
                row = await cur.fetchone()
        return {
            "derived_text_id": str(row[0]),
            "artifact_id": str(row[1]),
            "kind": row[2],
            "language": row[3],
            "text": row[4],
            "derivation_params": row[5] or {},
            "created_at": str(row[6]),
        }

    async def create_embedding_ref(
        self,
        *,
        ref_type: str,
        ref_id: UUID,
        model: str,
        qdrant_point_id: str,
    ) -> dict[str, Any]:
        q = """
        INSERT INTO embeddings (ref_type, ref_id, model, qdrant_point_id)
        VALUES (%s, %s, %s, %s)
        RETURNING id;
        """
        async with self.pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(q, (ref_type, ref_id, model, qdrant_point_id))
                row = await cur.fetchone()
        return {"embedding_id": str(row[0])}

    async def get_derived_text_snippets_by_ids(self, ids: list[UUID]) -> list[dict[str, Any]]:
        if not ids:
            return []
        id_strs = [str(i) for i in ids]
        q = """
        SELECT dt.id, dt.artifact_id, dt.text, dt.derivation_params, dt.created_at, a.file_path, a.repo_name, a.mime
        FROM derived_text dt
        JOIN artifacts a ON a.id = dt.artifact_id
        WHERE dt.id = ANY(%s);
        """
        async with self.pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(q, (id_strs,))
                rows = await cur.fetchall()
        by_id: dict[str, dict[str, Any]] = {}
        for row in rows:
            by_id[str(row[0])] = {
                "derived_text_id": str(row[0]),
                "artifact_id": str(row[1]),
                "text": row[2],
                "derivation_params": row[3] or {},
                "created_at": str(row[4]),
                "file_path": row[5] or "",
                "repo_name": row[6],
                "mime": row[7],
            }
        return [by_id[item] for item in id_strs if item in by_id]

    async def get_recent_message_snippets(self, conversation_id: UUID, limit: int = 10) -> list[dict[str, Any]]:
        q = """
        SELECT id, conversation_id, role, content, created_at
        FROM messages
        WHERE conversation_id = %s
        ORDER BY created_at DESC
        LIMIT %s;
        """
        async with self.pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(q, (conversation_id, limit))
                rows = await cur.fetchall()

        rows.reverse()
        return [
            {
                "message_id": str(mid),
                "conversation_id": str(cid),
                "role": role,
                "content": content,
                "created_at": str(created_at),
            }
            for (mid, cid, role, content, created_at) in rows
        ]

    async def get_pinned_memories(
        self,
        owner_id: str,
        conversation_id: UUID | None = None,
        limit: int = 5,
    ) -> list[dict[str, Any]]:
        params: list[Any] = [owner_id]
        where = "WHERE owner_id = %s"
        if conversation_id is not None:
            where += " AND (conversation_id = %s OR conversation_id IS NULL)"
            params.append(conversation_id)

        q = f"""
        SELECT id, content, metadata
        FROM pinned_memories
        {where}
        ORDER BY created_at DESC
        LIMIT %s;
        """
        params.append(limit)

        async with self.pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(q, tuple(params))
                rows = await cur.fetchall()

        return [
            {
                "id": str(pid),
                "content": content,
                "metadata": metadata or {},
            }
            for (pid, content, metadata) in rows
        ]

    async def get_pinned_memories_for_hygiene(self, owner_id: str, limit: int = 50) -> list[dict[str, Any]]:
        q = """
        SELECT id, conversation_id, content, metadata, created_at
        FROM pinned_memories
        WHERE owner_id = %s
        ORDER BY created_at DESC
        LIMIT %s;
        """
        async with self.pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(q, (owner_id, limit))
                rows = await cur.fetchall()
        return [
            {
                "id": str(row[0]),
                "conversation_id": str(row[1]) if row[1] else None,
                "content": row[2],
                "metadata": row[3] or {},
                "created_at": str(row[4]),
            }
            for row in rows
        ]

    async def get_policy_overlays(self, owner_id: str, surface: str | None = None) -> list[dict[str, Any]]:
        q = """
        SELECT id, policy_json
        FROM policy_overlays
        WHERE owner_id = %s
          AND (surface = %s OR surface IS NULL)
        ORDER BY created_at DESC
        LIMIT 5;
        """
        async with self.pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(q, (owner_id, surface))
                rows = await cur.fetchall()

        return [{"id": str(pid), "content": "policy", "metadata": payload or {}} for (pid, payload) in rows]

    async def get_persona_overlays(self, owner_id: str, surface: str | None = None) -> list[dict[str, Any]]:
        q = """
        SELECT id, persona_json
        FROM persona_overlays
        WHERE owner_id = %s
          AND (surface = %s OR surface IS NULL)
        ORDER BY created_at DESC
        LIMIT 5;
        """
        async with self.pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(q, (owner_id, surface))
                rows = await cur.fetchall()

        return [{"id": str(pid), "content": "persona", "metadata": payload or {}} for (pid, payload) in rows]

    async def create_hygiene_flag(
        self,
        *,
        owner_id: str,
        subject_type: str,
        subject_id: UUID | None,
        flag_type: str,
        details: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        details_payload = details or {}
        q_existing = """
        SELECT id, owner_id, subject_type, subject_id, flag_type, details_json, status, created_at, resolved_at
        FROM memory_hygiene_flags
        WHERE owner_id = %s
          AND subject_type = %s
          AND subject_id IS NOT DISTINCT FROM %s
          AND flag_type = %s
          AND details_json = %s::jsonb
          AND status = 'open'
        ORDER BY created_at DESC
        LIMIT 1;
        """
        q_insert = """
        INSERT INTO memory_hygiene_flags (owner_id, subject_type, subject_id, flag_type, details_json)
        VALUES (%s, %s, %s, %s, %s::jsonb)
        RETURNING id, owner_id, subject_type, subject_id, flag_type, details_json, status, created_at, resolved_at;
        """
        async with self.pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    q_existing,
                    (owner_id, subject_type, subject_id, flag_type, Json(details_payload)),
                )
                row = await cur.fetchone()
                created = False
                if row is None:
                    await cur.execute(
                        q_insert,
                        (owner_id, subject_type, subject_id, flag_type, Json(details_payload)),
                    )
                    row = await cur.fetchone()
                    created = True
        return {
            "flag_id": str(row[0]),
            "owner_id": row[1],
            "subject_type": row[2],
            "subject_id": str(row[3]) if row[3] else None,
            "flag_type": row[4],
            "details": row[5] or {},
            "status": row[6],
            "created_at": str(row[7]),
            "resolved_at": str(row[8]) if row[8] else None,
            "created": created,
        }

    async def list_hygiene_flags(
        self,
        *,
        owner_id: str,
        status: str | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        params: list[Any] = [owner_id]
        where = "WHERE owner_id = %s"
        if status is not None:
            where += " AND status = %s"
            params.append(status)
        q = f"""
        SELECT id, owner_id, subject_type, subject_id, flag_type, details_json, status, created_at, resolved_at
        FROM memory_hygiene_flags
        {where}
        ORDER BY created_at DESC
        LIMIT %s;
        """
        params.append(limit)
        async with self.pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(q, tuple(params))
                rows = await cur.fetchall()
        return [
            {
                "flag_id": str(row[0]),
                "owner_id": row[1],
                "subject_type": row[2],
                "subject_id": str(row[3]) if row[3] else None,
                "flag_type": row[4],
                "details": row[5] or {},
                "status": row[6],
                "created_at": str(row[7]),
                "resolved_at": str(row[8]) if row[8] else None,
            }
            for row in rows
        ]

    async def write_trace(
        self,
        request_id: str,
        conversation_id: UUID | None,
        owner_id: str | None,
        surface: str | None,
        router_decision: dict[str, Any] | None,
        retrieval: dict[str, Any] | None,
        model_calls: dict[str, Any] | None,
        cost: dict[str, Any] | None,
        latency_ms: int | None,
    ) -> str:
        trace_id = await self.create_trace(
            {
                "request_id": request_id,
                "conversation_id": conversation_id,
                "owner_id": owner_id or "",
                "surface": surface or "unknown",
                "profile": {},
                "retrieval": retrieval or {},
                "router_decision": router_decision or {},
                "manual_override": {},
                "model_call": model_calls or {},
                "fallback": {},
                "cost": cost or {},
                "latency_ms": latency_ms,
                "status": "ok",
                "error": None,
            }
        )
        return str(trace_id)

    async def ping(self) -> None:
        async with self.pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute("SELECT 1;")
                await cur.fetchone()

    async def get_recent_message_items(self, conversation_id: UUID, limit: int = 10) -> list[dict[str, Any]]:
        q = """
        SELECT id, conversation_id, role, content, created_at
        FROM messages
        WHERE conversation_id = %s
        ORDER BY created_at DESC
        LIMIT %s;
        """
        async with self.pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(q, (conversation_id, limit))
                rows = await cur.fetchall()

        rows.reverse()
        return [
            {
                "message_id": str(r[0]),
                "conversation_id": str(r[1]),
                "role": r[2],
                "content": r[3],
                "created_at": str(r[4]),
            }
            for r in rows
        ]

    async def resolve_profile(
        self,
        owner_id: str,
        surface: str,
        requested_profile: str | None = None,
        client_id: str | None = None,
        default_profile_name: str = "dev",
    ) -> dict[str, Any]:
        client_key = client_id or ""

        if requested_profile:
            q = """
            SELECT profile_name, profile_version, prompt_overlay, retrieval_policy_json,
                   routing_policy_json, response_style_json, safety_policy_json, tool_policy_json
            FROM profiles
            WHERE owner_id = %s AND profile_name = %s AND active = true
            ORDER BY profile_version DESC
            LIMIT 1;
            """
            async with self.pool.connection() as conn:
                async with conn.cursor() as cur:
                    await cur.execute(q, (owner_id, requested_profile))
                    row = await cur.fetchone()
            if row:
                return {
                    "profile_name": row[0],
                    "source": "requested",
                    "profile_version": row[1],
                    "effective_profile_ref": f"{owner_id}:{row[0]}:{row[1]}",
                    "prompt_overlay": row[2] or "",
                    "retrieval_policy": row[3] or {},
                    "routing_policy": row[4] or {},
                    "response_style": row[5] or {},
                    "safety_policy": row[6] or {},
                    "tool_policy": row[7] or {},
                }

        q_surface = """
        SELECT p.profile_name, p.profile_version, p.prompt_overlay, p.retrieval_policy_json,
               p.routing_policy_json, p.response_style_json, p.safety_policy_json, p.tool_policy_json
        FROM surface_profile_defaults spd
        JOIN profiles p
          ON p.owner_id = spd.owner_id
         AND p.profile_name = spd.profile_name
         AND p.active = true
        WHERE spd.owner_id = %s
          AND spd.surface = %s
          AND spd.client_id = %s
        ORDER BY p.profile_version DESC
        LIMIT 1;
        """
        async with self.pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(q_surface, (owner_id, surface, client_key))
                row = await cur.fetchone()
        if row:
            return {
                "profile_name": row[0],
                "source": "surface_default",
                "profile_version": row[1],
                "effective_profile_ref": f"{owner_id}:{row[0]}:{row[1]}",
                "prompt_overlay": row[2] or "",
                "retrieval_policy": row[3] or {},
                "routing_policy": row[4] or {},
                "response_style": row[5] or {},
                "safety_policy": row[6] or {},
                "tool_policy": row[7] or {},
            }

        q_global = """
        SELECT profile_name, profile_version, prompt_overlay, retrieval_policy_json,
               routing_policy_json, response_style_json, safety_policy_json, tool_policy_json
        FROM profiles
        WHERE owner_id = %s AND profile_name = %s AND active = true
        ORDER BY profile_version DESC
        LIMIT 1;
        """
        async with self.pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(q_global, (owner_id, default_profile_name))
                row = await cur.fetchone()
        if row:
            return {
                "profile_name": row[0],
                "source": "global_default",
                "profile_version": row[1],
                "effective_profile_ref": f"{owner_id}:{row[0]}:{row[1]}",
                "prompt_overlay": row[2] or "",
                "retrieval_policy": row[3] or {},
                "routing_policy": row[4] or {},
                "response_style": row[5] or {},
                "safety_policy": row[6] or {},
                "tool_policy": row[7] or {},
            }

        return {
            "profile_name": default_profile_name,
            "source": "global_default",
            "profile_version": 1,
            "effective_profile_ref": f"{owner_id}:{default_profile_name}:1",
            "prompt_overlay": "",
            "retrieval_policy": {},
            "routing_policy": {},
            "response_style": {},
            "safety_policy": {},
            "tool_policy": {},
        }

    async def create_trace(self, trace: dict[str, Any]) -> UUID:
        q = """
        INSERT INTO traces (
            request_id, conversation_id, owner_id, client_id, surface,
            profile_json, retrieval_json, router_decision_json, manual_override_json,
            model_call_json, fallback_json, cost_json, latency_ms, status, error_text
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (request_id) DO UPDATE
            SET conversation_id = EXCLUDED.conversation_id,
                owner_id = EXCLUDED.owner_id,
                client_id = EXCLUDED.client_id,
                surface = EXCLUDED.surface,
                profile_json = EXCLUDED.profile_json,
                retrieval_json = EXCLUDED.retrieval_json,
                router_decision_json = EXCLUDED.router_decision_json,
                manual_override_json = EXCLUDED.manual_override_json,
                model_call_json = EXCLUDED.model_call_json,
                fallback_json = EXCLUDED.fallback_json,
                cost_json = EXCLUDED.cost_json,
                latency_ms = EXCLUDED.latency_ms,
                status = EXCLUDED.status,
                error_text = EXCLUDED.error_text
        RETURNING id;
        """
        async with self.pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    q,
                    (
                        trace["request_id"],
                        trace["conversation_id"],
                        trace["owner_id"],
                        trace.get("client_id"),
                        trace["surface"],
                        Json(trace.get("profile", {})),
                        Json(trace.get("retrieval", {})),
                        Json(trace.get("router_decision", {})),
                        Json(trace.get("manual_override", {})),
                        Json(trace.get("model_call", {})),
                        Json(trace.get("fallback", {})),
                        Json(trace.get("cost", {})),
                        trace.get("latency_ms"),
                        trace["status"],
                        trace.get("error"),
                    ),
                )
                row = await cur.fetchone()
                return row[0]

    async def get_trace_by_request_id(self, request_id: str) -> dict[str, Any] | None:
        q = """
        SELECT id, request_id, conversation_id, owner_id, client_id, surface,
               profile_json, retrieval_json, router_decision_json, manual_override_json,
               model_call_json, fallback_json, cost_json, latency_ms, status, error_text, created_at
        FROM traces
        WHERE request_id = %s
        LIMIT 1;
        """
        async with self.pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(q, (request_id,))
                row = await cur.fetchone()
        if not row:
            return None
        return {
            "trace_id": str(row[0]),
            "request_id": row[1],
            "conversation_id": str(row[2]),
            "owner_id": row[3],
            "client_id": row[4],
            "surface": row[5],
            "profile": row[6] or {},
            "retrieval": row[7] or {},
            "router_decision": row[8] or {},
            "manual_override": row[9] or {},
            "model_call": row[10] or {},
            "fallback": row[11] or {},
            "cost": row[12] or {},
            "latency_ms": row[13],
            "status": row[14],
            "error": row[15],
            "created_at": str(row[16]),
        }
