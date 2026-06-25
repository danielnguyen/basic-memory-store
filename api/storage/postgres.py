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

    async def create_initiative_event(
        self,
        *,
        owner_id: str,
        request_id: str,
        source_event_log_id: UUID | None,
        trigger_type: str,
        trigger_ref_json: dict[str, Any],
        payload_json: dict[str, Any],
    ) -> dict[str, Any]:
        q = """
        INSERT INTO initiative_events (
            owner_id, request_id, source_event_log_id, trigger_type, trigger_ref_json, payload_json
        )
        VALUES (%s, %s, %s, %s, %s, %s)
        ON CONFLICT (owner_id, request_id) DO UPDATE
            SET source_event_log_id = EXCLUDED.source_event_log_id,
                trigger_type = EXCLUDED.trigger_type,
                trigger_ref_json = EXCLUDED.trigger_ref_json,
                payload_json = EXCLUDED.payload_json
        RETURNING id, owner_id, request_id, source_event_log_id, trigger_type,
                  trigger_ref_json, payload_json, created_at;
        """
        async with self.pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    q,
                    (
                        owner_id,
                        request_id,
                        source_event_log_id,
                        trigger_type,
                        Json(trigger_ref_json),
                        Json(payload_json),
                    ),
                )
                row = await cur.fetchone()
        return {
            "initiative_event_id": str(row[0]),
            "owner_id": row[1],
            "request_id": row[2],
            "source_event_log_id": str(row[3]) if row[3] else None,
            "trigger_type": row[4],
            "trigger_ref_json": row[5] or {},
            "payload_json": row[6] or {},
            "created_at": str(row[7]),
        }

    async def create_initiative_decision(
        self,
        *,
        initiative_event_id: UUID,
        owner_id: str,
        proactive_suggestion_id: UUID | None,
        decision_status: str,
        score: float | None,
        reason_json: dict[str, Any],
        delivery_surface: str | None,
        delivery_status: str,
        suppression_reason: str | None,
        cooldown_identity_key: str | None,
        normalized_subject: str | None,
        cooldown_until: str | None,
    ) -> dict[str, Any]:
        q = """
        INSERT INTO initiative_decisions (
            initiative_event_id, owner_id, proactive_suggestion_id, decision_status, score,
            reason_json, delivery_surface, delivery_status, suppression_reason,
            cooldown_identity_key, normalized_subject, cooldown_until
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        RETURNING id, initiative_event_id, owner_id, proactive_suggestion_id, decision_status,
                  score, reason_json, delivery_surface, delivery_status, suppression_reason,
                  cooldown_identity_key, normalized_subject, cooldown_until, created_at;
        """
        async with self.pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    q,
                    (
                        initiative_event_id,
                        owner_id,
                        proactive_suggestion_id,
                        decision_status,
                        score,
                        Json(reason_json),
                        delivery_surface,
                        delivery_status,
                        suppression_reason,
                        cooldown_identity_key,
                        normalized_subject,
                        cooldown_until,
                    ),
                )
                row = await cur.fetchone()
        return self._initiative_decision_from_row(row)

    def _initiative_decision_from_row(self, row: Any) -> dict[str, Any]:
        return {
            "decision_id": str(row[0]),
            "initiative_event_id": str(row[1]),
            "owner_id": row[2],
            "proactive_suggestion_id": str(row[3]) if row[3] else None,
            "decision_status": row[4],
            "score": float(row[5]) if row[5] is not None else None,
            "reason_json": row[6] or {},
            "delivery_surface": row[7],
            "delivery_status": row[8],
            "suppression_reason": row[9],
            "cooldown_identity_key": row[10],
            "normalized_subject": row[11],
            "cooldown_until": str(row[12]) if row[12] else None,
            "created_at": str(row[13]),
        }

    async def get_initiative_event(self, initiative_event_id: UUID) -> dict[str, Any] | None:
        q = """
        SELECT id, owner_id, request_id, source_event_log_id, trigger_type,
               trigger_ref_json, payload_json, created_at
        FROM initiative_events
        WHERE id = %s
        LIMIT 1;
        """
        async with self.pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(q, (initiative_event_id,))
                row = await cur.fetchone()
        if row is None:
            return None
        return {
            "initiative_event_id": str(row[0]),
            "owner_id": row[1],
            "request_id": row[2],
            "source_event_log_id": str(row[3]) if row[3] else None,
            "trigger_type": row[4],
            "trigger_ref_json": row[5] or {},
            "payload_json": row[6] or {},
            "created_at": str(row[7]),
        }

    async def get_initiative_event_by_request_id(
        self,
        *,
        owner_id: str,
        request_id: str,
    ) -> dict[str, Any] | None:
        q = """
        SELECT id, owner_id, request_id, source_event_log_id, trigger_type,
               trigger_ref_json, payload_json, created_at
        FROM initiative_events
        WHERE owner_id = %s AND request_id = %s
        ORDER BY created_at DESC, id DESC
        LIMIT 1;
        """
        async with self.pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(q, (owner_id, request_id))
                row = await cur.fetchone()
        if row is None:
            return None
        return {
            "initiative_event_id": str(row[0]),
            "owner_id": row[1],
            "request_id": row[2],
            "source_event_log_id": str(row[3]) if row[3] else None,
            "trigger_type": row[4],
            "trigger_ref_json": row[5] or {},
            "payload_json": row[6] or {},
            "created_at": str(row[7]),
        }

    async def list_initiative_decisions(self, initiative_event_id: UUID) -> list[dict[str, Any]]:
        q = """
        SELECT id, initiative_event_id, owner_id, proactive_suggestion_id, decision_status,
               score, reason_json, delivery_surface, delivery_status, suppression_reason,
               cooldown_identity_key, normalized_subject, cooldown_until, created_at
        FROM initiative_decisions
        WHERE initiative_event_id = %s
        ORDER BY created_at DESC, id DESC;
        """
        async with self.pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(q, (initiative_event_id,))
                rows = await cur.fetchall()
        return [self._initiative_decision_from_row(row) for row in rows]

    async def get_initiative_decision(self, decision_id: UUID) -> dict[str, Any] | None:
        q = """
        SELECT id, initiative_event_id, owner_id, proactive_suggestion_id, decision_status,
               score, reason_json, delivery_surface, delivery_status, suppression_reason,
               cooldown_identity_key, normalized_subject, cooldown_until, created_at
        FROM initiative_decisions
        WHERE id = %s
        LIMIT 1;
        """
        async with self.pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(q, (decision_id,))
                row = await cur.fetchone()
        if row is None:
            return None
        return self._initiative_decision_from_row(row)

    async def get_recent_initiative_cooldown(
        self,
        *,
        owner_id: str,
        cooldown_identity_key: str,
        cooldown_hours: float,
    ) -> dict[str, Any] | None:
        q = """
        SELECT id, initiative_event_id, owner_id, proactive_suggestion_id, decision_status,
               score, reason_json, delivery_surface, delivery_status, suppression_reason,
               cooldown_identity_key, normalized_subject,
               COALESCE(cooldown_until, created_at + (%s * interval '1 hour')) AS cooldown_until,
               created_at
        FROM initiative_decisions
        WHERE owner_id = %s
          AND cooldown_identity_key = %s
          AND decision_status = 'created'
          AND created_at > now() - (%s * interval '1 hour')
        ORDER BY created_at DESC, id DESC
        LIMIT 1;
        """
        async with self.pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(q, (cooldown_hours, owner_id, cooldown_identity_key, cooldown_hours))
                row = await cur.fetchone()
        if row is None:
            return None
        return self._initiative_decision_from_row(row)

    async def get_recent_negative_initiative_feedback(
        self,
        *,
        owner_id: str,
        cooldown_identity_key: str,
        lookback_days: float,
    ) -> dict[str, Any] | None:
        q = """
        SELECT f.id, f.decision_id, f.proactive_feedback_id, f.owner_id,
               f.feedback_type, f.feedback_json, f.created_at
        FROM initiative_feedback f
        JOIN initiative_decisions d ON d.id = f.decision_id
        WHERE f.owner_id = %s
          AND d.cooldown_identity_key = %s
          AND f.feedback_type IN ('dismissed', 'not_useful')
          AND f.created_at > now() - (%s * interval '1 day')
        ORDER BY f.created_at DESC, f.id DESC
        LIMIT 1;
        """
        async with self.pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(q, (owner_id, cooldown_identity_key, lookback_days))
                row = await cur.fetchone()
        if row is None:
            return None
        return {
            "feedback_id": str(row[0]),
            "decision_id": str(row[1]),
            "proactive_feedback_id": str(row[2]) if row[2] else None,
            "owner_id": row[3],
            "feedback_type": row[4],
            "feedback_json": row[5] or {},
            "created_at": str(row[6]),
        }

    async def record_initiative_feedback(
        self,
        *,
        decision_id: UUID,
        proactive_feedback_id: UUID | None,
        owner_id: str,
        feedback_type: str,
        feedback_json: dict[str, Any],
    ) -> dict[str, Any]:
        q = """
        INSERT INTO initiative_feedback (
            decision_id, proactive_feedback_id, owner_id, feedback_type, feedback_json
        )
        VALUES (%s, %s, %s, %s, %s)
        RETURNING id, decision_id, proactive_feedback_id, owner_id,
                  feedback_type, feedback_json, created_at;
        """
        async with self.pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    q,
                    (decision_id, proactive_feedback_id, owner_id, feedback_type, Json(feedback_json)),
                )
                row = await cur.fetchone()
        return {
            "feedback_id": str(row[0]),
            "decision_id": str(row[1]),
            "proactive_feedback_id": str(row[2]) if row[2] else None,
            "owner_id": row[3],
            "feedback_type": row[4],
            "feedback_json": row[5] or {},
            "created_at": str(row[6]),
        }

    async def list_initiative_feedback_for_event(
        self,
        initiative_event_id: UUID,
    ) -> list[dict[str, Any]]:
        q = """
        SELECT f.id, f.decision_id, f.proactive_feedback_id, f.owner_id,
               f.feedback_type, f.feedback_json, f.created_at
        FROM initiative_feedback f
        JOIN initiative_decisions d ON d.id = f.decision_id
        WHERE d.initiative_event_id = %s
        ORDER BY f.created_at DESC, f.id DESC;
        """
        async with self.pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(q, (initiative_event_id,))
                rows = await cur.fetchall()
        return [
            {
                "feedback_id": str(row[0]),
                "decision_id": str(row[1]),
                "proactive_feedback_id": str(row[2]) if row[2] else None,
                "owner_id": row[3],
                "feedback_type": row[4],
                "feedback_json": row[5] or {},
                "created_at": str(row[6]),
            }
            for row in rows
        ]

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
        SELECT id, conversation_id, role, content, metadata, created_at
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
                "metadata": r[4] or {},
                "created_at": str(r[5]),
            }
            for r in rows
        ]

    async def get_memory_items_for_source_refs(
        self,
        *,
        owner_id: str,
        source_refs: list[dict[str, str]],
    ) -> dict[tuple[str, str], dict[str, Any]]:
        normalized_refs: list[tuple[str, str]] = []
        seen: set[tuple[str, str]] = set()
        for ref in source_refs:
            ref_type = str(ref.get("ref_type") or "").strip()
            ref_id = str(ref.get("ref_id") or "").strip()
            if not ref_type or not ref_id:
                continue
            key = (ref_type, ref_id)
            if key in seen:
                continue
            seen.add(key)
            normalized_refs.append(key)

        if not normalized_refs:
            return {}

        select_cols = """
            id, owner_id, memory_type, summary, source_refs_json, source_ref_hash,
            scores_json, promotion_state, status, supersedes_memory_id,
            superseded_by_memory_id, last_reinforced_at, expires_at,
            derivation_version, confidence, explanation_json, generation_trace_id,
            created_at, updated_at
        """
        predicates = " OR ".join(["source_refs_json @> %s::jsonb"] * len(normalized_refs))
        q = f"""
        SELECT {select_cols}
        FROM memory_items
        WHERE owner_id = %s
          AND ({predicates})
        ORDER BY updated_at DESC, created_at DESC;
        """
        params: list[Any] = [owner_id]
        params.extend(Json([{"ref_type": ref_type, "ref_id": ref_id}]) for ref_type, ref_id in normalized_refs)

        async with self.pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(q, tuple(params))
                rows = await cur.fetchall()

        by_ref: dict[tuple[str, str], dict[str, Any]] = {}
        wanted = set(normalized_refs)
        for row in rows:
            item = self._memory_item_from_row(row)
            for ref in item.get("source_refs_json") or []:
                ref_type = str(ref.get("ref_type") or "").strip()
                ref_id = str(ref.get("ref_id") or "").strip()
                key = (ref_type, ref_id)
                if key in wanted and key not in by_ref:
                    by_ref[key] = item
        return by_ref

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


    def _memory_item_from_row(self, row: tuple[Any, ...]) -> dict[str, Any]:
        return {
            "memory_id": str(row[0]),
            "owner_id": row[1],
            "memory_type": row[2],
            "summary": row[3],
            "source_refs_json": row[4] or [],
            "source_ref_hash": row[5],
            "scores_json": row[6] or {},
            "promotion_state": row[7],
            "status": row[8],
            "supersedes_memory_id": str(row[9]) if row[9] else None,
            "superseded_by_memory_id": str(row[10]) if row[10] else None,
            "last_reinforced_at": str(row[11]) if row[11] else None,
            "expires_at": str(row[12]) if row[12] else None,
            "derivation_version": row[13],
            "confidence": row[14],
            "explanation_json": row[15] or {},
            "generation_trace_id": row[16],
            "created_at": str(row[17]),
            "updated_at": str(row[18]),
        }

    def _memory_event_from_row(self, row: tuple[Any, ...]) -> dict[str, Any]:
        return {
            "event_id": str(row[0]),
            "memory_id": str(row[1]),
            "owner_id": row[2],
            "event_type": row[3],
            "reason_json": row[4] or {},
            "created_at": str(row[5]),
        }

    async def promote_memory_item(
        self,
        *,
        owner_id: str,
        memory_type: str,
        summary: str,
        source_refs_json: list[dict[str, Any]],
        source_ref_hash: str,
        scores_json: dict[str, Any],
        promotion_state: str,
        confidence: float | None,
        explanation_json: dict[str, Any],
        generation_trace_id: str | None,
        expires_at: str | None,
        request_id: str,
        reinforce: bool,
        supersedes_memory_id: UUID | None,
        derivation_version: str = "r20-mvp-v1",
    ) -> dict[str, Any]:
        select_cols = """
            id, owner_id, memory_type, summary, source_refs_json, source_ref_hash,
            scores_json, promotion_state, status, supersedes_memory_id,
            superseded_by_memory_id, last_reinforced_at, expires_at,
            derivation_version, confidence, explanation_json, generation_trace_id,
            created_at, updated_at
        """
        events_appended: list[str] = []
        incoming_for_compare = {
            "memory_type": memory_type,
            "summary": summary,
            "source_refs_json": source_refs_json,
            "scores_json": scores_json,
            "promotion_state": promotion_state,
            "expires_at": expires_at,
            "confidence": confidence,
            "explanation_json": explanation_json,
            "generation_trace_id": generation_trace_id,
        }

        async with self.pool.connection() as conn:
            async with conn.cursor() as cur:
                if supersedes_memory_id is not None:
                    new_id = uuid4()
                    await cur.execute(
                        f"""
                        SELECT {select_cols}
                        FROM memory_items
                        WHERE id = %s AND owner_id = %s
                        FOR UPDATE;
                        """,
                        (supersedes_memory_id, owner_id),
                    )
                    old_row = await cur.fetchone()
                    if old_row is None:
                        raise KeyError("superseded memory not found")

                    await cur.execute(
                        """
                        UPDATE memory_items
                        SET status = 'superseded',
                            superseded_by_memory_id = %s,
                            updated_at = now()
                        WHERE id = %s AND owner_id = %s;
                        """,
                        (new_id, supersedes_memory_id, owner_id),
                    )
                    await cur.execute(
                        """
                        INSERT INTO memory_events (memory_id, owner_id, event_type, reason_json)
                        VALUES (%s, %s, 'superseded', %s::jsonb);
                        """,
                        (
                            supersedes_memory_id,
                            owner_id,
                            Json({"request_id": request_id, "superseded_by_memory_id": str(new_id)}),
                        ),
                    )

                    await cur.execute(
                        f"""
                        INSERT INTO memory_items (
                            id, owner_id, memory_type, summary, source_refs_json,
                            source_ref_hash, scores_json, promotion_state, status,
                            supersedes_memory_id, expires_at, derivation_version,
                            confidence, explanation_json, generation_trace_id
                        ) VALUES (%s, %s, %s, %s, %s::jsonb, %s, %s::jsonb, %s,
                                  'active', %s, %s, %s, %s, %s::jsonb, %s)
                        RETURNING {select_cols};
                        """,
                        (
                            new_id,
                            owner_id,
                            memory_type,
                            summary,
                            Json(source_refs_json),
                            source_ref_hash,
                            Json(scores_json),
                            promotion_state,
                            supersedes_memory_id,
                            expires_at,
                            derivation_version,
                            confidence,
                            Json(explanation_json),
                            generation_trace_id,
                        ),
                    )
                    row = await cur.fetchone()
                    for event_type in ("created", "promoted"):
                        await cur.execute(
                            """
                            INSERT INTO memory_events (memory_id, owner_id, event_type, reason_json)
                            VALUES (%s, %s, %s, %s::jsonb);
                            """,
                            (new_id, owner_id, event_type, Json({"request_id": request_id})),
                        )
                    return {
                        "memory": self._memory_item_from_row(row),
                        "created": True,
                        "updated": False,
                        "reinforced": False,
                        "superseded": True,
                        "events_appended": ["superseded", "created", "promoted"],
                    }

                await cur.execute(
                    f"""
                    SELECT {select_cols}
                    FROM memory_items
                    WHERE owner_id = %s AND source_ref_hash = %s AND status = 'active'
                    LIMIT 1
                    FOR UPDATE;
                    """,
                    (owner_id, source_ref_hash),
                )
                existing_row = await cur.fetchone()

                if existing_row is None:
                    await cur.execute(
                        f"""
                        INSERT INTO memory_items (
                            owner_id, memory_type, summary, source_refs_json,
                            source_ref_hash, scores_json, promotion_state, status,
                            expires_at, derivation_version, confidence,
                            explanation_json, generation_trace_id,
                            last_reinforced_at
                        ) VALUES (%s, %s, %s, %s::jsonb, %s, %s::jsonb, %s,
                                  'active', %s, %s, %s, %s::jsonb, %s,
                                  CASE WHEN %s THEN now() ELSE NULL END)
                        RETURNING {select_cols};
                        """,
                        (
                            owner_id,
                            memory_type,
                            summary,
                            Json(source_refs_json),
                            source_ref_hash,
                            Json(scores_json),
                            promotion_state,
                            expires_at,
                            derivation_version,
                            confidence,
                            Json(explanation_json),
                            generation_trace_id,
                            reinforce,
                        ),
                    )
                    row = await cur.fetchone()
                    memory_id = row[0]
                    for event_type in ("created", "promoted"):
                        await cur.execute(
                            """
                            INSERT INTO memory_events (memory_id, owner_id, event_type, reason_json)
                            VALUES (%s, %s, %s, %s::jsonb);
                            """,
                            (memory_id, owner_id, event_type, Json({"request_id": request_id})),
                        )
                        events_appended.append(event_type)
                    if reinforce:
                        await cur.execute(
                            """
                            INSERT INTO memory_events (memory_id, owner_id, event_type, reason_json)
                            VALUES (%s, %s, 'reinforced', %s::jsonb);
                            """,
                            (memory_id, owner_id, Json({"request_id": request_id, "source": "promote"})),
                        )
                        events_appended.append("reinforced")
                    return {
                        "memory": self._memory_item_from_row(row),
                        "created": True,
                        "updated": False,
                        "reinforced": reinforce,
                        "superseded": False,
                        "events_appended": events_appended,
                    }

                existing = self._memory_item_from_row(existing_row)
                changed = any(existing.get(k) != v for k, v in incoming_for_compare.items())
                memory_id = existing_row[0]
                if changed:
                    await cur.execute(
                        f"""
                        UPDATE memory_items
                        SET memory_type = %s,
                            summary = %s,
                            source_refs_json = %s::jsonb,
                            scores_json = %s::jsonb,
                            promotion_state = %s,
                            expires_at = %s,
                            confidence = %s,
                            explanation_json = %s::jsonb,
                            generation_trace_id = %s,
                            updated_at = now()
                        WHERE id = %s AND owner_id = %s
                        RETURNING {select_cols};
                        """,
                        (
                            memory_type,
                            summary,
                            Json(source_refs_json),
                            Json(scores_json),
                            promotion_state,
                            expires_at,
                            confidence,
                            Json(explanation_json),
                            generation_trace_id,
                            memory_id,
                            owner_id,
                        ),
                    )
                    row = await cur.fetchone()
                    await cur.execute(
                        """
                        INSERT INTO memory_events (memory_id, owner_id, event_type, reason_json)
                        VALUES (%s, %s, 'updated', %s::jsonb);
                        """,
                        (memory_id, owner_id, Json({"request_id": request_id, "source": "promote"})),
                    )
                    events_appended.append("updated")
                else:
                    row = existing_row

                if reinforce:
                    await cur.execute(
                        f"""
                        UPDATE memory_items
                        SET last_reinforced_at = now(),
                            updated_at = now()
                        WHERE id = %s AND owner_id = %s
                        RETURNING {select_cols};
                        """,
                        (memory_id, owner_id),
                    )
                    row = await cur.fetchone()
                    await cur.execute(
                        """
                        INSERT INTO memory_events (memory_id, owner_id, event_type, reason_json)
                        VALUES (%s, %s, 'reinforced', %s::jsonb);
                        """,
                        (memory_id, owner_id, Json({"request_id": request_id, "source": "promote"})),
                    )
                    events_appended.append("reinforced")

                return {
                    "memory": self._memory_item_from_row(row),
                    "created": False,
                    "updated": changed,
                    "reinforced": reinforce,
                    "superseded": False,
                    "events_appended": events_appended,
                }

    async def reinforce_memory_item(
        self,
        *,
        memory_id: UUID,
        owner_id: str,
        scores_json: dict[str, Any],
        reason_json: dict[str, Any],
        request_id: str,
    ) -> dict[str, Any] | None:
        select_cols = """
            id, owner_id, memory_type, summary, source_refs_json, source_ref_hash,
            scores_json, promotion_state, status, supersedes_memory_id,
            superseded_by_memory_id, last_reinforced_at, expires_at,
            derivation_version, confidence, explanation_json, generation_trace_id,
            created_at, updated_at
        """
        async with self.pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    f"SELECT {select_cols} FROM memory_items WHERE id = %s AND owner_id = %s FOR UPDATE;",
                    (memory_id, owner_id),
                )
                row = await cur.fetchone()
                if row is None:
                    return None
                existing = self._memory_item_from_row(row)
                merged_scores = {**(existing.get("scores_json") or {}), **scores_json}
                await cur.execute(
                    f"""
                    UPDATE memory_items
                    SET scores_json = %s::jsonb,
                        last_reinforced_at = now(),
                        updated_at = now()
                    WHERE id = %s AND owner_id = %s
                    RETURNING {select_cols};
                    """,
                    (Json(merged_scores), memory_id, owner_id),
                )
                updated_row = await cur.fetchone()
                await cur.execute(
                    """
                    INSERT INTO memory_events (memory_id, owner_id, event_type, reason_json)
                    VALUES (%s, %s, 'reinforced', %s::jsonb);
                    """,
                    (memory_id, owner_id, Json({**reason_json, "request_id": request_id})),
                )
        return self._memory_item_from_row(updated_row)

    async def get_memory_debug(self, memory_id: UUID) -> dict[str, Any] | None:
        select_cols = """
            id, owner_id, memory_type, summary, source_refs_json, source_ref_hash,
            scores_json, promotion_state, status, supersedes_memory_id,
            superseded_by_memory_id, last_reinforced_at, expires_at,
            derivation_version, confidence, explanation_json, generation_trace_id,
            created_at, updated_at
        """
        async with self.pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(f"SELECT {select_cols} FROM memory_items WHERE id = %s;", (memory_id,))
                row = await cur.fetchone()
                if row is None:
                    return None
                await cur.execute(
                    """
                    SELECT id, memory_id, owner_id, event_type, reason_json, created_at
                    FROM memory_events
                    WHERE memory_id = %s
                    ORDER BY created_at ASC, id ASC;
                    """,
                    (memory_id,),
                )
                event_rows = await cur.fetchall()
        return {
            "memory": self._memory_item_from_row(row),
            "events": [self._memory_event_from_row(event_row) for event_row in event_rows],
        }


    def _episode_from_row(self, row: tuple[Any, ...]) -> dict[str, Any]:
        return {
            "episode_id": str(row[0]),
            "owner_id": row[1],
            "title": row[2],
            "summary": row[3],
            "episode_type": row[4],
            "trigger_json": row[5] or {},
            "outcome": row[6],
            "significance": row[7],
            "unresolved_json": row[8] or {},
            "source_refs_json": row[9] or [],
            "source_ref_hash": row[10],
            "episode_key": row[11],
            "callback_candidates_json": row[12] or [],
            "time_window_json": row[13] or {},
            "participants_json": row[14] or [],
            "status": row[15],
            "derivation_version": row[16],
            "confidence": row[17],
            "explanation_json": row[18] or {},
            "generation_trace_id": row[19],
            "created_at": str(row[20]),
            "updated_at": str(row[21]),
        }

    def _episode_link_from_row(self, row: tuple[Any, ...]) -> dict[str, Any]:
        return {
            "link_id": str(row[0]),
            "episode_id": str(row[1]),
            "owner_id": row[2],
            "ref_type": row[3],
            "ref_id": row[4],
            "relationship": row[5],
            "created_at": str(row[6]),
        }

    def _episode_event_from_row(self, row: tuple[Any, ...]) -> dict[str, Any]:
        return {
            "event_id": str(row[0]),
            "episode_id": str(row[1]),
            "owner_id": row[2],
            "event_type": row[3],
            "reason_json": row[4] or {},
            "created_at": str(row[5]),
        }

    async def create_or_update_episode(
        self,
        *,
        owner_id: str,
        title: str,
        summary: str,
        episode_type: str,
        trigger_json: dict[str, Any],
        outcome: str | None,
        significance: str | None,
        unresolved_json: dict[str, Any],
        source_refs_json: list[dict[str, Any]],
        source_ref_hash: str,
        episode_key: str,
        callback_candidates_json: list[Any],
        time_window_json: dict[str, Any],
        participants_json: list[Any],
        confidence: float | None,
        explanation_json: dict[str, Any],
        generation_trace_id: str | None,
        request_id: str,
        derivation_version: str = "r21-m0-v1",
    ) -> dict[str, Any]:
        select_cols = """
            id, owner_id, title, summary, episode_type, trigger_json,
            outcome, significance, unresolved_json, source_refs_json,
            source_ref_hash, episode_key, callback_candidates_json,
            time_window_json, participants_json, status, derivation_version,
            confidence, explanation_json, generation_trace_id, created_at, updated_at
        """
        incoming_mutable = {
            "title": title,
            "summary": summary,
            "outcome": outcome,
            "significance": significance,
            "unresolved_json": unresolved_json,
            "callback_candidates_json": callback_candidates_json,
            "participants_json": participants_json,
            "confidence": confidence,
            "explanation_json": explanation_json,
            "generation_trace_id": generation_trace_id,
        }
        async with self.pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    f"""
                    SELECT {select_cols}
                    FROM episodes
                    WHERE owner_id = %s AND episode_key = %s AND status = 'active'
                    LIMIT 1
                    FOR UPDATE;
                    """,
                    (owner_id, episode_key),
                )
                existing_row = await cur.fetchone()
                if existing_row is None:
                    await cur.execute(
                        f"""
                        INSERT INTO episodes (
                            owner_id, title, summary, episode_type, trigger_json,
                            outcome, significance, unresolved_json, source_refs_json,
                            source_ref_hash, episode_key, callback_candidates_json,
                            time_window_json, participants_json, status,
                            derivation_version, confidence, explanation_json,
                            generation_trace_id
                        ) VALUES (
                            %s, %s, %s, %s, %s::jsonb,
                            %s, %s, %s::jsonb, %s::jsonb,
                            %s, %s, %s::jsonb,
                            %s::jsonb, %s::jsonb, 'active',
                            %s, %s, %s::jsonb,
                            %s
                        ) RETURNING {select_cols};
                        """,
                        (
                            owner_id,
                            title,
                            summary,
                            episode_type,
                            Json(trigger_json),
                            outcome,
                            significance,
                            Json(unresolved_json),
                            Json(source_refs_json),
                            source_ref_hash,
                            episode_key,
                            Json(callback_candidates_json),
                            Json(time_window_json),
                            Json(participants_json),
                            derivation_version,
                            confidence,
                            Json(explanation_json),
                            generation_trace_id,
                        ),
                    )
                    row = await cur.fetchone()
                    episode_id = row[0]
                    await cur.execute(
                        """
                        INSERT INTO episode_events (episode_id, owner_id, event_type, reason_json)
                        VALUES (%s, %s, 'created', %s::jsonb);
                        """,
                        (episode_id, owner_id, Json({"request_id": request_id})),
                    )
                    return {
                        "episode": self._episode_from_row(row),
                        "created": True,
                        "updated": False,
                    }

                existing = self._episode_from_row(existing_row)
                changed = any(existing.get(k) != v for k, v in incoming_mutable.items())
                if not changed:
                    return {
                        "episode": existing,
                        "created": False,
                        "updated": False,
                    }

                await cur.execute(
                    f"""
                    UPDATE episodes
                    SET title = %s,
                        summary = %s,
                        outcome = %s,
                        significance = %s,
                        unresolved_json = %s::jsonb,
                        callback_candidates_json = %s::jsonb,
                        participants_json = %s::jsonb,
                        confidence = %s,
                        explanation_json = %s::jsonb,
                        generation_trace_id = %s,
                        updated_at = now()
                    WHERE id = %s AND owner_id = %s
                    RETURNING {select_cols};
                    """,
                    (
                        title,
                        summary,
                        outcome,
                        significance,
                        Json(unresolved_json),
                        Json(callback_candidates_json),
                        Json(participants_json),
                        confidence,
                        Json(explanation_json),
                        generation_trace_id,
                        existing_row[0],
                        owner_id,
                    ),
                )
                row = await cur.fetchone()
                await cur.execute(
                    """
                    INSERT INTO episode_events (episode_id, owner_id, event_type, reason_json)
                    VALUES (%s, %s, 'updated', %s::jsonb);
                    """,
                    (existing_row[0], owner_id, Json({"request_id": request_id})),
                )
                return {
                    "episode": self._episode_from_row(row),
                    "created": False,
                    "updated": True,
                }

    async def create_episode_links(
        self,
        *,
        episode_id: UUID,
        owner_id: str,
        links: list[dict[str, Any]],
        request_id: str,
    ) -> dict[str, Any] | None:
        async with self.pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    """
                    SELECT id
                    FROM episodes
                    WHERE id = %s AND owner_id = %s
                    FOR UPDATE;
                    """,
                    (episode_id, owner_id),
                )
                row = await cur.fetchone()
                if row is None:
                    return None

                out_links: list[dict[str, Any]] = []
                created_count = 0
                existing_count = 0
                created_refs: list[dict[str, Any]] = []
                for link in links:
                    await cur.execute(
                        """
                        INSERT INTO episode_links (episode_id, owner_id, ref_type, ref_id, relationship)
                        VALUES (%s, %s, %s, %s, %s)
                        ON CONFLICT (episode_id, ref_type, ref_id, relationship) DO NOTHING
                        RETURNING id, episode_id, owner_id, ref_type, ref_id, relationship, created_at;
                        """,
                        (episode_id, owner_id, link["ref_type"], link["ref_id"], link["relationship"]),
                    )
                    inserted = await cur.fetchone()
                    if inserted is not None:
                        created_count += 1
                        created_refs.append(
                            {
                                "ref_type": link["ref_type"],
                                "ref_id": link["ref_id"],
                                "relationship": link["relationship"],
                            }
                        )
                        out_links.append(self._episode_link_from_row(inserted))
                        continue

                    existing_count += 1
                    await cur.execute(
                        """
                        SELECT id, episode_id, owner_id, ref_type, ref_id, relationship, created_at
                        FROM episode_links
                        WHERE episode_id = %s AND ref_type = %s AND ref_id = %s AND relationship = %s
                        LIMIT 1;
                        """,
                        (episode_id, link["ref_type"], link["ref_id"], link["relationship"]),
                    )
                    existing_link = await cur.fetchone()
                    if existing_link is not None:
                        out_links.append(self._episode_link_from_row(existing_link))

                if created_count > 0:
                    await cur.execute(
                        """
                        INSERT INTO episode_events (episode_id, owner_id, event_type, reason_json)
                        VALUES (%s, %s, 'linked', %s::jsonb);
                        """,
                        (
                            episode_id,
                            owner_id,
                            Json({
                                "request_id": request_id,
                                "created_count": created_count,
                                "links": created_refs,
                            }),
                        ),
                    )

        return {
            "episode_id": str(episode_id),
            "created_count": created_count,
            "existing_count": existing_count,
            "links": out_links,
        }

    async def get_episode_debug(self, episode_id: UUID) -> dict[str, Any] | None:
        select_cols = """
            id, owner_id, title, summary, episode_type, trigger_json,
            outcome, significance, unresolved_json, source_refs_json,
            source_ref_hash, episode_key, callback_candidates_json,
            time_window_json, participants_json, status, derivation_version,
            confidence, explanation_json, generation_trace_id, created_at, updated_at
        """
        async with self.pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(f"SELECT {select_cols} FROM episodes WHERE id = %s;", (episode_id,))
                row = await cur.fetchone()
                if row is None:
                    return None
                await cur.execute(
                    """
                    SELECT id, episode_id, owner_id, ref_type, ref_id, relationship, created_at
                    FROM episode_links
                    WHERE episode_id = %s
                    ORDER BY created_at ASC, id ASC;
                    """,
                    (episode_id,),
                )
                link_rows = await cur.fetchall()
                await cur.execute(
                    """
                    SELECT id, episode_id, owner_id, event_type, reason_json, created_at
                    FROM episode_events
                    WHERE episode_id = %s
                    ORDER BY created_at ASC, id ASC;
                    """,
                    (episode_id,),
                )
                event_rows = await cur.fetchall()
        return {
            "episode": self._episode_from_row(row),
            "links": [self._episode_link_from_row(link_row) for link_row in link_rows],
            "events": [self._episode_event_from_row(event_row) for event_row in event_rows],
        }

    def _recall_decision_from_row(self, row: tuple[Any, ...]) -> dict[str, Any]:
        return {
            "id": str(row[0]),
            "request_id": row[1],
            "owner_id": row[2],
            "candidate_id": row[3],
            "candidate_type": row[4],
            "candidate_ref_json": row[5] or {},
            "source_refs_json": row[6] or [],
            "scene_id": row[7],
            "surface": row[8],
            "urgency": row[9],
            "sensitivity": row[10],
            "relevance_score": row[11],
            "salience_score": row[12],
            "recency_score": row[13],
            "mentionability_score": row[14],
            "decision": row[15],
            "mention_strategy": row[16],
            "prompt_eligible": row[17],
            "reason_json": row[18] or {},
            "created_at": str(row[19]),
        }

    async def persist_recall_decisions(
        self,
        *,
        request_id: str,
        owner_id: str,
        decisions: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        select_cols = """
            id, request_id, owner_id, candidate_id, candidate_type,
            candidate_ref_json, source_refs_json, scene_id, surface,
            urgency, sensitivity, relevance_score, salience_score,
            recency_score, mentionability_score, decision, mention_strategy,
            prompt_eligible, reason_json, created_at
        """
        out: list[dict[str, Any]] = []
        async with self.pool.connection() as conn:
            async with conn.cursor() as cur:
                for item in decisions:
                    await cur.execute(
                        f"""
                        INSERT INTO recall_decisions (
                            request_id, owner_id, candidate_id, candidate_type,
                            candidate_ref_json, source_refs_json, scene_id, surface,
                            urgency, sensitivity, relevance_score, salience_score,
                            recency_score, mentionability_score, decision,
                            mention_strategy, prompt_eligible, reason_json
                        ) VALUES (
                            %s, %s, %s, %s,
                            %s::jsonb, %s::jsonb, %s, %s,
                            %s, %s, %s, %s,
                            %s, %s, %s,
                            %s, %s, %s::jsonb
                        )
                        ON CONFLICT (request_id, owner_id, candidate_type, candidate_id)
                        DO UPDATE SET
                            candidate_ref_json = EXCLUDED.candidate_ref_json,
                            source_refs_json = EXCLUDED.source_refs_json,
                            scene_id = EXCLUDED.scene_id,
                            surface = EXCLUDED.surface,
                            urgency = EXCLUDED.urgency,
                            sensitivity = EXCLUDED.sensitivity,
                            relevance_score = EXCLUDED.relevance_score,
                            salience_score = EXCLUDED.salience_score,
                            recency_score = EXCLUDED.recency_score,
                            mentionability_score = EXCLUDED.mentionability_score,
                            decision = EXCLUDED.decision,
                            mention_strategy = EXCLUDED.mention_strategy,
                            prompt_eligible = EXCLUDED.prompt_eligible,
                            reason_json = EXCLUDED.reason_json
                        RETURNING {select_cols};
                        """,
                        (
                            request_id,
                            owner_id,
                            item["candidate_id"],
                            item["candidate_type"],
                            Json(item.get("candidate_ref_json") or {}),
                            Json(item.get("source_refs_json") or []),
                            item.get("scene_id"),
                            item.get("surface"),
                            item.get("urgency"),
                            item.get("sensitivity"),
                            item.get("relevance_score"),
                            item.get("salience_score"),
                            item.get("recency_score"),
                            item["mentionability_score"],
                            item["decision"],
                            item["mention_strategy"],
                            item["prompt_eligible"],
                            Json(item.get("reason_json") or {}),
                        ),
                    )
                    row = await cur.fetchone()
                    out.append(self._recall_decision_from_row(row))
        return out

    async def get_recall_debug(self, *, request_id: str, owner_id: str) -> list[dict[str, Any]]:
        q = """
        SELECT id, request_id, owner_id, candidate_id, candidate_type,
               candidate_ref_json, source_refs_json, scene_id, surface,
               urgency, sensitivity, relevance_score, salience_score,
               recency_score, mentionability_score, decision, mention_strategy,
               prompt_eligible, reason_json, created_at
        FROM recall_decisions
        WHERE request_id = %s AND owner_id = %s
        ORDER BY created_at ASC, id ASC;
        """
        async with self.pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(q, (request_id, owner_id))
                rows = await cur.fetchall()
        return [self._recall_decision_from_row(row) for row in rows]

    async def create_trace(self, trace: dict[str, Any]) -> UUID:
        q = """
        INSERT INTO traces (
            request_id, conversation_id, owner_id, client_id, surface,
            profile_json, retrieval_json, prompt_json, router_decision_json, manual_override_json,
            model_call_json, model_calls_json, fallback_json, artifacts_json, references_json,
            cost_json, latency_ms, status, error_text
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (request_id) DO UPDATE
            SET conversation_id = EXCLUDED.conversation_id,
                owner_id = EXCLUDED.owner_id,
                client_id = EXCLUDED.client_id,
                surface = EXCLUDED.surface,
                profile_json = EXCLUDED.profile_json,
                retrieval_json = EXCLUDED.retrieval_json,
                prompt_json = EXCLUDED.prompt_json,
                router_decision_json = EXCLUDED.router_decision_json,
                manual_override_json = EXCLUDED.manual_override_json,
                model_call_json = EXCLUDED.model_call_json,
                model_calls_json = EXCLUDED.model_calls_json,
                fallback_json = EXCLUDED.fallback_json,
                artifacts_json = EXCLUDED.artifacts_json,
                references_json = EXCLUDED.references_json,
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
                        Json(trace.get("prompt", {})),
                        Json(trace.get("router_decision", {})),
                        Json(trace.get("manual_override", {})),
                        Json(trace.get("model_call", {})),
                        Json(trace.get("model_calls", [])),
                        Json(trace.get("fallback", {})),
                        Json(trace.get("artifacts", {})),
                        Json(trace.get("references", [])),
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
               profile_json, retrieval_json, prompt_json, router_decision_json, manual_override_json,
               model_call_json, model_calls_json, fallback_json, artifacts_json, references_json,
               cost_json, latency_ms, status, error_text, created_at
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
            "prompt": row[8] or {},
            "router_decision": row[9] or {},
            "manual_override": row[10] or {},
            "model_call": row[11] or {},
            "model_calls": row[12] or [],
            "fallback": row[13] or {},
            "artifacts": row[14] or {},
            "references": row[15] or [],
            "cost": row[16] or {},
            "latency_ms": row[17],
            "status": row[18],
            "error": row[19],
            "created_at": str(row[20]),
        }
