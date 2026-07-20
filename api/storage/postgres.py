from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any, Callable, Optional
from uuid import UUID, uuid4

from psycopg_pool import AsyncConnectionPool
from psycopg.types.json import Json

from services.derivation_versions import EPISODE_DERIVATION_VERSION, MEMORY_ITEM_DERIVATION_VERSION
from services.memory_lifecycle import bounded_transition_reason


_CLAIM_RECORD_COLUMNS = """
    claim_id, schema_version, owner_id, conversation_id, request_id,
    assistant_message_id, surface, runtime_session_id, runtime_turn_id,
    acquisition_manifest_id, claim_anchor, claim_anchor_digest, claim_class, calibration_status,
    evidence_strength, confidence, strongest_authority, freshness_summary,
    uncertainty_disclosure_required, evidence_references_json,
    limitation_codes_json, user_safe_summary, created_at
"""


def _claim_record_from_row(row: tuple[Any, ...]) -> dict[str, Any]:
    return {
        "claim_id": row[0],
        "schema_version": row[1],
        "owner_id": row[2],
        "conversation_id": str(row[3]),
        "request_id": row[4],
        "assistant_message_id": str(row[5]),
        "surface": row[6],
        "runtime_session_id": row[7],
        "runtime_turn_id": row[8],
        "acquisition_manifest_id": row[9],
        "claim_anchor": row[10],
        "claim_anchor_digest": row[11],
        "claim_class": row[12],
        "calibration_status": row[13],
        "evidence_strength": row[14],
        "confidence": row[15],
        "strongest_authority": row[16],
        "freshness_summary": row[17],
        "uncertainty_disclosure_required": row[18],
        "validated_evidence_references": row[19] or [],
        "limitation_codes": row[20] or [],
        "user_safe_summary": row[21],
        "created_at": str(row[22]),
    }


def _bounded_scalar_map(value: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    out: dict[str, Any] = {}
    for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))[:12]:
        safe_key = str(key).strip()[:64]
        if not safe_key:
            continue
        if isinstance(item, str):
            out[safe_key] = item[:200]
        elif isinstance(item, (int, float, bool)) or item is None:
            out[safe_key] = item
    return out


def _coerce_score(value: Any) -> float:
    try:
        return max(0.0, min(1.0, float(value)))
    except (TypeError, ValueError):
        return 0.0


