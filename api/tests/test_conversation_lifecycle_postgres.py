from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any
from uuid import UUID, uuid4

import psycopg
import pytest

from storage.postgres import (
    ConversationLifecycleConflictError,
    ConversationNotFoundError,
    ConversationNotOpenError,
    ConversationReplacementError,
    PostgresStore,
)


async def _use_store(
    dsn: str,
    operation: Callable[[PostgresStore], Awaitable[Any]],
) -> Any:
    store = PostgresStore(dsn)
    await store.open()
    try:
        return await operation(store)
    finally:
        await store.close()


def _run(dsn: str, operation: Callable[[PostgresStore], Awaitable[Any]]) -> Any:
    return asyncio.run(_use_store(dsn, operation))


def _row(dsn: str, conversation_id: UUID) -> tuple[Any, ...]:
    with psycopg.connect(dsn) as conn:
        return conn.execute(
            """
            SELECT lifecycle_state, superseded_by_conversation_id, updated_at
            FROM conversations
            WHERE id = %s
            """,
            (conversation_id,),
        ).fetchone()


def _message_count(dsn: str, conversation_id: UUID) -> int:
    with psycopg.connect(dsn) as conn:
        return conn.execute(
            "SELECT count(*) FROM messages WHERE conversation_id = %s",
            (conversation_id,),
        ).fetchone()[0]


def test_creation_exact_lookup_and_owner_scoped_lifecycle_listing(postgres_database):
    async def exercise(store: PostgresStore):
        open_id = await store.create_conversation("owner-alpha", "client-one", "open row")
        closed_id = await store.create_conversation("owner-alpha", "client-one", "closed row")
        replacement_id = await store.create_conversation("owner-alpha", "client-two", "replacement")
        superseded_id = await store.create_conversation("owner-alpha", "client-one", "superseded row")
        foreign_id = await store.create_conversation("owner-beta", "client-one", "foreign row")
        await store.transition_conversation_lifecycle(
            conversation_id=closed_id,
            owner_id="owner-alpha",
            lifecycle_state="closed",
            superseded_by_conversation_id=None,
        )
        await store.transition_conversation_lifecycle(
            conversation_id=superseded_id,
            owner_id="owner-alpha",
            lifecycle_state="superseded",
            superseded_by_conversation_id=replacement_id,
        )
        exact = await store.get_conversation_for_owner(open_id, "owner-alpha")
        hidden = await store.get_conversation_for_owner(open_id, "owner-beta")
        open_rows, _ = await store.list_conversations(
            "owner-alpha",
            client_id="client-one",
            lifecycle_state="open",
            limit=1,
        )
        closed_rows, _ = await store.list_conversations(
            "owner-alpha",
            client_id="client-one",
            lifecycle_state="closed",
            limit=1,
        )
        superseded_rows, _ = await store.list_conversations(
            "owner-alpha",
            client_id="client-one",
            lifecycle_state="superseded",
            limit=1,
        )
        foreign_rows, _ = await store.list_conversations("owner-alpha", limit=20)
        return {
            "ids": (open_id, closed_id, replacement_id, superseded_id, foreign_id),
            "exact": exact,
            "hidden": hidden,
            "open": open_rows,
            "closed": closed_rows,
            "superseded": superseded_rows,
            "all": foreign_rows,
        }

    result = _run(postgres_database, exercise)
    open_id, closed_id, replacement_id, superseded_id, foreign_id = result["ids"]
    assert result["exact"]["conversation_id"] == str(open_id)
    assert result["exact"]["lifecycle_state"] == "open"
    assert result["exact"]["superseded_by_conversation_id"] is None
    assert result["hidden"] is None
    assert [row["conversation_id"] for row in result["open"]] == [str(open_id)]
    assert [row["conversation_id"] for row in result["closed"]] == [str(closed_id)]
    assert [row["conversation_id"] for row in result["superseded"]] == [str(superseded_id)]
    assert result["superseded"][0]["superseded_by_conversation_id"] == str(replacement_id)
    assert str(foreign_id) not in {row["conversation_id"] for row in result["all"]}


def test_lifecycle_filter_is_applied_before_limit(postgres_database):
    async def seed(store: PostgresStore):
        selected_id = await store.create_conversation("owner-filter", "client-one", "selected")
        newer_id = await store.create_conversation("owner-filter", "client-one", "newer")
        await store.transition_conversation_lifecycle(
            conversation_id=newer_id,
            owner_id="owner-filter",
            lifecycle_state="closed",
            superseded_by_conversation_id=None,
        )
        rows, _ = await store.list_conversations(
            "owner-filter",
            lifecycle_state="open",
            limit=1,
        )
        return selected_id, rows

    selected_id, rows = _run(postgres_database, seed)
    assert [row["conversation_id"] for row in rows] == [str(selected_id)]


def test_allowed_idempotent_and_terminal_lifecycle_transitions(postgres_database):
    async def exercise(store: PostgresStore):
        source_id = await store.create_conversation("owner-state", "client-one", "source")
        replacement_id = await store.create_conversation("owner-state", "client-two", "replacement")
        closed_source_id = await store.create_conversation("owner-state", "client-three", "closed source")

        closed = await store.transition_conversation_lifecycle(
            conversation_id=source_id,
            owner_id="owner-state",
            lifecycle_state="closed",
            superseded_by_conversation_id=None,
        )
        repeated_closed = await store.transition_conversation_lifecycle(
            conversation_id=source_id,
            owner_id="owner-state",
            lifecycle_state="closed",
            superseded_by_conversation_id=None,
        )
        reopened = await store.transition_conversation_lifecycle(
            conversation_id=source_id,
            owner_id="owner-state",
            lifecycle_state="open",
            superseded_by_conversation_id=None,
        )
        superseded = await store.transition_conversation_lifecycle(
            conversation_id=source_id,
            owner_id="owner-state",
            lifecycle_state="superseded",
            superseded_by_conversation_id=replacement_id,
        )
        repeated_superseded = await store.transition_conversation_lifecycle(
            conversation_id=source_id,
            owner_id="owner-state",
            lifecycle_state="superseded",
            superseded_by_conversation_id=replacement_id,
        )
        await store.transition_conversation_lifecycle(
            conversation_id=closed_source_id,
            owner_id="owner-state",
            lifecycle_state="closed",
            superseded_by_conversation_id=None,
        )
        closed_to_superseded = await store.transition_conversation_lifecycle(
            conversation_id=closed_source_id,
            owner_id="owner-state",
            lifecycle_state="superseded",
            superseded_by_conversation_id=replacement_id,
        )
        with pytest.raises(ConversationLifecycleConflictError):
            await store.transition_conversation_lifecycle(
                conversation_id=source_id,
                owner_id="owner-state",
                lifecycle_state="closed",
                superseded_by_conversation_id=None,
            )
        return closed, repeated_closed, reopened, superseded, repeated_superseded, closed_to_superseded

    closed, repeated_closed, reopened, superseded, repeated_superseded, closed_to_superseded = _run(
        postgres_database,
        exercise,
    )
    assert closed["lifecycle_state"] == repeated_closed["lifecycle_state"] == "closed"
    assert closed["updated_at"] == repeated_closed["updated_at"]
    assert reopened["lifecycle_state"] == "open"
    assert superseded["lifecycle_state"] == "superseded"
    assert superseded == repeated_superseded
    assert closed_to_superseded["lifecycle_state"] == "superseded"


def test_invalid_replacements_and_rejected_transitions_leave_source_unchanged(postgres_database):
    async def exercise(store: PostgresStore):
        source_id = await store.create_conversation("owner-source", "client-one", "source")
        foreign_id = await store.create_conversation("owner-foreign", "client-one", "foreign")
        closed_id = await store.create_conversation("owner-source", "client-two", "closed")
        await store.transition_conversation_lifecycle(
            conversation_id=closed_id,
            owner_id="owner-source",
            lifecycle_state="closed",
            superseded_by_conversation_id=None,
        )
        invalid_ids = [uuid4(), source_id, foreign_id, closed_id]
        for replacement_id in invalid_ids:
            before = await store.get_conversation_for_owner(source_id, "owner-source")
            with pytest.raises(ConversationReplacementError):
                await store.transition_conversation_lifecycle(
                    conversation_id=source_id,
                    owner_id="owner-source",
                    lifecycle_state="superseded",
                    superseded_by_conversation_id=replacement_id,
                )
            after = await store.get_conversation_for_owner(source_id, "owner-source")
            assert after == before
        with pytest.raises(ConversationNotFoundError):
            await store.transition_conversation_lifecycle(
                conversation_id=source_id,
                owner_id="owner-hidden",
                lifecycle_state="closed",
                superseded_by_conversation_id=None,
            )
        with pytest.raises(ConversationNotFoundError):
            await store.transition_conversation_lifecycle(
                conversation_id=uuid4(),
                owner_id="owner-source",
                lifecycle_state="closed",
                superseded_by_conversation_id=None,
            )
        return source_id

    source_id = _run(postgres_database, exercise)
    assert _row(postgres_database, source_id)[0:2] == ("open", None)