def _append_metadata_lifecycle_event(metadata: dict[str, Any], event: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    lifecycle = metadata.get("lifecycle") if isinstance(metadata.get("lifecycle"), dict) else {}
    events = lifecycle.get("events") if isinstance(lifecycle.get("events"), list) else []
    signature = (event.get("event_type"), event.get("request_id"), event.get("reason_code"))
    for existing in events:
        if not isinstance(existing, dict):
            continue
        if (existing.get("event_type"), existing.get("request_id"), existing.get("reason_code")) == signature:
            return metadata, False
    safe_event = {**event}
    if "metadata" in safe_event:
        safe_event["metadata"] = _bounded_scalar_map(safe_event.get("metadata"))
    event_type = str(safe_event.get("event_type") or "")
    status = lifecycle.get("status") or metadata.get("status")
    if event_type in {"invalidated", "rebuilding", "superseded"}:
        if status not in {"superseded", "invalidated"} or event_type == "superseded":
            status = event_type
    if event_type == "rebuild_terminal":
        result = safe_event.get("result") or safe_event.get("metadata", {}).get("result")
        status = "active" if result == "identical" else status
        if result == "replaced":
            status = "superseded"
        if result in {"unsupported", "failed"}:
            status = status if status in {"superseded", "invalidated"} else "invalidated"
    updated_lifecycle = {
        **lifecycle,
        "status": status,
        "invalidated_reason": safe_event.get("reason_code") if event_type == "invalidated" else lifecycle.get("invalidated_reason"),
        "last_request_id": safe_event.get("request_id") or lifecycle.get("last_request_id"),
        "terminal_result": safe_event.get("result") or safe_event.get("metadata", {}).get("result") or lifecycle.get("terminal_result"),
        "replacement_id": safe_event.get("replacement_id") or safe_event.get("metadata", {}).get("replacement_id") or lifecycle.get("replacement_id"),
        "failure_reason": safe_event.get("failure_reason") or safe_event.get("metadata", {}).get("failure_reason") or lifecycle.get("failure_reason"),
        "events": [*events, safe_event],
    }
    updated = {**metadata, "lifecycle": updated_lifecycle}
    if status:
        updated["status"] = status
    if updated_lifecycle.get("replacement_id"):
        updated["replacement_id"] = updated_lifecycle["replacement_id"]
    return updated, True


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
        policy_metadata: dict | None = None,
    ) -> UUID:
        q = """
        INSERT INTO messages (conversation_id, owner_id, client_id, role, content, metadata, policy_metadata)
        VALUES (%s, %s, %s, %s, %s, %s, %s)
        RETURNING id;
        """
        q_touch = """
        UPDATE conversations
        SET updated_at = now()
        WHERE id = %s;
        """
        meta_param = Json(metadata) if metadata is not None else None
        policy_param = Json(policy_metadata) if policy_metadata is not None else None
        async with self.pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(q, (conversation_id, owner_id, client_id, role, content, meta_param, policy_param))
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
        SELECT id, conversation_id, role, content, metadata, policy_metadata, created_at
        FROM messages
        WHERE id = ANY(%s);
        """
        async with self.pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(q, (id_strs,))
                rows = await cur.fetchall()

        by_id: dict[str, dict[str, Any]] = {}
        for (mid, cid, role, content, metadata, policy_metadata, created_at) in rows:
            by_id[str(mid)] = {
                "message_id": str(mid),
                "conversation_id": str(cid),
                "role": role,
                "content": content,
                "metadata": metadata or {},
                "policy_metadata": policy_metadata,
                "created_at": str(created_at),
            }

        # Preserve input order
        return [by_id[mid] for mid in id_strs if mid in by_id]

    async def get_message_owner(self, message_id: UUID) -> str | None:
        q = """
        SELECT c.owner_id
        FROM messages m
        JOIN conversations c ON c.id = m.conversation_id
        WHERE m.id = %s
        LIMIT 1;
        """
        async with self.pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(q, (message_id,))
                row = await cur.fetchone()
        return str(row[0]) if row else None

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

    async def update_proactive_suggestion_evidence(
        self,
        *,
        suggestion_id: UUID,
        owner_id: str,
        evidence_json: dict[str, Any],
    ) -> dict[str, Any] | None:
        q = """
        UPDATE proactive_suggestions
        SET evidence_json = %s::jsonb,
            updated_at = now()
        WHERE id = %s AND owner_id = %s
        RETURNING id, owner_id, source_event_log_id, source_type, kind, status, title, body,
                  explanation_json, evidence_json, target_surface, delivery_surface,
                  delivery_status, delivery_external_id, delivery_error, delivered_at,
                  created_at, updated_at;
        """
        async with self.pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(q, (Json(evidence_json), suggestion_id, owner_id))
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

    async def append_proactive_suggestion_lifecycle_event(
        self,
        *,
        suggestion_id: UUID,
        owner_id: str,
        event: dict[str, Any],
    ) -> dict[str, Any] | None:
        select_cols = """
            id, owner_id, source_event_log_id, source_type, kind, status, title, body,
            explanation_json, evidence_json, target_surface, delivery_surface,
            delivery_status, delivery_external_id, delivery_error, delivered_at,
            created_at, updated_at
        """
        async with self.pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    f"SELECT {select_cols} FROM proactive_suggestions WHERE id = %s AND owner_id = %s FOR UPDATE;",
                    (suggestion_id, owner_id),
                )
                row = await cur.fetchone()
                if row is None:
                    return None
                evidence = row[9] or {}
                updated, _ = _append_metadata_lifecycle_event(evidence, event)
                await cur.execute(
                    f"""
                    UPDATE proactive_suggestions
                    SET evidence_json = %s::jsonb,
                        updated_at = now()
                    WHERE id = %s AND owner_id = %s
                    RETURNING {select_cols};
                    """,
                    (Json(updated), suggestion_id, owner_id),
                )
                row = await cur.fetchone()
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
        SELECT id, conversation_id, owner_id, client_id, role, content, metadata, policy_metadata, created_at
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
        for (mid, cid, owner, client_id, role, content, metadata, policy_metadata, created_at) in rows:
            out.append(
                {
                    "message_id": mid,
                    "conversation_id": cid,
                    "owner_id": owner,
                    "client_id": client_id,
                    "role": role,
                    "content": content,
                    "metadata": metadata or {},
                    "policy_metadata": policy_metadata,
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
        policy_metadata: dict | None = None,
    ) -> dict[str, Any]:
        q = """
        INSERT INTO artifacts (
            id, owner_id, client_id, conversation_id, filename, mime, size, object_uri, source_surface,
            status, sha256, source_kind, repo_name, repo_ref, file_path, ingestion_id, policy_metadata, completed_at
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                CASE WHEN %s = 'completed' THEN now() ELSE NULL END)
        RETURNING id, owner_id, client_id, conversation_id, filename, mime, size, object_uri, source_surface,
                  status, sha256, created_at, completed_at, source_kind, repo_name, repo_ref, file_path, ingestion_id,
                  policy_metadata;
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
                        Json(policy_metadata) if policy_metadata is not None else None,
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
                    policy_metadata_out,
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
            "policy_metadata": policy_metadata_out,
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
        RETURNING id, owner_id, client_id, conversation_id, filename, mime, size, object_uri, source_surface,
                  status, sha256, created_at, completed_at, source_kind, repo_name, repo_ref, file_path, ingestion_id,
                  policy_metadata;
        """
        async with self.pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(q, (status, sha256, status, artifact_id))
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
            status_out,
            sha256_out,
            created_at,
            completed_at,
            source_kind,
            repo_name,
            repo_ref,
            file_path,
            ingestion_id,
            policy_metadata,
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
            "status": status_out,
            "sha256": sha256_out,
            "created_at": str(created_at),
            "completed_at": str(completed_at) if completed_at else None,
            "source_kind": source_kind,
            "repo_name": repo_name,
            "repo_ref": repo_ref,
            "file_path": file_path,
            "ingestion_id": str(ingestion_id) if ingestion_id else None,
            "policy_metadata": policy_metadata,
        }

    async def get_artifact(self, artifact_id: UUID) -> dict[str, Any] | None:
        q = """
        SELECT id, owner_id, client_id, conversation_id, filename, mime, size, object_uri, source_surface,
               status, sha256, created_at, completed_at, source_kind, repo_name, repo_ref, file_path, ingestion_id,
               policy_metadata
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
            policy_metadata,
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
            "policy_metadata": policy_metadata,
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
        SELECT dt.id, dt.artifact_id, dt.kind, dt.text, dt.derivation_params, dt.created_at,
               a.owner_id, a.file_path, a.repo_name, a.mime, a.policy_metadata
        FROM derived_text dt
        JOIN artifacts a ON a.id = dt.artifact_id
        WHERE dt.id = ANY(%s)
          AND COALESCE(dt.derivation_params->>'status', 'active') = 'active'
          AND a.status = 'completed';
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
                "kind": row[2],
                "text": row[3],
                "derivation_params": row[4] or {},
                "created_at": str(row[5]),
                "owner_id": row[6],
                "file_path": row[7] or "",
                "repo_name": row[8],
                "mime": row[9],
                "policy_metadata": row[10],
            }
        return [by_id[item] for item in id_strs if item in by_id]

    async def get_active_derived_text_for_artifact(
        self,
        *,
        artifact_id: UUID,
        derivation_version: str,
    ) -> list[dict[str, Any]]:
        q = """
        SELECT id, artifact_id, kind, language, text, derivation_params, created_at
        FROM derived_text
        WHERE artifact_id = %s
          AND derivation_params->>'derivation_version' = %s
          AND COALESCE(derivation_params->>'status', 'active') = 'active'
        ORDER BY (derivation_params->>'chunk_index')::int NULLS LAST, created_at ASC;
        """
        async with self.pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(q, (artifact_id, derivation_version))
                rows = await cur.fetchall()
        return [
            {
                "derived_text_id": str(row[0]),
                "artifact_id": str(row[1]),
                "kind": row[2],
                "language": row[3],
                "text": row[4],
                "derivation_params": row[5] or {},
                "created_at": str(row[6]),
            }
            for row in rows
        ]

    async def get_derived_text_for_artifact_version(
        self,
        *,
        artifact_id: UUID,
        derivation_version: str,
    ) -> list[dict[str, Any]]:
        q = """
        SELECT id, artifact_id, kind, language, text, derivation_params, created_at
        FROM derived_text
        WHERE artifact_id = %s
          AND derivation_params->>'derivation_version' = %s
        ORDER BY (derivation_params->>'chunk_index')::int NULLS LAST, created_at ASC;
        """
        async with self.pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(q, (artifact_id, derivation_version))
                rows = await cur.fetchall()
        return [
            {
                "derived_text_id": str(row[0]),
                "artifact_id": str(row[1]),
                "kind": row[2],
                "language": row[3],
                "text": row[4],
                "derivation_params": row[5] or {},
                "created_at": str(row[6]),
            }
            for row in rows
        ]

    async def get_derived_text_for_owner(
        self,
        derived_text_id: UUID,
        owner_id: str,
    ) -> dict[str, Any] | None:
        q = """
        SELECT dt.id, dt.artifact_id, dt.kind, dt.language, dt.text,
               dt.derivation_params, dt.created_at, a.owner_id
        FROM derived_text dt
        JOIN artifacts a ON a.id = dt.artifact_id
        WHERE dt.id = %s AND a.owner_id = %s
        LIMIT 1;
        """
        async with self.pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(q, (derived_text_id, owner_id))
                row = await cur.fetchone()
        if row is None:
            return None
        return {
            "derived_text_id": str(row[0]),
            "artifact_id": str(row[1]),
            "kind": row[2],
            "language": row[3],
            "text": row[4],
            "derivation_params": row[5] or {},
            "created_at": str(row[6]),
            "owner_id": row[7],
        }

    async def update_derived_text_params(
        self,
        *,
        derived_text_id: UUID,
        owner_id: str,
        derivation_params: dict[str, Any],
    ) -> dict[str, Any] | None:
        q = """
        UPDATE derived_text dt
        SET derivation_params = %s::jsonb
        FROM artifacts a
        WHERE dt.id = %s
          AND dt.artifact_id = a.id
          AND a.owner_id = %s
        RETURNING dt.id, dt.artifact_id, dt.kind, dt.language, dt.text,
                  dt.derivation_params, dt.created_at, a.owner_id;
        """
        async with self.pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(q, (Json(derivation_params), derived_text_id, owner_id))
                row = await cur.fetchone()
        if row is None:
            return None
        return {
            "derived_text_id": str(row[0]),
            "artifact_id": str(row[1]),
            "kind": row[2],
            "language": row[3],
            "text": row[4],
            "derivation_params": row[5] or {},
            "created_at": str(row[6]),
            "owner_id": row[7],
        }

    async def activate_derived_text_attempt(
        self,
        *,
        artifact_id: UUID,
        owner_id: str,
        derivation_version: str,
        attempt_id: str,
        expected_chunk_count: int,
    ) -> list[dict[str, Any]]:
        select_cols = """
            dt.id, dt.artifact_id, dt.kind, dt.language, dt.text,
            dt.derivation_params, dt.created_at, a.owner_id
        """
        async with self.pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    """
                    SELECT count(*)
                    FROM derived_text dt
                    JOIN artifacts a ON a.id = dt.artifact_id
                    WHERE dt.artifact_id = %s
                      AND a.owner_id = %s
                      AND dt.derivation_params->>'derivation_version' = %s
                      AND dt.derivation_params->>'attempt_id' = %s
                      AND dt.derivation_params->>'status' = 'building'
                      AND dt.derivation_params->>'indexing_status' = 'indexed';
                    """,
                    (artifact_id, owner_id, derivation_version, attempt_id),
                )
                count_row = await cur.fetchone()
                if int(count_row[0]) != expected_chunk_count:
                    raise RuntimeError("derived text attempt is incomplete")
                await cur.execute(
                    f"""
                    UPDATE derived_text dt
                    SET derivation_params =
                        jsonb_set(
                            jsonb_set(dt.derivation_params, '{{status}}', '"active"'::jsonb, true),
                            '{{activated_at}}',
                            to_jsonb(now()::text),
                            true
                        )
                    FROM artifacts a
                    WHERE dt.artifact_id = %s
                      AND dt.artifact_id = a.id
                      AND a.owner_id = %s
                      AND dt.derivation_params->>'derivation_version' = %s
                      AND dt.derivation_params->>'attempt_id' = %s
                      AND dt.derivation_params->>'status' = 'building'
                    RETURNING {select_cols};
                    """,
                    (artifact_id, owner_id, derivation_version, attempt_id),
                )
                rows = await cur.fetchall()
        if len(rows) != expected_chunk_count:
            raise RuntimeError("derived text attempt activation count mismatch")
        return [
            {
                "derived_text_id": str(row[0]),
                "artifact_id": str(row[1]),
                "kind": row[2],
                "language": row[3],
                "text": row[4],
                "derivation_params": row[5] or {},
                "created_at": str(row[6]),
                "owner_id": row[7],
            }
            for row in rows
        ]

    async def append_derived_text_lifecycle_event(
        self,
        *,
        derived_text_id: UUID,
        owner_id: str,
        event: dict[str, Any],
    ) -> dict[str, Any] | None:
        select_cols = """
            dt.id, dt.artifact_id, dt.kind, dt.language, dt.text,
            dt.derivation_params, dt.created_at, a.owner_id
        """
        async with self.pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    f"""
                    SELECT {select_cols}
                    FROM derived_text dt
                    JOIN artifacts a ON a.id = dt.artifact_id
                    WHERE dt.id = %s AND a.owner_id = %s
                    FOR UPDATE OF dt;
                    """,
                    (derived_text_id, owner_id),
                )
                row = await cur.fetchone()
                if row is None:
                    return None
                params = row[5] or {}
                updated, _ = _append_metadata_lifecycle_event(params, event)
                await cur.execute(
                    f"""
                    UPDATE derived_text dt
                    SET derivation_params = %s::jsonb
                    FROM artifacts a
                    WHERE dt.id = %s
                      AND dt.artifact_id = a.id
                      AND a.owner_id = %s
                    RETURNING {select_cols};
                    """,
                    (Json(updated), derived_text_id, owner_id),
                )
                row = await cur.fetchone()
        return {
            "derived_text_id": str(row[0]),
            "artifact_id": str(row[1]),
            "kind": row[2],
            "language": row[3],
            "text": row[4],
            "derivation_params": row[5] or {},
            "created_at": str(row[6]),
            "owner_id": row[7],
        }

    async def replace_derived_text_atomically(
        self,
        *,
        predecessor_derived_text_id: UUID,
        owner_id: str,
        request_id: str,
        kind: str,
        text: str,
        language: str | None,
        derivation_params: dict[str, Any],
        inject_failure_after_insert: bool = False,
    ) -> dict[str, Any] | None:
        select_cols = """
            dt.id, dt.artifact_id, dt.kind, dt.language, dt.text,
            dt.derivation_params, dt.created_at, a.owner_id
        """

        def row_to_dict(row: tuple[Any, ...]) -> dict[str, Any]:
            return {
                "derived_text_id": str(row[0]),
                "artifact_id": str(row[1]),
                "kind": row[2],
                "language": row[3],
                "text": row[4],
                "derivation_params": row[5] or {},
                "created_at": str(row[6]),
                "owner_id": row[7],
            }

        async with self.pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    f"""
                    SELECT {select_cols}
                    FROM derived_text dt
                    JOIN artifacts a ON a.id = dt.artifact_id
                    WHERE dt.id = %s AND a.owner_id = %s
                    FOR UPDATE OF dt;
                    """,
                    (predecessor_derived_text_id, owner_id),
                )
                predecessor_row = await cur.fetchone()
                if predecessor_row is None:
                    return None
                predecessor = row_to_dict(predecessor_row)
                predecessor_params = predecessor["derivation_params"] or {}
                lifecycle = predecessor_params.get("lifecycle") if isinstance(predecessor_params.get("lifecycle"), dict) else {}
                events = lifecycle.get("events") if isinstance(lifecycle.get("events"), list) else []
                for event in reversed(events):
                    if not isinstance(event, dict) or event.get("request_id") != request_id:
                        continue
                    terminal_result = event.get("result") or event.get("terminal_result")
                    if terminal_result == "replaced" and event.get("replacement_id"):
                        replacement_id = UUID(str(event["replacement_id"]))
                        await cur.execute(
                            f"""
                            SELECT {select_cols}
                            FROM derived_text dt
                            JOIN artifacts a ON a.id = dt.artifact_id
                            WHERE dt.id = %s AND a.owner_id = %s
                            LIMIT 1;
                            """,
                            (replacement_id, owner_id),
                        )
                        replacement_row = await cur.fetchone()
                        if replacement_row is None:
                            raise ValueError("terminal_replacement_missing")
                        return {
                            "predecessor": predecessor,
                            "replacement": row_to_dict(replacement_row),
                            "idempotent": True,
                        }
                effective_status = predecessor_params.get("status") or lifecycle.get("status") or "active"
                if effective_status == "superseded" or predecessor_params.get("replacement_id"):
                    raise ValueError("predecessor_not_current")

                replacement_params = {
                    **derivation_params,
                    "replacement_for": str(predecessor_derived_text_id),
                    "status": "active",
                }
                await cur.execute(
                    """
                    INSERT INTO derived_text (artifact_id, kind, language, text, derivation_params)
                    VALUES (%s, %s, %s, %s, %s::jsonb)
                    RETURNING id, artifact_id, kind, language, text, derivation_params, created_at;
                    """,
                    (
                        UUID(predecessor["artifact_id"]),
                        kind,
                        language,
                        text,
                        Json(replacement_params),
                    ),
                )
                new_row = await cur.fetchone()
                if inject_failure_after_insert:
                    raise RuntimeError("injected_replace_derived_text_failure")
                replacement_id = str(new_row[0])
                superseded_event = {
                    "event_type": "superseded",
                    "request_id": request_id,
                    "reason_code": "rebuild_replaced",
                    "replacement_id": replacement_id,
                }
                terminal_event = {
                    "event_type": "rebuild_terminal",
                    "request_id": request_id,
                    "reason_code": "rebuild_terminal",
                    "result": "replaced",
                    "replacement_id": replacement_id,
                }
                updated_params, _ = _append_metadata_lifecycle_event(predecessor_params, superseded_event)
                updated_params, _ = _append_metadata_lifecycle_event(updated_params, terminal_event)
                await cur.execute(
                    f"""
                    UPDATE derived_text dt
                    SET derivation_params = %s::jsonb
                    FROM artifacts a
                    WHERE dt.id = %s
                      AND dt.artifact_id = a.id
                      AND a.owner_id = %s
                    RETURNING {select_cols};
                    """,
                    (Json(updated_params), predecessor_derived_text_id, owner_id),
                )
                updated_predecessor_row = await cur.fetchone()
                return {
                    "predecessor": row_to_dict(updated_predecessor_row),
                    "replacement": {
                        "derived_text_id": replacement_id,
                        "artifact_id": str(new_row[1]),
                        "kind": new_row[2],
                        "language": new_row[3],
                        "text": new_row[4],
                        "derivation_params": new_row[5] or {},
                        "created_at": str(new_row[6]),
                        "owner_id": owner_id,
                    },
                    "idempotent": False,
                }

    async def get_recent_message_snippets(
        self,
        conversation_id: UUID,
        limit: int = 10,
        owner_id: str | None = None,
    ) -> list[dict[str, Any]]:
        where = ["conversation_id = %s"]
        params: list[Any] = [conversation_id]
        if owner_id is not None:
            where.append("owner_id = %s")
            params.append(owner_id)
        params.append(limit)
        q = f"""
        SELECT id, conversation_id, role, content, created_at
        FROM messages
        WHERE {' AND '.join(where)}
        ORDER BY created_at DESC
        LIMIT %s;
        """
        async with self.pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(q, tuple(params))
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

    def _append_json_policy_where(
        self,
        where: str,
        params: list[Any],
        policy_filter: dict[str, Any] | None,
        *,
        column: str,
    ) -> tuple[str, list[Any]]:
        if policy_filter is None:
            return where, params
        allowed_domains = list(policy_filter.get("allowed_domains") or [])
        if not allowed_domains:
            return where + " AND false", params
        blocked_domains = list(policy_filter.get("blocked_domains") or [])
        allowed_sensitivities = list(policy_filter.get("allowed_sensitivities") or ["low", "medium", "high"])
        where += f" AND {column} IS NOT NULL"
        where += f" AND ({column}->'memory_domains') ?| %s"
        params.append(allowed_domains)
        if blocked_domains:
            where += f" AND NOT (({column}->'memory_domains') ?| %s)"
            params.append(blocked_domains)
        where += f" AND ({column}->>'sensitivity') = ANY(%s)"
        params.append(allowed_sensitivities)
        relationship = policy_filter.get("relationship_scope") or {}
        if relationship.get("applied"):
            relationship_ids = list(relationship.get("relationship_ids") or [])
            entity_ids = list(relationship.get("entity_ids") or [])
            where += f" AND (({column}->'relationship_ids') ?| %s OR ({column}->'entity_ids') ?| %s)"
            params.extend([relationship_ids, entity_ids])
            relationship_scopes = list(relationship.get("relationship_scopes") or [])
            if relationship_scopes:
                where += (
                    f" AND (jsonb_array_length(COALESCE({column}->'relationship_scopes', '[]'::jsonb)) = 0 "
                    f"OR ({column}->'relationship_scopes') ?| %s)"
                )
                params.append(relationship_scopes)
        return where, params

    async def get_pinned_memories(
        self,
        owner_id: str,
        conversation_id: UUID | None = None,
        limit: int = 5,
        policy_filter: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        params: list[Any] = [owner_id]
        where = "WHERE owner_id = %s"
        if conversation_id is not None:
            where += " AND (conversation_id = %s OR conversation_id IS NULL)"
            params.append(conversation_id)
        where, params = self._append_json_policy_where(where, params, policy_filter, column="policy_metadata")

        q = f"""
        SELECT id, content, metadata, policy_metadata
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
                "policy_metadata": policy_metadata,
            }
            for (pid, content, metadata, policy_metadata) in rows
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

    async def get_policy_overlays(
        self,
        owner_id: str,
        surface: str | None = None,
        policy_filter: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        where = "WHERE owner_id = %s AND (surface = %s OR surface IS NULL)"
        params: list[Any] = [owner_id, surface]
        where, params = self._append_json_policy_where(where, params, policy_filter, column="policy_metadata")
        q = f"""
        SELECT id, policy_json, policy_metadata
        FROM policy_overlays
        {where}
        ORDER BY created_at DESC
        LIMIT 5;
        """
        async with self.pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(q, tuple(params))
                rows = await cur.fetchall()

        return [
            {"id": str(pid), "content": "policy", "metadata": payload or {}, "policy_metadata": policy_metadata}
            for (pid, payload, policy_metadata) in rows
        ]

    async def get_persona_overlays(
        self,
        owner_id: str,
        surface: str | None = None,
        policy_filter: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        where = "WHERE owner_id = %s AND (surface = %s OR surface IS NULL)"
        params: list[Any] = [owner_id, surface]
        where, params = self._append_json_policy_where(where, params, policy_filter, column="policy_metadata")
        q = f"""
        SELECT id, persona_json, policy_metadata
        FROM persona_overlays
        {where}
        ORDER BY created_at DESC
        LIMIT 5;
        """
        async with self.pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(q, tuple(params))
                rows = await cur.fetchall()

        return [
            {"id": str(pid), "content": "persona", "metadata": payload or {}, "policy_metadata": policy_metadata}
            for (pid, payload, policy_metadata) in rows
        ]

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

    async def get_recent_message_items(
        self,
        conversation_id: UUID,
        limit: int = 10,
        policy_filter: dict[str, Any] | None = None,
        owner_id: str | None = None,
    ) -> list[dict[str, Any]]:
        where = ["conversation_id = %s"]
        params: list[Any] = [conversation_id]
        if owner_id is not None:
            where.append("owner_id = %s")
            params.append(owner_id)
        if policy_filter is not None:
            allowed_domains = list(policy_filter.get("allowed_domains") or [])
            if not allowed_domains:
                return []
            blocked_domains = list(policy_filter.get("blocked_domains") or [])
            allowed_sensitivities = list(policy_filter.get("allowed_sensitivities") or [])
            where.append("policy_metadata IS NOT NULL")
            where.append("(policy_metadata->'memory_domains') ?| %s")
            params.append(allowed_domains)
            if blocked_domains:
                where.append("NOT ((policy_metadata->'memory_domains') ?| %s)")
                params.append(blocked_domains)
            where.append("(policy_metadata->>'sensitivity') = ANY(%s)")
            params.append(allowed_sensitivities or ["low", "medium", "high"])
            relationship = policy_filter.get("relationship_scope") or {}
            if relationship.get("applied"):
                relationship_ids = list(relationship.get("relationship_ids") or [])
                entity_ids = list(relationship.get("entity_ids") or [])
                where.append(
                    "((policy_metadata->'relationship_ids') ?| %s "
                    "OR (policy_metadata->'entity_ids') ?| %s)"
                )
                params.extend([relationship_ids, entity_ids])
                relationship_scopes = list(relationship.get("relationship_scopes") or [])
                if relationship_scopes:
                    where.append(
                        "(jsonb_array_length(COALESCE(policy_metadata->'relationship_scopes', '[]'::jsonb)) = 0 "
                        "OR (policy_metadata->'relationship_scopes') ?| %s)"
                    )
                    params.append(relationship_scopes)
        params.append(limit)
        q = f"""
        SELECT id, conversation_id, role, content, metadata, policy_metadata, created_at
        FROM messages
        WHERE {' AND '.join(where)}
        ORDER BY created_at DESC
        LIMIT %s;
        """
        async with self.pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(q, tuple(params))
                rows = await cur.fetchall()

        rows.reverse()
        return [
            {
                "message_id": str(r[0]),
                "conversation_id": str(r[1]),
                "role": r[2],
                "content": r[3],
                "metadata": r[4] or {},
                "policy_metadata": r[5],
                "created_at": str(r[6]),
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
        derivation_version: str = MEMORY_ITEM_DERIVATION_VERSION,
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
                    if old_row[10] is not None:
                        raise ValueError("superseded memory already has a replacement")

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
                            Json(
                                bounded_transition_reason(
                                    code="memory_replaced",
                                    metadata={},
                                    request_id=request_id,
                                    previous_status=str(old_row[8]),
                                    new_status="superseded",
                                    related_memory_id=str(new_id),
                                )
                            ),
                        ),
                    )
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

    async def record_memory_decision(
        self,
        *,
        owner_id: str,
        memory_type: str,
        summary: str,
        source_refs_json: list[dict[str, Any]],
        source_ref_hash: str,
        scores_json: dict[str, Any],
        promotion_state: str,
        status: str,
        explanation_json: dict[str, Any],
        request_id: str,
        event_type: str,
        reason_json: dict[str, Any],
        derivation_version: str = MEMORY_ITEM_DERIVATION_VERSION,
    ) -> dict[str, Any]:
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
                    f"""
                    INSERT INTO memory_items (
                        owner_id, memory_type, summary, source_refs_json,
                        source_ref_hash, scores_json, promotion_state, status,
                        derivation_version, explanation_json
                    ) VALUES (%s, %s, %s, %s::jsonb, %s, %s::jsonb, %s, %s, %s, %s::jsonb)
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
                        status,
                        derivation_version,
                        Json(explanation_json),
                    ),
                )
                row = await cur.fetchone()
                memory_id = row[0]
                await cur.execute(
                    """
                    INSERT INTO memory_events (memory_id, owner_id, event_type, reason_json)
                    VALUES (%s, %s, %s, %s::jsonb);
                    """,
                    (
                        memory_id,
                        owner_id,
                        event_type,
                        Json({**reason_json, "request_id": request_id}),
                    ),
                )
        return await self.get_memory_debug(memory_id, owner_id)

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
                previous_scores = existing.get("scores_json") or {}
                merged_scores = {**previous_scores, **scores_json}
                previous_salience = previous_scores.get("salience_score")
                refreshed_salience = max(_coerce_score(previous_salience) + 0.1, _coerce_score(scores_json.get("salience_score")))
                merged_scores["salience_score"] = round(min(1.0, refreshed_salience), 4)
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

    async def decay_memory_item(
        self,
        *,
        memory_id: UUID,
        owner_id: str,
        decay_factor: float,
        demote: bool,
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
                previous_scores = existing.get("scores_json") or {}
                previous_salience = _coerce_score(previous_scores.get("salience_score"))
                new_salience = round(max(0.0, previous_salience * (1.0 - max(0.0, min(1.0, decay_factor)))), 4)
                merged_scores = {
                    **previous_scores,
                    "salience_score": new_salience,
                    "last_decay_factor": round(max(0.0, min(1.0, decay_factor)), 4),
                }
                new_status = "forgotten_or_demoted" if demote else "stale"
                new_promotion_state = "decayed" if demote or new_salience <= 0.2 else existing["promotion_state"]
                await cur.execute(
                    f"""
                    UPDATE memory_items
                    SET scores_json = %s::jsonb,
                        promotion_state = %s,
                        status = %s,
                        updated_at = now()
                    WHERE id = %s AND owner_id = %s
                    RETURNING {select_cols};
                    """,
                    (Json(merged_scores), new_promotion_state, new_status, memory_id, owner_id),
                )
                updated_row = await cur.fetchone()
                await cur.execute(
                    """
                    INSERT INTO memory_events (memory_id, owner_id, event_type, reason_json)
                    VALUES (%s, %s, 'decayed', %s::jsonb);
                    """,
                    (
                        memory_id,
                        owner_id,
                        Json(
                            {
                                **reason_json,
                                "request_id": request_id,
                                "previous_salience_score": previous_salience,
                                "new_salience_score": new_salience,
                                "demoted": demote,
                            }
                        ),
                    ),
                )
        return self._memory_item_from_row(updated_row)

    async def transition_memory_item(
        self,
        *,
        memory_id: UUID,
        owner_id: str,
        new_status: str,
        reason_code: str,
        reason_metadata: dict[str, Any],
        request_id: str,
        related_memory_id: UUID | None,
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
                    f"""
                    SELECT {select_cols}
                    FROM memory_items
                    WHERE id = %s AND owner_id = %s
                    FOR UPDATE;
                    """,
                    (memory_id, owner_id),
                )
                row = await cur.fetchone()
                if row is None:
                    return None
                current = self._memory_item_from_row(row)
                related = None

                if related_memory_id == memory_id:
                    raise ValueError("related memory must be different from the transitioned memory")
                if new_status in {"corrected", "superseded"} and related_memory_id is None:
                    raise ValueError(f"related_memory_id is required for status {new_status}")
                if related_memory_id is not None:
                    await cur.execute(
                        f"""
                        SELECT {select_cols}
                        FROM memory_items
                        WHERE id = %s AND owner_id = %s
                        FOR UPDATE;
                        """,
                        (related_memory_id, owner_id),
                    )
                    related_row = await cur.fetchone()
                    if related_row is None:
                        raise KeyError("related memory not found")
                    related = self._memory_item_from_row(related_row)

                primary_supersedes = current.get("supersedes_memory_id")
                primary_superseded_by = current.get("superseded_by_memory_id")
                related_changed_status = False
                related_previous_status = None

                if new_status == "corrected":
                    assert related is not None
                    if primary_supersedes not in {None, str(related_memory_id)}:
                        raise ValueError("memory already corrects or supersedes a different item")
                    if related.get("superseded_by_memory_id") not in {None, str(memory_id)}:
                        raise ValueError("related memory already has a different replacement")
                    primary_supersedes = str(related_memory_id)
                elif new_status in {"superseded", "contradicted"} and related is not None:
                    if primary_superseded_by not in {None, str(related_memory_id)}:
                        raise ValueError("memory already has a different replacement")
                    if related.get("supersedes_memory_id") not in {None, str(memory_id)}:
                        raise ValueError("related memory already replaces a different item")
                    primary_superseded_by = str(related_memory_id)
                elif related is not None:
                    raise ValueError("related_memory_id is only supported for correction or supersession")

                relation_is_current = True
                if new_status == "corrected":
                    relation_is_current = (
                        current.get("supersedes_memory_id") == str(related_memory_id)
                        and related is not None
                        and related.get("superseded_by_memory_id") == str(memory_id)
                        and related.get("status") == "superseded"
                    )
                elif new_status in {"superseded", "contradicted"} and related is not None:
                    relation_is_current = (
                        current.get("superseded_by_memory_id") == str(related_memory_id)
                        and related.get("supersedes_memory_id") == str(memory_id)
                    )

                if current["status"] == new_status and relation_is_current:
                    return {
                        "memory": current,
                        "changed": False,
                        "events_appended": [],
                    }

                await cur.execute(
                    f"""
                    UPDATE memory_items
                    SET status = %s,
                        supersedes_memory_id = %s,
                        superseded_by_memory_id = %s,
                        updated_at = now()
                    WHERE id = %s AND owner_id = %s
                    RETURNING {select_cols};
                    """,
                    (
                        new_status,
                        primary_supersedes,
                        primary_superseded_by,
                        memory_id,
                        owner_id,
                    ),
                )
                updated_row = await cur.fetchone()

                if new_status == "corrected":
                    assert related is not None
                    related_previous_status = related["status"]
                    related_changed_status = related_previous_status != "superseded"
                    await cur.execute(
                        """
                        UPDATE memory_items
                        SET status = 'superseded',
                            superseded_by_memory_id = %s,
                            updated_at = now()
                        WHERE id = %s AND owner_id = %s;
                        """,
                        (memory_id, related_memory_id, owner_id),
                    )
                elif new_status in {"superseded", "contradicted"} and related is not None:
                    await cur.execute(
                        """
                        UPDATE memory_items
                        SET supersedes_memory_id = %s,
                            updated_at = now()
                        WHERE id = %s AND owner_id = %s;
                        """,
                        (memory_id, related_memory_id, owner_id),
                    )

                primary_reason = bounded_transition_reason(
                    code=reason_code,
                    metadata=reason_metadata,
                    request_id=request_id,
                    previous_status=current["status"],
                    new_status=new_status,
                    related_memory_id=str(related_memory_id) if related_memory_id else None,
                )
                await cur.execute(
                    """
                    INSERT INTO memory_events (memory_id, owner_id, event_type, reason_json)
                    VALUES (%s, %s, 'state_changed', %s::jsonb);
                    """,
                    (memory_id, owner_id, Json(primary_reason)),
                )

                if related_changed_status:
                    related_reason = bounded_transition_reason(
                        code="replaced_by_related_memory",
                        metadata={},
                        request_id=request_id,
                        previous_status=str(related_previous_status),
                        new_status="superseded",
                        related_memory_id=str(memory_id),
                    )
                    await cur.execute(
                        """
                        INSERT INTO memory_events (memory_id, owner_id, event_type, reason_json)
                        VALUES (%s, %s, 'state_changed', %s::jsonb);
                        """,
                        (related_memory_id, owner_id, Json(related_reason)),
                    )

        return {
            "memory": self._memory_item_from_row(updated_row),
            "changed": True,
            "events_appended": ["state_changed"],
        }

    async def get_memory_debug(self, memory_id: UUID, owner_id: str) -> dict[str, Any] | None:
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
                    f"SELECT {select_cols} FROM memory_items WHERE id = %s AND owner_id = %s;",
                    (memory_id, owner_id),
                )
                row = await cur.fetchone()
                if row is None:
                    return None
                await cur.execute(
                    """
                    SELECT id, memory_id, owner_id, event_type, reason_json, created_at
                    FROM memory_events
                    WHERE memory_id = %s AND owner_id = %s
                    ORDER BY created_at ASC, id ASC;
                    """,
                    (memory_id, owner_id),
                )
                event_rows = await cur.fetchall()
        return {
            "memory": self._memory_item_from_row(row),
            "events": [self._memory_event_from_row(event_row) for event_row in event_rows],
        }

    async def append_memory_lifecycle_event(
        self,
        *,
        memory_id: UUID,
        owner_id: str,
        event_type: str,
        reason_json: dict[str, Any],
    ) -> dict[str, Any] | None:
        select_cols = """
            id, owner_id, memory_type, summary, source_refs_json, source_ref_hash,
            scores_json, promotion_state, status, supersedes_memory_id,
            superseded_by_memory_id, last_reinforced_at, expires_at,
            derivation_version, confidence, explanation_json, generation_trace_id,
            created_at, updated_at
        """
        request_id = reason_json.get("request_id")
        reason_code = reason_json.get("reason_code")
        async with self.pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    f"SELECT {select_cols} FROM memory_items WHERE id = %s AND owner_id = %s FOR UPDATE;",
                    (memory_id, owner_id),
                )
                row = await cur.fetchone()
                if row is None:
                    return None
                await cur.execute(
                    """
                    SELECT id FROM memory_events
                    WHERE memory_id = %s
                      AND owner_id = %s
                      AND event_type = %s
                      AND reason_json->>'request_id' IS NOT DISTINCT FROM %s
                      AND reason_json->>'reason_code' IS NOT DISTINCT FROM %s
                    LIMIT 1;
                    """,
                    (memory_id, owner_id, event_type, request_id, reason_code),
                )
                existing = await cur.fetchone()
                if existing is None:
                    await cur.execute(
                        """
                        INSERT INTO memory_events (memory_id, owner_id, event_type, reason_json)
                        VALUES (%s, %s, %s, %s::jsonb);
                        """,
                        (memory_id, owner_id, event_type, Json(reason_json)),
                    )
        return await self.get_memory_debug(memory_id, owner_id)


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
        derivation_version: str = EPISODE_DERIVATION_VERSION,
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

    async def get_episode_debug(self, episode_id: UUID, owner_id: str) -> dict[str, Any] | None:
        select_cols = """
            id, owner_id, title, summary, episode_type, trigger_json,
            outcome, significance, unresolved_json, source_refs_json,
            source_ref_hash, episode_key, callback_candidates_json,
            time_window_json, participants_json, status, derivation_version,
            confidence, explanation_json, generation_trace_id, created_at, updated_at
        """
        async with self.pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    f"SELECT {select_cols} FROM episodes WHERE id = %s AND owner_id = %s;",
                    (episode_id, owner_id),
                )
                row = await cur.fetchone()
                if row is None:
                    return None
                await cur.execute(
                    """
                    SELECT id, episode_id, owner_id, ref_type, ref_id, relationship, created_at
                    FROM episode_links
                    WHERE episode_id = %s AND owner_id = %s
                    ORDER BY created_at ASC, id ASC;
                    """,
                    (episode_id, owner_id),
                )
                link_rows = await cur.fetchall()
                await cur.execute(
                    """
                    SELECT id, episode_id, owner_id, event_type, reason_json, created_at
                    FROM episode_events
                    WHERE episode_id = %s AND owner_id = %s
                    ORDER BY created_at ASC, id ASC;
                    """,
                    (episode_id, owner_id),
                )
                event_rows = await cur.fetchall()
        return {
            "episode": self._episode_from_row(row),
            "links": [self._episode_link_from_row(link_row) for link_row in link_rows],
            "events": [self._episode_event_from_row(event_row) for event_row in event_rows],
        }

    async def list_episode_candidates(
        self,
        *,
        owner_id: str,
        limit: int,
    ) -> list[dict[str, Any]]:
        select_cols = """
            id, owner_id, title, summary, episode_type, trigger_json,
            outcome, significance, unresolved_json, source_refs_json,
            source_ref_hash, episode_key, callback_candidates_json,
            time_window_json, participants_json, status, derivation_version,
            confidence, explanation_json, generation_trace_id, created_at, updated_at
        """
        async with self.pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    f"""
                    SELECT {select_cols}
                    FROM episodes
                    WHERE owner_id = %s AND status = 'active'
                    ORDER BY updated_at DESC, id ASC
                    LIMIT %s;
                    """,
                    (owner_id, max(1, min(100, limit))),
                )
                rows = await cur.fetchall()
        return [self._episode_from_row(row) for row in rows]

    async def transition_episode_status(
        self,
        *,
        episode_id: UUID,
        owner_id: str,
        new_status: str,
        request_id: str,
        reason_json: dict[str, Any],
    ) -> dict[str, Any] | None:
        select_cols = """
            id, owner_id, title, summary, episode_type, trigger_json,
            outcome, significance, unresolved_json, source_refs_json,
            source_ref_hash, episode_key, callback_candidates_json,
            time_window_json, participants_json, status, derivation_version,
            confidence, explanation_json, generation_trace_id, created_at, updated_at
        """
        async with self.pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    f"SELECT {select_cols} FROM episodes WHERE id = %s AND owner_id = %s FOR UPDATE;",
                    (episode_id, owner_id),
                )
                row = await cur.fetchone()
                if row is None:
                    return None
                current = self._episode_from_row(row)
                if current["status"] == new_status:
                    return {"episode": current, "changed": False}
                await cur.execute(
                    f"""
                    UPDATE episodes
                    SET status = %s,
                        updated_at = now()
                    WHERE id = %s AND owner_id = %s
                    RETURNING {select_cols};
                    """,
                    (new_status, episode_id, owner_id),
                )
                updated = await cur.fetchone()
                await cur.execute(
                    """
                    INSERT INTO episode_events (episode_id, owner_id, event_type, reason_json)
                    VALUES (%s, %s, 'updated', %s::jsonb);
                    """,
                    (episode_id, owner_id, Json({**reason_json, "request_id": request_id})),
                )
        return {"episode": self._episode_from_row(updated), "changed": True}

    async def append_episode_lifecycle_event(
        self,
        *,
        episode_id: UUID,
        owner_id: str,
        event_type: str,
        reason_json: dict[str, Any],
    ) -> dict[str, Any] | None:
        request_id = reason_json.get("request_id")
        reason_code = reason_json.get("reason_code")
        async with self.pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "SELECT id FROM episodes WHERE id = %s AND owner_id = %s FOR UPDATE;",
                    (episode_id, owner_id),
                )
                row = await cur.fetchone()
                if row is None:
                    return None
                await cur.execute(
                    """
                    SELECT id FROM episode_events
                    WHERE episode_id = %s
                      AND owner_id = %s
                      AND event_type = %s
                      AND reason_json->>'request_id' IS NOT DISTINCT FROM %s
                      AND reason_json->>'reason_code' IS NOT DISTINCT FROM %s
                    LIMIT 1;
                    """,
                    (episode_id, owner_id, event_type, request_id, reason_code),
                )
                existing = await cur.fetchone()
                if existing is None:
                    await cur.execute(
                        """
                        INSERT INTO episode_events (episode_id, owner_id, event_type, reason_json)
                        VALUES (%s, %s, %s, %s::jsonb);
                        """,
                        (episode_id, owner_id, event_type, Json(reason_json)),
                    )
        return await self.get_episode_debug(episode_id, owner_id)

    async def replace_episode(
        self,
        *,
        old_episode_id: UUID,
        owner_id: str,
        request_id: str,
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
        derivation_version: str,
        confidence: float | None,
        explanation_json: dict[str, Any],
        generation_trace_id: str,
    ) -> dict[str, Any]:
        select_cols = """
            id, owner_id, title, summary, episode_type, trigger_json,
            outcome, significance, unresolved_json, source_refs_json,
            source_ref_hash, episode_key, callback_candidates_json,
            time_window_json, participants_json, status, derivation_version,
            confidence, explanation_json, generation_trace_id, created_at, updated_at
        """
        async with self.pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    f"SELECT {select_cols} FROM episodes WHERE id = %s AND owner_id = %s FOR UPDATE;",
                    (old_episode_id, owner_id),
                )
                old_row = await cur.fetchone()
                if old_row is None:
                    raise KeyError("episode not found")
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
                new_row = await cur.fetchone()
                new_id = new_row[0]
                await cur.execute(
                    """
                    UPDATE episodes
                    SET status = 'superseded',
                        updated_at = now()
                    WHERE id = %s AND owner_id = %s;
                    """,
                    (old_episode_id, owner_id),
                )
                await cur.execute(
                    """
                    INSERT INTO episode_events (episode_id, owner_id, event_type, reason_json)
                    VALUES (%s, %s, 'updated', %s::jsonb), (%s, %s, 'created', %s::jsonb);
                    """,
                    (
                        old_episode_id,
                        owner_id,
                        Json({"request_id": request_id, "reason_code": "rebuild_replaced", "replacement_episode_id": str(new_id)}),
                        new_id,
                        owner_id,
                        Json({"request_id": request_id, "replaces_episode_id": str(old_episode_id)}),
                    ),
                )
        return {"episode": self._episode_from_row(new_row)}

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

    async def create_claim_record(
        self,
        *,
        record: dict[str, Any],
        validate_association: Callable[
            [dict[str, Any], dict[str, Any]],
            dict[str, Any] | None,
        ],
    ) -> dict[str, Any]:
        conversation_id = UUID(record["conversation_id"])
        assistant_message_id = UUID(record["assistant_message_id"])
        evidence = record["validated_evidence_references"]

        async with self.pool.connection() as conn:
            async with conn.transaction():
                async with conn.cursor() as cur:
                    await cur.execute(
                        "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0));",
                        (record["claim_id"],),
                    )
                    await cur.execute(
                        f"SELECT {_CLAIM_RECORD_COLUMNS} FROM claim_records WHERE claim_id = %s;",
                        (record["claim_id"],),
                    )
                    existing_row = await cur.fetchone()
                    existing = (
                        _claim_record_from_row(existing_row)
                        if existing_row is not None
                        else None
                    )

                    await cur.execute(
                        "SELECT owner_id FROM conversations WHERE id = %s LIMIT 1;",
                        (conversation_id,),
                    )
                    conversation = await cur.fetchone()

                    await cur.execute(
                        """
                        SELECT owner_id, conversation_id, role, metadata, content
                        FROM messages
                        WHERE id = %s
                        LIMIT 1;
                        """,
                        (assistant_message_id,),
                    )
                    message = await cur.fetchone()

                    await cur.execute(
                        """
                        SELECT owner_id, conversation_id, surface, status,
                               references_json, prompt_json
                        FROM traces
                        WHERE request_id = %s
                        LIMIT 1;
                        """,
                        (record["request_id"],),
                    )
                    trace = await cur.fetchone()

                    local_references: dict[tuple[str, str], dict[str, Any] | None] = {}
                    for reference in evidence:
                        ref_type = reference["ref_type"]
                        if ref_type not in {"message", "artifact", "derived_text"}:
                            continue
                        ref_id = UUID(reference["ref_id"])

                        if ref_type == "message":
                            await cur.execute(
                                """
                                SELECT owner_id, conversation_id
                                FROM messages
                                WHERE id = %s
                                LIMIT 1;
                                """,
                                (ref_id,),
                            )
                        elif ref_type == "artifact":
                            await cur.execute(
                                """
                                SELECT owner_id, conversation_id
                                FROM artifacts
                                WHERE id = %s
                                LIMIT 1;
                                """,
                                (ref_id,),
                            )
                        else:
                            await cur.execute(
                                """
                                SELECT a.owner_id, a.conversation_id
                                FROM derived_text dt
                                JOIN artifacts a ON a.id = dt.artifact_id
                                WHERE dt.id = %s
                                LIMIT 1;
                                """,
                                (ref_id,),
                            )
                        local_reference = await cur.fetchone()
                        local_references[(ref_type, reference["ref_id"])] = (
                            {
                                "owner_id": local_reference[0],
                                "conversation_id": (
                                    str(local_reference[1])
                                    if local_reference[1] is not None
                                    else None
                                ),
                            }
                            if local_reference is not None
                            else None
                        )

                    validated_existing = validate_association(
                        record,
                        {
                            "existing": existing,
                            "conversation": (
                                {"owner_id": conversation[0]}
                                if conversation is not None
                                else None
                            ),
                            "assistant_message": (
                                {
                                    "owner_id": message[0],
                                    "conversation_id": str(message[1]),
                                    "role": message[2],
                                    "metadata": message[3],
                                    "content": message[4],
                                }
                                if message is not None
                                else None
                            ),
                            "trace": (
                                {
                                    "owner_id": trace[0],
                                    "conversation_id": str(trace[1]),
                                    "surface": trace[2],
                                    "status": trace[3],
                                    "references": trace[4] if isinstance(trace[4], list) else [],
                                    "prompt": trace[5],
                                }
                                if trace is not None
                                else None
                            ),
                            "local_references": local_references,
                        },
                    )
                    if validated_existing is not None:
                        return {"created": False, "record": validated_existing}

                    await cur.execute(
                        """
                        INSERT INTO claim_records (
                            claim_id, schema_version, owner_id, conversation_id, request_id,
                            assistant_message_id, surface, runtime_session_id, runtime_turn_id,
                            acquisition_manifest_id,
                            claim_anchor, claim_anchor_digest, claim_class, calibration_status,
                            evidence_strength, confidence, strongest_authority, freshness_summary,
                            uncertainty_disclosure_required, evidence_references_json,
                            limitation_codes_json, user_safe_summary
                        ) VALUES (
                            %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                            %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
                        )
                        RETURNING
                        """
                        + _CLAIM_RECORD_COLUMNS,
                        (
                            record["claim_id"],
                            record["schema_version"],
                            record["owner_id"],
                            conversation_id,
                            record["request_id"],
                            assistant_message_id,
                            record["surface"],
                            record["runtime_session_id"],
                            record["runtime_turn_id"],
                            record["acquisition_manifest_id"],
                            record["claim_anchor"],
                            record["claim_anchor_digest"],
                            record["claim_class"],
                            record["calibration_status"],
                            record["evidence_strength"],
                            record["confidence"],
                            record["strongest_authority"],
                            record["freshness_summary"],
                            record["uncertainty_disclosure_required"],
                            Json(evidence),
                            Json(record["limitation_codes"]),
                            record["user_safe_summary"],
                        ),
                    )
                    inserted = await cur.fetchone()
                    return {"created": True, "record": _claim_record_from_row(inserted)}

    async def get_claim_record(
        self,
        *,
        claim_id: str,
        owner_id: str,
        conversation_id: str,
    ) -> dict[str, Any] | None:
        try:
            scoped_conversation_id = UUID(conversation_id)
        except (TypeError, ValueError):
            return None
        query = f"""
        SELECT {_CLAIM_RECORD_COLUMNS}
        FROM claim_records
        WHERE claim_id = %s AND owner_id = %s AND conversation_id = %s
        LIMIT 1;
        """
        async with self.pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(query, (claim_id, owner_id, scoped_conversation_id))
                row = await cur.fetchone()
        return _claim_record_from_row(row) if row else None

    async def list_claim_records(
        self,
        *,
        owner_id: str,
        conversation_id: str,
        assistant_message_id: str | None,
        request_id: str | None,
        limit: int,
    ) -> list[dict[str, Any]]:
        try:
            scoped_conversation_id = UUID(conversation_id)
            scoped_message_id = UUID(assistant_message_id) if assistant_message_id else None
        except (TypeError, ValueError):
            return []
        filters = ["cr.owner_id = %s", "cr.conversation_id = %s"]
        parameters: list[Any] = [owner_id, scoped_conversation_id]
        if scoped_message_id is not None:
            filters.append("cr.assistant_message_id = %s")
            parameters.append(scoped_message_id)
        if request_id is not None:
            filters.append("cr.request_id = %s")
            parameters.append(request_id)
        parameters.append(limit)
        query = f"""
        SELECT {', '.join('cr.' + column.strip() for column in _CLAIM_RECORD_COLUMNS.split(','))}
        FROM claim_records cr
        JOIN messages m ON m.id = cr.assistant_message_id
        WHERE {' AND '.join(filters)}
        ORDER BY m.created_at DESC, cr.created_at ASC, cr.claim_id ASC
        LIMIT %s;
        """
        async with self.pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(query, tuple(parameters))
                rows = await cur.fetchall()
        return [_claim_record_from_row(row) for row in rows]

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