def test_rolling_resolver_reuses_only_recent_open_same_client_conversations(postgres_database):
    async def exercise(store: PostgresStore):
        open_id = await store.create_conversation("owner-open", "client-one", "open")
        reused_id, reused = await store.resolve_conversation("owner-open", "client-one")

        closed_id = await store.create_conversation("owner-closed", "client-one", "closed")
        await store.transition_conversation_lifecycle(
            conversation_id=closed_id,
            owner_id="owner-closed",
            lifecycle_state="closed",
            superseded_by_conversation_id=None,
        )
        after_closed_id, closed_reused = await store.resolve_conversation("owner-closed", "client-one")

        superseded_id = await store.create_conversation("owner-superseded", "client-one", "superseded")
        replacement_id = await store.create_conversation("owner-superseded", "client-two", "replacement")
        await store.transition_conversation_lifecycle(
            conversation_id=superseded_id,
            owner_id="owner-superseded",
            lifecycle_state="superseded",
            superseded_by_conversation_id=replacement_id,
        )
        after_superseded_id, superseded_reused = await store.resolve_conversation(
            "owner-superseded",
            "client-one",
        )
        cross_client_id, cross_client_reused = await store.resolve_conversation(
            "owner-open",
            "client-two",
        )
        foreign_id, foreign_reused = await store.resolve_conversation(
            "owner-foreign",
            "client-one",
        )
        return locals()

    result = _run(postgres_database, exercise)
    assert (result["reused_id"], result["reused"]) == (result["open_id"], True)
    assert result["closed_reused"] is False and result["after_closed_id"] != result["closed_id"]
    assert result["superseded_reused"] is False
    assert result["after_superseded_id"] not in {result["superseded_id"], result["replacement_id"]}
    assert result["cross_client_reused"] is False and result["cross_client_id"] != result["open_id"]
    assert result["foreign_reused"] is False and result["foreign_id"] != result["open_id"]


def test_message_append_owner_and_open_state_checks_are_atomic(postgres_database):
    async def exercise(store: PostgresStore):
        open_id = await store.create_conversation("owner-message", "client-one", "open")
        closed_id = await store.create_conversation("owner-message", "client-one", "closed")
        superseded_id = await store.create_conversation("owner-message", "client-one", "superseded")
        replacement_id = await store.create_conversation("owner-message", "client-two", "replacement")
        await store.transition_conversation_lifecycle(
            conversation_id=closed_id,
            owner_id="owner-message",
            lifecycle_state="closed",
            superseded_by_conversation_id=None,
        )
        await store.transition_conversation_lifecycle(
            conversation_id=superseded_id,
            owner_id="owner-message",
            lifecycle_state="superseded",
            superseded_by_conversation_id=replacement_id,
        )
        with psycopg.connect(postgres_database) as conn:
            conn.execute(
                "UPDATE conversations SET updated_at = '2026-01-01T00:00:00Z' WHERE id = ANY(%s)",
                ([open_id, closed_id, superseded_id],),
            )
            conn.commit()

        before = {
            conversation_id: _row(postgres_database, conversation_id)[2]
            for conversation_id in (open_id, closed_id, superseded_id)
        }
        message_id = await store.add_message(
            open_id,
            "owner-message",
            "user",
            "accepted content",
            "client-one",
        )
        with pytest.raises(ConversationNotFoundError):
            await store.add_message(
                uuid4(),
                "owner-message",
                "user",
                "missing content",
                "client-one",
            )
        with pytest.raises(ConversationNotFoundError):
            await store.add_message(
                open_id,
                "owner-hidden",
                "user",
                "hidden content",
                "client-one",
            )
        with pytest.raises(ConversationNotOpenError):
            await store.add_message(
                closed_id,
                "owner-message",
                "user",
                "closed content",
                "client-one",
            )
        with pytest.raises(ConversationNotOpenError):
            await store.add_message(
                superseded_id,
                "owner-message",
                "user",
                "superseded content",
                "client-one",
            )
        return message_id, open_id, closed_id, superseded_id, before

    message_id, open_id, closed_id, superseded_id, before = _run(postgres_database, exercise)
    assert isinstance(message_id, UUID)
    assert _message_count(postgres_database, open_id) == 1
    assert _message_count(postgres_database, closed_id) == 0
    assert _message_count(postgres_database, superseded_id) == 0
    assert _row(postgres_database, open_id)[2] > before[open_id]
    assert _row(postgres_database, closed_id)[2] == before[closed_id]
    assert _row(postgres_database, superseded_id)[2] == before[superseded_id]
