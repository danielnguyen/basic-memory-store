from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta, timezone
from typing import Any
from uuid import UUID, uuid4

import psycopg
import pytest

from storage.postgres import (
    ConversationLifecycleConflictError,
    ConversationNotFoundError,
    ConversationNotOpenError,
    ConversationReplacementError,
    MessageAppendConflictError,
    PostgresStore,
    WorkError,
)


async def _new_work(store, *, owner="owner-work", client="client-work", request="request-work"):
    conversation = await store.create_conversation(owner, client, "private-title-sentinel")
    work = await store.create_work(
        owner_id=owner, conversation_id=conversation, request_id=request,
        client_id=client, surface="web",
    )
    return work


async def _change_work(store, work, state, **result):
    return await store.transition_work(
        work_id=work["work_id"], owner_id=work["owner_id"],
        conversation_id=work["conversation_id"], state=state, **result,
    )


@pytest.mark.parametrize("has_prior_assistant", [True, False])
def test_pending_work_does_not_replace_immediate_assistant_history(
    postgres_database, has_prior_assistant,
):
    owner = "owner-history-work"
    pending_request = "request-pending-work-sentinel"
    newest_request = "request-canonical-newest"
    newest_content = "CANONICAL-NEWEST-SENTINEL: the recorded setting is active."

    async def seed(store):
        conversation = await store.create_conversation(owner, "client-history")
        newest_message = None
        if has_prior_assistant:
            for request, content in [
                ("request-canonical-older", "CANONICAL-OLDER-SENTINEL: the setting was inactive."),
                (newest_request, newest_content),
            ]:
                newest_message = await store.add_message(
                    conversation, owner, "assistant", content,
                    metadata={"request_id": request},
                )
                await store.create_trace({
                    "owner_id": owner, "conversation_id": conversation,
                    "request_id": request, "surface": "web", "status": "ok",
                })
        # Newer messages outside the exact owner/conversation are never candidates.
        other_conversation = await store.create_conversation(owner, "other-client")
        foreign_conversation = await store.create_conversation("foreign-owner", "client-history")
        for scope_owner, scope_conversation in [
            (owner, other_conversation), ("foreign-owner", foreign_conversation),
        ]:
            await store.add_message(
                scope_conversation, scope_owner, "assistant", "OTHER-SCOPE-SENTINEL",
                metadata={"request_id": "request-other-scope"},
            )
        return conversation, newest_message, other_conversation, foreign_conversation

    conversation, newest_message, other_conversation, foreign_conversation = _run(
        postgres_database, seed,
    )

    async def immediate_candidate(store):
        # This is the exact bounded storage query used by both immediate-history resolvers.
        return await store.list_assistant_trace_candidates(
            owner_id=owner, conversation_id=conversation, limit=1,
        )

    before = _run(postgres_database, immediate_candidate)
    if has_prior_assistant:
        assert len(before) == 1
        assert before[0]["message_id"] == str(newest_message)
        assert before[0]["message_content"] == newest_content
        assert before[0]["message_request_id"] == before[0]["trace_request_id"] == newest_request
        assert before[0]["message_owner_id"] == before[0]["trace_owner_id"] == owner
        assert before[0]["message_conversation_id"] == str(conversation)
        assert before[0]["trace_conversation_id"] == str(conversation)
        assert before[0]["message_role"] == "assistant"
        assert before[0]["trace_status"] == "ok"
    else:
        assert before == []

    with psycopg.connect(postgres_database) as conn:
        messages_before = conn.execute("SELECT * FROM messages ORDER BY id").fetchall()
    count_before = _message_count(postgres_database, conversation)
    pending = _run(postgres_database, lambda store: store.create_work(
        owner_id=owner, conversation_id=conversation, request_id=pending_request,
        client_id="client-history", surface="web",
    ))
    # Reopen storage: the assertions cannot pass merely because a fake retained local state.
    loaded = _run(postgres_database, lambda store: store.get_work(
        owner_id=owner, conversation_id=conversation, work_id=pending["work_id"],
    ))
    assert loaded == pending
    assert loaded["state"] == "pending"
    assert loaded["assistant_message_id"] is None
    assert loaded["started_at"] is loaded["completed_at"] is loaded["failure_code"] is None
    assert set(loaded) == {
        "work_id", "owner_id", "conversation_id", "request_id", "client_id", "surface",
        "state", "created_at", "started_at", "completed_at", "assistant_message_id", "failure_code",
    }
    assert "SENTINEL: the" not in json.dumps(loaded, default=str)
    assert _message_count(postgres_database, conversation) == count_before
    with psycopg.connect(postgres_database) as conn:
        assert conn.execute("SELECT * FROM messages ORDER BY id").fetchall() == messages_before
        assistant_contents = conn.execute(
            "SELECT content FROM messages WHERE role = 'assistant'",
        ).fetchall()
    for (content,) in assistant_contents:
        assert all(value not in content for value in (
            "still working", "pending", "running", str(pending["work_id"]), pending_request,
        ))
    after = _run(postgres_database, immediate_candidate)
    assert after == before
    serialized = json.dumps(after, default=str)
    assert str(pending["work_id"]) not in serialized
    assert pending_request not in serialized
    assert "pending" not in serialized

    async def isolated(store):
        for scoped_owner, scoped_conversation in [
            ("foreign-owner", conversation), (owner, foreign_conversation),
        ]:
            assert await store.list_assistant_trace_candidates(
                owner_id=scoped_owner, conversation_id=scoped_conversation, limit=1,
            ) == []
        for scoped_owner, scoped_conversation in [
            ("foreign-owner", conversation), (owner, other_conversation),
        ]:
            assert await store.get_work(
                owner_id=scoped_owner, conversation_id=scoped_conversation,
                work_id=pending["work_id"],
            ) is None

    _run(postgres_database, isolated)


def test_work_create_replay_and_restart_preserve_exact_associations(postgres_database):
    async def exercise(store):
        work = await _new_work(store, client=None)
        replay = await store.create_work(**{
            key: work[key] for key in
            ("owner_id", "conversation_id", "request_id", "client_id", "surface")
        })
        assert replay == work
        assert work["state"] == "pending"
        assert work["client_id"] is None
        assert work["started_at"] is work["completed_at"] is None
        assert work["assistant_message_id"] is work["failure_code"] is None
        for changes in (
            {"client_id": "another-client"}, {"surface": "voice"},
            {"conversation_id": await store.create_conversation("owner-work")},
        ):
            values = {key: work[key] for key in
                      ("owner_id", "conversation_id", "request_id", "client_id", "surface")}
            with pytest.raises(WorkError, match="work_conflict"):
                await store.create_work(**(values | changes))
        return work

    work = _run(postgres_database, exercise)
    loaded = _run(postgres_database, lambda store: store.get_work(
        work_id=work["work_id"], owner_id=work["owner_id"],
        conversation_id=work["conversation_id"],
    ))
    assert loaded == work
    assert _message_count(postgres_database, work["conversation_id"]) == 0


async def _retire_work_conversation(store, work, lifecycle):
    replacement = (
        await store.create_conversation(work["owner_id"], "replacement-client")
        if lifecycle == "superseded" else None
    )
    await store.transition_conversation_lifecycle(
        conversation_id=work["conversation_id"], owner_id=work["owner_id"],
        lifecycle_state=lifecycle, superseded_by_conversation_id=replacement,
    )


@pytest.mark.parametrize("invalid", ["missing", "owner", "closed", "superseded"])
def test_work_creation_rejects_invalid_conversation(postgres_database, invalid):
    async def exercise(store):
        cid = await store.create_conversation("owner-work")
        if invalid in {"closed", "superseded"}:
            await _retire_work_conversation(
                store, {"conversation_id": cid, "owner_id": "owner-work"}, invalid,
            )
        with pytest.raises(WorkError):
            await store.create_work(
                owner_id="wrong-owner" if invalid == "owner" else "owner-work",
                conversation_id=uuid4() if invalid == "missing" else cid,
                request_id="request-work", client_id=None, surface="web",
            )
    _run(postgres_database, exercise)


@pytest.mark.parametrize("start", ["pending", "running", "completed", "failed"])
@pytest.mark.parametrize("target", ["pending", "running", "completed", "failed"])
def test_work_transition_matrix(postgres_database, start, target):
    async def exercise(store):
        work = await _new_work(store)
        message = await store.add_message(
            work["conversation_id"], work["owner_id"], "assistant", "answer-body-sentinel",
        )
        if start in {"running", "completed"}:
            work = await _change_work(store, work, "running")
        if start == "completed":
            work = await _change_work(store, work, start, assistant_message_id=message)
        elif start == "failed":
            work = await _change_work(store, work, start, failure_code="interrupted")
        result = (
            {"assistant_message_id": message} if target == "completed"
            else {"failure_code": "interrupted"} if target == "failed" else {}
        )
        allowed = start == target or (start, target) in {
            ("pending", "running"), ("pending", "failed"),
            ("running", "completed"), ("running", "failed"),
        }
        if not allowed:
            with pytest.raises(WorkError, match="work_conflict"):
                await _change_work(store, work, target, **result)
        else:
            updated = await _change_work(store, work, target, **result)
            assert updated["state"] == target
            if start == target:
                assert updated == work
            if target in {"completed", "failed"}:
                assert updated["completed_at"] >= updated["created_at"]
            assert "answer-body-sentinel" not in str(updated)
    _run(postgres_database, exercise)


@pytest.mark.parametrize("lifecycle", ["open", "closed", "superseded"])
@pytest.mark.parametrize("invalid", ["missing", "user", "owner", "conversation"])
def test_work_completion_validates_canonical_assistant(postgres_database, invalid, lifecycle):
    async def exercise(store):
        work = await _new_work(store)
        await _change_work(store, work, "running")
        owner = "other-owner" if invalid == "owner" else work["owner_id"]
        cid = work["conversation_id"]
        if invalid in {"owner", "conversation"}:
            cid = await store.create_conversation(owner)
        message = await store.add_message(
            cid, owner, "user" if invalid == "user" else "assistant", "private-answer-sentinel",
        )
        if lifecycle != "open":
            await _retire_work_conversation(store, work, lifecycle)
        with pytest.raises(WorkError, match="work_result_invalid"):
            await _change_work(
                store, work, "completed",
                assistant_message_id=uuid4() if invalid == "missing" else message,
            )
        loaded = await store.get_work(
            work_id=work["work_id"], owner_id=work["owner_id"],
            conversation_id=work["conversation_id"],
        )
        assert loaded["state"] == "running"
    _run(postgres_database, exercise)


def test_work_concurrent_creation_and_conflicting_finalization(postgres_database):
    async def exercise(store):
        cid = await store.create_conversation("owner-work", "client-work")
        values = {
            "owner_id": "owner-work", "conversation_id": cid, "request_id": "same-request",
            "client_id": "client-work", "surface": "web",
        }
        created = await asyncio.gather(*(store.create_work(**values) for _ in range(8)))
        assert len({work["work_id"] for work in created}) == 1
        work = created[0]
        await _change_work(store, work, "running")
        messages = [
            await store.add_message(cid, "owner-work", "assistant", "canonical-answer-sentinel")
            for _ in range(2)
        ]
        results = await asyncio.gather(*(
            _change_work(store, work, "completed", assistant_message_id=message)
            for message in messages
        ), return_exceptions=True)
        winners = [result for result in results if isinstance(result, dict)]
        assert len(winners) == 1
        assert sum(isinstance(result, WorkError) for result in results) == 1
        winner = winners[0]
        assert await _change_work(
            store, work, "completed", assistant_message_id=winner["assistant_message_id"],
        ) == winner
        with pytest.raises(WorkError, match="work_conflict"):
            await _change_work(store, work, "failed", failure_code="execution_failed")
        return winner
    winner = _run(postgres_database, exercise)
    with psycopg.connect(postgres_database) as conn:
        row = conn.execute(
            "SELECT row_to_json(w) FROM work_items w WHERE work_id = %s",
            (winner["work_id"],),
        ).fetchone()[0]
    assert set(row) == {
        "work_id", "owner_id", "conversation_id", "request_id", "client_id", "surface",
        "state", "created_at", "started_at", "completed_at", "assistant_message_id", "failure_code",
    }
    assert "canonical-answer-sentinel" not in str(row)


def test_work_terminal_timestamp_uses_update_time_not_transaction_start(postgres_database, monkeypatch):
    async def exercise(store):
        work = await _new_work(store)
        async with store.pool.connection() as waiting_conn, waiting_conn.transaction():
            await waiting_conn.execute("SELECT now()")
            running = await _change_work(store, work, "running")

            @asynccontextmanager
            async def waiting_connection():
                yield waiting_conn

            with monkeypatch.context() as patch:
                patch.setattr(store.pool, "connection", waiting_connection)
                failed = await _change_work(store, work, "failed", failure_code="interrupted")
            assert failed["completed_at"] >= running["started_at"]
    _run(postgres_database, exercise)


def test_work_failed_replay_conflicts_and_result_cannot_be_reused(postgres_database):
    async def exercise(store):
        work = await _new_work(store)
        failed = await _change_work(store, work, "failed", failure_code="interrupted")
        assert await _change_work(store, work, "failed", failure_code="interrupted") == failed
        with pytest.raises(WorkError, match="work_conflict"):
            await _change_work(store, work, "failed", failure_code="execution_failed")
        values = {key: work[key] for key in
                  ("owner_id", "conversation_id", "request_id", "client_id", "surface")}
        assert await store.create_work(**values) == failed
        message = await store.add_message(
            work["conversation_id"], work["owner_id"], "assistant", "one canonical result",
        )
        for request in ["request-one", "request-two"]:
            other = await store.create_work(**(values | {"request_id": request}))
            await _change_work(store, other, "running")
            if request == "request-one":
                await _change_work(store, other, "completed", assistant_message_id=message)
            else:
                with pytest.raises(WorkError, match="work_conflict"):
                    await _change_work(store, other, "completed", assistant_message_id=message)
    _run(postgres_database, exercise)


@pytest.mark.parametrize("lifecycle", ["closed", "superseded"])
def test_work_exact_lookup_isolation_and_retirement(postgres_database, lifecycle):
    async def exercise(store):
        work = await _new_work(store)
        await _retire_work_conversation(store, work, lifecycle)
        for owner, cid in [("other-owner", work["conversation_id"]), ("owner-work", uuid4())]:
            assert await store.get_work(
                work_id=work["work_id"], owner_id=owner, conversation_id=cid,
            ) is None
            with pytest.raises(WorkError, match="work_not_found"):
                await store.transition_work(
                    work_id=work["work_id"], owner_id=owner, conversation_id=cid, state="running",
                )
        assert await store.get_work(
            work_id=work["work_id"], owner_id="owner-work",
            conversation_id=work["conversation_id"],
        ) == work
    _run(postgres_database, exercise)


@pytest.mark.parametrize("lifecycle", ["closed", "superseded"])
@pytest.mark.parametrize("initial,target", [
    ("pending", None), ("pending", "failed"), ("running", "failed"),
    ("running", "completed"), ("completed", None), ("failed", None),
])
def test_work_survives_retirement_without_resuming_conversation(
    postgres_database, lifecycle, initial, target,
):
    async def exercise(store):
        work = await _new_work(store)
        associations = {key: work[key] for key in
                        ("owner_id", "conversation_id", "request_id", "client_id", "surface")}
        other = await store.create_work(**(associations | {"request_id": "other-request"}))
        result = {}
        if initial == "completed" or target == "completed":
            result = {"assistant_message_id": await store.add_message(
                work["conversation_id"], work["owner_id"], "assistant", "canonical-result-sentinel",
            )}
        elif initial == "failed" or target == "failed":
            result = {"failure_code": "interrupted"}
        if initial in {"running", "completed"}:
            work = await _change_work(store, work, "running")
        if initial in {"completed", "failed"}:
            work = await _change_work(store, work, initial, **result)
        locator = {"owner_id": work["owner_id"], "client_id": work["client_id"]}
        await store.set_current_work(**locator, work_id=work["work_id"])
        await _retire_work_conversation(store, work, lifecycle)
        retired = _row(postgres_database, work["conversation_id"])
        before_count = _message_count(postgres_database, work["conversation_id"])
        assert await store.get_work(
            work_id=work["work_id"], owner_id=work["owner_id"],
            conversation_id=work["conversation_id"],
        ) == work
        assert await store.get_current_work(**locator) == work
        with pytest.raises(WorkError, match="work_conversation_not_open"):
            await store.create_work(**(associations | {"request_id": "new-after-retirement"}))
        for role in ("user", "assistant"):
            with pytest.raises(ConversationNotOpenError):
                await store.add_message(
                    work["conversation_id"], work["owner_id"], role, "must-not-be-appended",
                )
        assert await store.get_conversation_by_owner_client(**locator) is None
        if target is not None:
            work = await _change_work(store, work, target, **result)
            assert await _change_work(store, work, target, **result) == work
            with pytest.raises(WorkError, match="work_conflict"):
                await _change_work(store, work, "running")
        assert await store.get_current_work(**locator) == work
        assert await store.set_current_work(**locator, work_id=other["work_id"]) == other
        assert await store.get_current_work(**locator) == other
        assert await store.set_current_work(**locator, work_id=work["work_id"]) == work
        assert _row(postgres_database, work["conversation_id"]) == retired
        assert _message_count(postgres_database, work["conversation_id"]) == before_count
        return work

    work = _run(postgres_database, exercise)
    assert _run(postgres_database, lambda store: store.get_work(
        work_id=work["work_id"], owner_id=work["owner_id"],
        conversation_id=work["conversation_id"],
    )) == work
    assert _run(postgres_database, lambda store: store.get_current_work(
        owner_id=work["owner_id"], client_id=work["client_id"],
    )) == work


@pytest.mark.parametrize("terminal_state", ["completed", "failed"])
def test_current_work_explicit_locator_survives_restart_and_terminal_state(
    postgres_database, terminal_state,
):
    async def seed(store):
        first = await _new_work(store)
        assert await store.get_current_work(owner_id="owner-work", client_id="client-work") is None
        await store.set_current_work(
            owner_id="owner-work", client_id="client-work", work_id=first["work_id"],
        )
        newer = await _new_work(store, request="newer-request")
        resolved = await store.get_current_work(owner_id="owner-work", client_id="client-work")
        assert resolved == first  # Creating newer work does not change the explicit locator.
        for owner, client in [("other-owner", "client-work"), ("owner-work", "other-client")]:
            assert await store.get_current_work(owner_id=owner, client_id=client) is None
            with pytest.raises(WorkError, match="work_not_found"):
                await store.set_current_work(owner_id=owner, client_id=client, work_id=first["work_id"])
        await store.set_current_work(
            owner_id="owner-work", client_id="client-work", work_id=newer["work_id"],
        )
        if terminal_state == "completed":
            await _change_work(store, newer, "running")
            message = await store.add_message(
                newer["conversation_id"], newer["owner_id"], "assistant", "private-result-sentinel",
            )
            return await _change_work(store, newer, "completed", assistant_message_id=message)
        return await _change_work(store, newer, "failed", failure_code="interrupted")
    terminal = _run(postgres_database, seed)
    assert _run(postgres_database, lambda store: store.get_current_work(
        owner_id="owner-work", client_id="client-work",
    )) == terminal


def test_work_without_client_cannot_be_current_work(postgres_database):
    async def exercise(store):
        work = await _new_work(store, client=None)
        with pytest.raises(WorkError, match="work_not_found"):
            await store.set_current_work(
                owner_id=work["owner_id"], client_id="client-work", work_id=work["work_id"],
            )
    _run(postgres_database, exercise)


@pytest.mark.parametrize("lifecycle", ["open", "closed", "superseded"])
@pytest.mark.parametrize("corruption", ["owner", "client", "conversation", "dangling"])
def test_current_work_invalid_association_fails_closed(postgres_database, corruption, lifecycle):
    work = _run(postgres_database, lambda store: _new_work(store))
    _run(postgres_database, lambda store: store.set_current_work(
        owner_id="owner-work", client_id="client-work", work_id=work["work_id"],
    ))
    if lifecycle != "open":
        _run(postgres_database, lambda store: _retire_work_conversation(store, work, lifecycle))
    with psycopg.connect(postgres_database) as conn:
        if corruption == "dangling":
            conn.execute("ALTER TABLE current_work DROP CONSTRAINT current_work_work_id_fkey")
            conn.execute("UPDATE current_work SET work_id = %s", (uuid4(),))
        elif corruption == "conversation":
            conn.execute("UPDATE conversations SET owner_id = 'other-owner' WHERE id = %s",
                         (work["conversation_id"],))
        elif corruption == "client":
            conn.execute("UPDATE work_items SET client_id = 'other-client'")
        else:
            conn.execute("UPDATE work_items SET owner_id = 'other-owner'")
    with pytest.raises(WorkError, match="work_not_found"):
        _run(postgres_database, lambda store: store.get_current_work(
            owner_id="owner-work", client_id="client-work",
        ))


@pytest.mark.parametrize("corruption", ["deleted_conversation", "owner", "result_owner", "result_role"])
def test_retired_work_corruption_remains_hidden(postgres_database, corruption):
    async def seed(store):
        work = await _new_work(store)
        if corruption.startswith("result_"):
            message = await store.add_message(
                work["conversation_id"], work["owner_id"], "assistant", "private-result-sentinel",
            )
            await _change_work(store, work, "running")
            work = await _change_work(store, work, "completed", assistant_message_id=message)
        await store.set_current_work(
            owner_id=work["owner_id"], client_id=work["client_id"], work_id=work["work_id"],
        )
        await _retire_work_conversation(store, work, "closed")
        return work

    work = _run(postgres_database, seed)
    with psycopg.connect(postgres_database) as conn:
        if corruption == "deleted_conversation":
            conn.execute("DELETE FROM conversations WHERE id = %s", (work["conversation_id"],))
        elif corruption == "owner":
            conn.execute("UPDATE conversations SET owner_id = 'other-owner' WHERE id = %s",
                         (work["conversation_id"],))
        elif corruption == "result_owner":
            conn.execute("UPDATE messages SET owner_id = 'other-owner' WHERE id = %s",
                         (work["assistant_message_id"],))
        else:
            conn.execute("UPDATE messages SET role = 'user' WHERE id = %s",
                         (work["assistant_message_id"],))

    async def verify(store):
        assert await store.get_work(
            work_id=work["work_id"], owner_id=work["owner_id"],
            conversation_id=work["conversation_id"],
        ) is None
        with pytest.raises(WorkError, match="work_not_found"):
            if work["state"] == "completed":
                await _change_work(store, work, "completed",
                                   assistant_message_id=work["assistant_message_id"])
            else:
                await _change_work(store, work, "failed", failure_code="interrupted")
        with pytest.raises(WorkError, match="work_not_found"):
            await store.set_current_work(
                owner_id=work["owner_id"], client_id=work["client_id"], work_id=work["work_id"],
            )
        if corruption == "deleted_conversation":
            assert await store.get_current_work(
                owner_id=work["owner_id"], client_id=work["client_id"],
            ) is None
        else:
            with pytest.raises(WorkError, match="work_not_found"):
                await store.get_current_work(owner_id=work["owner_id"], client_id=work["client_id"])
    _run(postgres_database, verify)


@pytest.mark.parametrize("lifecycle", ["open", "closed", "superseded"])
def test_reconcile_interrupted_work_preserves_identity_terminals_and_locator(
    postgres_database, lifecycle,
):
    async def seed(store):
        pending = await _new_work(store, request="pending-request")
        await store.set_current_work(
            owner_id=pending["owner_id"], client_id=pending["client_id"],
            work_id=pending["work_id"],
        )
        running = await _new_work(store, owner="other-owner", request="running-request")
        running = await _change_work(store, running, "running")
        completed = await _new_work(store, request="completed-request")
        message = await store.add_message(
            completed["conversation_id"], completed["owner_id"], "assistant", "canonical-sentinel",
        )
        await _change_work(store, completed, "running")
        completed = await _change_work(store, completed, "completed", assistant_message_id=message)
        failed = await _new_work(store, request="failed-request")
        failed = await _change_work(store, failed, "failed", failure_code="execution_failed")
        if lifecycle != "open":
            for work in (pending, running):
                await _retire_work_conversation(store, work, lifecycle)
        return pending, running, completed, failed

    works = _run(postgres_database, seed)
    with psycopg.connect(postgres_database) as conn:
        conversations = conn.execute("SELECT * FROM conversations ORDER BY id").fetchall()
        messages = conn.execute("SELECT * FROM messages ORDER BY id").fetchall()
        locators = conn.execute("SELECT * FROM current_work ORDER BY owner_id, client_id").fetchall()
        before = conn.execute("SELECT clock_timestamp()").fetchone()[0]
    assert _run(postgres_database, lambda store: store.reconcile_interrupted_work()) == {
        "interrupted_count": 2,
    }
    with psycopg.connect(postgres_database) as conn:
        after = conn.execute("SELECT clock_timestamp()").fetchone()[0]

    async def readback(store):
        loaded = []
        for original in works:
            work = await store.get_work(
                work_id=original["work_id"], owner_id=original["owner_id"],
                conversation_id=original["conversation_id"],
            )
            if original["state"] in {"pending", "running"}:
                assert work == original | {
                    "state": "failed", "failure_code": "interrupted",
                    "completed_at": work["completed_at"],
                }
                assert before <= work["completed_at"] <= after
                assert work["assistant_message_id"] is None
            else:
                assert work == original
            loaded.append(work)
        assert loaded[0]["started_at"] is None
        assert loaded[1]["started_at"] == works[1]["started_at"]
        assert await store.get_current_work(
            owner_id=works[0]["owner_id"], client_id=works[0]["client_id"],
        ) == loaded[0]
        return loaded

    first = _run(postgres_database, readback)  # New pool/connection after the transaction.
    assert _run(postgres_database, lambda store: store.reconcile_interrupted_work()) == {
        "interrupted_count": 0,
    }
    assert _run(postgres_database, readback) == first
    with psycopg.connect(postgres_database) as conn:
        assert conn.execute("SELECT * FROM conversations ORDER BY id").fetchall() == conversations
        assert conn.execute("SELECT * FROM messages ORDER BY id").fetchall() == messages
        assert conn.execute(
            "SELECT * FROM current_work ORDER BY owner_id, client_id",
        ).fetchall() == locators


@pytest.mark.parametrize("corruption", ["owner", "missing_conversation", "started", "failure_code"])
def test_reconcile_interrupted_work_corruption_rolls_back_every_row(postgres_database, corruption):
    async def seed(store):
        await _new_work(store, request="first")
        await _new_work(store, request="second")
    _run(postgres_database, seed)
    with psycopg.connect(postgres_database) as conn:
        # Corrupt the last processed row, so rollback must undo the earlier valid update.
        work_id, conversation = conn.execute(
            "SELECT work_id, conversation_id FROM work_items ORDER BY conversation_id, work_id",
        ).fetchall()[-1]
        if corruption == "owner":
            conn.execute("UPDATE conversations SET owner_id = 'mismatch' WHERE id = %s",
                         (conversation,))
        elif corruption == "missing_conversation":
            conn.execute("SET LOCAL session_replication_role = replica")
            conn.execute("DELETE FROM conversations WHERE id = %s", (conversation,))
        else:
            conn.execute("ALTER TABLE work_items DROP CONSTRAINT work_items_state_fields_check")
            if corruption == "started":
                conn.execute("UPDATE work_items SET started_at = created_at WHERE work_id = %s",
                             (work_id,))
            else:
                conn.execute("UPDATE work_items SET failure_code = 'interrupted' WHERE work_id = %s",
                             (work_id,))
        before = conn.execute("SELECT * FROM work_items ORDER BY work_id").fetchall()
    with pytest.raises(WorkError, match="work_unavailable"):
        _run(postgres_database, lambda store: store.reconcile_interrupted_work())
    with psycopg.connect(postgres_database) as conn:
        assert conn.execute("SELECT * FROM work_items ORDER BY work_id").fetchall() == before


def test_reconcile_interrupted_work_concurrent_connections_transition_once(postgres_database):
    async def seed(store):
        pending = await _new_work(store, request="pending")
        running = await _new_work(store, request="running")
        await _change_work(store, running, "running")
        return pending, running
    works = _run(postgres_database, seed)

    async def race():
        ready = asyncio.Event()
        entered = 0

        async def reconcile(store):
            nonlocal entered
            entered += 1
            if entered == 2:
                ready.set()
            await ready.wait()
            return await store.reconcile_interrupted_work()

        return await asyncio.gather(*[
            _use_store(postgres_database, reconcile) for _ in range(2)
        ])

    results = asyncio.run(race())
    assert sorted(result["interrupted_count"] for result in results) == [0, 2]
    with psycopg.connect(postgres_database) as conn:
        rows = conn.execute(
            "SELECT work_id, state, failure_code, completed_at, assistant_message_id FROM work_items",
        ).fetchall()
    assert {row[0] for row in rows} == {work["work_id"] for work in works}
    assert all(row[1:3] == ("failed", "interrupted") for row in rows)
    assert all(row[3] is not None and row[4] is None for row in rows)


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


def _message_row(dsn: str, message_id: UUID) -> tuple[Any, ...] | None:
    with psycopg.connect(dsn) as conn:
        return conn.execute(
            """
            SELECT conversation_id, owner_id, client_id, role, content,
                   metadata, policy_metadata, created_at
            FROM messages
            WHERE id = %s
            """,
            (message_id,),
        ).fetchone()


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


def test_activity_filter_is_inclusive_and_applied_before_limit(postgres_database):
    cutoff = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)

    async def exercise(store: PostgresStore):
        older_id = await store.create_conversation("owner-activity", "client-one", "older")
        equal_id = await store.create_conversation("owner-activity", "client-one", "equal")
        newer_id = await store.create_conversation("owner-activity", "client-one", "newer")
        with psycopg.connect(postgres_database) as conn:
            conn.execute(
                "UPDATE conversations SET updated_at = %s WHERE id = %s",
                (datetime(2026, 1, 1, 11, 59, 59, tzinfo=UTC), older_id),
            )
            conn.execute(
                "UPDATE conversations SET updated_at = %s WHERE id = %s",
                (cutoff, equal_id),
            )
            conn.execute(
                "UPDATE conversations SET updated_at = %s WHERE id = %s",
                (datetime(2026, 1, 1, 12, 0, 1, tzinfo=UTC), newer_id),
            )
            conn.commit()
        before = {
            conversation_id: _row(postgres_database, conversation_id)[2]
            for conversation_id in (older_id, equal_id, newer_id)
        }
        filtered, _ = await store.list_conversations(
            "owner-activity",
            updated_since=cutoff,
            limit=2,
        )
        unfiltered, _ = await store.list_conversations("owner-activity", limit=3)
        after = {
            conversation_id: _row(postgres_database, conversation_id)[2]
            for conversation_id in (older_id, equal_id, newer_id)
        }
        return older_id, equal_id, newer_id, filtered, unfiltered, before, after

    older_id, equal_id, newer_id, filtered, unfiltered, before, after = _run(
        postgres_database,
        exercise,
    )
    assert [row["conversation_id"] for row in filtered] == [str(newer_id), str(equal_id)]
    assert [row["conversation_id"] for row in unfiltered] == [
        str(newer_id),
        str(equal_id),
        str(older_id),
    ]
    assert before == after


def test_activity_filter_composes_with_owner_lifecycle_and_client(postgres_database):
    cutoff = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)

    async def exercise(store: PostgresStore):
        selected_id = await store.create_conversation("owner-filtered", "client-one", "selected")
        wrong_owner_id = await store.create_conversation("owner-foreign", "client-one", "foreign")
        wrong_client_id = await store.create_conversation("owner-filtered", "client-two", "other client")
        closed_id = await store.create_conversation("owner-filtered", "client-one", "closed")
        await store.transition_conversation_lifecycle(
            conversation_id=closed_id,
            owner_id="owner-filtered",
            lifecycle_state="closed",
            superseded_by_conversation_id=None,
        )
        conversation_ids = (selected_id, wrong_owner_id, wrong_client_id, closed_id)
        with psycopg.connect(postgres_database) as conn:
            for offset, conversation_id in enumerate(conversation_ids):
                conn.execute(
                    "UPDATE conversations SET updated_at = %s WHERE id = %s",
                    (datetime(2026, 1, 1, 12, offset, tzinfo=UTC), conversation_id),
                )
            conn.commit()
        rows, _ = await store.list_conversations(
            "owner-filtered",
            client_id="client-one",
            lifecycle_state="open",
            updated_since=cutoff,
            limit=10,
        )
        return selected_id, rows

    selected_id, rows = _run(postgres_database, exercise)
    assert [row["conversation_id"] for row in rows] == [str(selected_id)]


def test_activity_filter_preserves_cursor_pagination(postgres_database):
    cutoff = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)

    async def exercise(store: PostgresStore):
        older_id = await store.create_conversation("owner-pages", "client-one", "older")
        equal_id = await store.create_conversation("owner-pages", "client-one", "equal")
        middle_id = await store.create_conversation("owner-pages", "client-one", "middle")
        newest_id = await store.create_conversation("owner-pages", "client-one", "newest")
        timestamps = {
            older_id: datetime(2026, 1, 1, 11, 59, tzinfo=UTC),
            equal_id: cutoff,
            middle_id: datetime(2026, 1, 1, 12, 1, tzinfo=UTC),
            newest_id: datetime(2026, 1, 1, 12, 2, tzinfo=UTC),
        }
        with psycopg.connect(postgres_database) as conn:
            for conversation_id, updated_at in timestamps.items():
                conn.execute(
                    "UPDATE conversations SET updated_at = %s WHERE id = %s",
                    (updated_at, conversation_id),
                )
            conn.commit()
        before = {
            conversation_id: _row(postgres_database, conversation_id)[2]
            for conversation_id in timestamps
        }
        first, first_cursor = await store.list_conversations(
            "owner-pages",
            updated_since=cutoff,
            limit=2,
        )
        second, second_cursor = await store.list_conversations(
            "owner-pages",
            updated_since=cutoff,
            limit=2,
            cursor=first_cursor,
        )
        final, final_cursor = await store.list_conversations(
            "owner-pages",
            updated_since=cutoff,
            limit=2,
            cursor=second_cursor,
        )
        after = {
            conversation_id: _row(postgres_database, conversation_id)[2]
            for conversation_id in timestamps
        }
        return (
            older_id,
            equal_id,
            middle_id,
            newest_id,
            first,
            first_cursor,
            second,
            second_cursor,
            final,
            final_cursor,
            before,
            after,
        )

    result = _run(postgres_database, exercise)
    (
        older_id,
        equal_id,
        middle_id,
        newest_id,
        first,
        first_cursor,
        second,
        second_cursor,
        final,
        final_cursor,
        before,
        after,
    ) = result
    assert [row["conversation_id"] for row in first] == [str(newest_id), str(middle_id)]
    assert first_cursor == f"{first[-1]['updated_at']}|{middle_id}"
    assert [row["conversation_id"] for row in second] == [str(equal_id)]
    assert second_cursor == f"{second[-1]['updated_at']}|{equal_id}"
    assert final == []
    assert final_cursor is None
    assert str(older_id) not in {
        row["conversation_id"]
        for row in [*first, *second, *final]
    }
    assert before == after


def test_updated_before_is_strict_applied_before_limit_and_mutation_free(
    postgres_database,
):
    cutoff = datetime(2026, 2, 1, 12, 0, tzinfo=UTC)

    async def exercise(store: PostgresStore):
        older_id = await store.create_conversation("owner-before", "client-one", "older")
        equal_id = await store.create_conversation("owner-before", "client-one", "equal")
        newer_id = await store.create_conversation("owner-before", "client-one", "newer")
        timestamps = {
            older_id: datetime(2026, 2, 1, 11, 59, 59, tzinfo=UTC),
            equal_id: cutoff,
            newer_id: datetime(2026, 2, 1, 12, 0, 1, tzinfo=UTC),
        }
        with psycopg.connect(postgres_database) as conn:
            for conversation_id, updated_at in timestamps.items():
                conn.execute(
                    "UPDATE conversations SET updated_at = %s WHERE id = %s",
                    (updated_at, conversation_id),
                )
            conn.commit()
        before = {
            conversation_id: _row(postgres_database, conversation_id)
            for conversation_id in timestamps
        }
        filtered, _ = await store.list_conversations(
            "owner-before",
            updated_before=cutoff,
            limit=1,
        )
        after = {
            conversation_id: _row(postgres_database, conversation_id)
            for conversation_id in timestamps
        }
        return older_id, equal_id, newer_id, filtered, before, after

    older_id, equal_id, newer_id, filtered, before, after = _run(
        postgres_database,
        exercise,
    )
    assert [row["conversation_id"] for row in filtered] == [str(older_id)]
    assert str(equal_id) not in {row["conversation_id"] for row in filtered}
    assert str(newer_id) not in {row["conversation_id"] for row in filtered}
    assert before == after


def test_updated_before_composes_with_owner_client_lifecycle_and_updated_since(
    postgres_database,
):
    updated_since = datetime(2026, 2, 1, 11, 58, tzinfo=UTC)
    updated_before = datetime(2026, 2, 1, 12, 0, tzinfo=UTC)

    async def exercise(store: PostgresStore):
        selected_id = await store.create_conversation("owner-window", "client-one", "selected")
        too_old_id = await store.create_conversation("owner-window", "client-one", "too old")
        equal_upper_id = await store.create_conversation(
            "owner-window",
            "client-one",
            "equal upper",
        )
        wrong_owner_id = await store.create_conversation("owner-foreign", "client-one", "foreign")
        wrong_client_id = await store.create_conversation(
            "owner-window",
            "client-two",
            "other client",
        )
        closed_id = await store.create_conversation("owner-window", "client-one", "closed")
        await store.transition_conversation_lifecycle(
            conversation_id=closed_id,
            owner_id="owner-window",
            lifecycle_state="closed",
            superseded_by_conversation_id=None,
        )
        timestamps = {
            selected_id: datetime(2026, 2, 1, 11, 59, tzinfo=UTC),
            too_old_id: datetime(2026, 2, 1, 11, 57, 59, tzinfo=UTC),
            equal_upper_id: updated_before,
            wrong_owner_id: datetime(2026, 2, 1, 11, 59, tzinfo=UTC),
            wrong_client_id: datetime(2026, 2, 1, 11, 59, tzinfo=UTC),
            closed_id: datetime(2026, 2, 1, 11, 59, tzinfo=UTC),
        }
        with psycopg.connect(postgres_database) as conn:
            for conversation_id, updated_at in timestamps.items():
                conn.execute(
                    "UPDATE conversations SET updated_at = %s WHERE id = %s",
                    (updated_at, conversation_id),
                )
            conn.commit()
        rows, _ = await store.list_conversations(
            "owner-window",
            client_id="client-one",
            lifecycle_state="open",
            updated_since=updated_since,
            updated_before=updated_before,
            limit=10,
        )
        return selected_id, rows

    selected_id, rows = _run(postgres_database, exercise)
    assert [row["conversation_id"] for row in rows] == [str(selected_id)]


def test_updated_before_preserves_cursor_pagination(postgres_database):
    cutoff = datetime(2026, 2, 1, 12, 0, tzinfo=UTC)

    async def exercise(store: PostgresStore):
        oldest_id = await store.create_conversation("owner-before-pages", "client-one", "oldest")
        middle_id = await store.create_conversation("owner-before-pages", "client-one", "middle")
        newest_id = await store.create_conversation("owner-before-pages", "client-one", "newest")
        equal_id = await store.create_conversation("owner-before-pages", "client-one", "equal")
        timestamps = {
            oldest_id: datetime(2026, 2, 1, 11, 57, tzinfo=UTC),
            middle_id: datetime(2026, 2, 1, 11, 58, tzinfo=UTC),
            newest_id: datetime(2026, 2, 1, 11, 59, tzinfo=UTC),
            equal_id: cutoff,
        }
        with psycopg.connect(postgres_database) as conn:
            for conversation_id, updated_at in timestamps.items():
                conn.execute(
                    "UPDATE conversations SET updated_at = %s WHERE id = %s",
                    (updated_at, conversation_id),
                )
            conn.commit()
        before = {
            conversation_id: _row(postgres_database, conversation_id)
            for conversation_id in timestamps
        }
        first, first_cursor = await store.list_conversations(
            "owner-before-pages",
            updated_before=cutoff,
            limit=2,
        )
        second, second_cursor = await store.list_conversations(
            "owner-before-pages",
            updated_before=cutoff,
            limit=2,
            cursor=first_cursor,
        )
        final, final_cursor = await store.list_conversations(
            "owner-before-pages",
            updated_before=cutoff,
            limit=2,
            cursor=second_cursor,
        )
        after = {
            conversation_id: _row(postgres_database, conversation_id)
            for conversation_id in timestamps
        }
        return (
            oldest_id,
            middle_id,
            newest_id,
            equal_id,
            first,
            first_cursor,
            second,
            second_cursor,
            final,
            final_cursor,
            before,
            after,
        )

    result = _run(postgres_database, exercise)
    (
        oldest_id,
        middle_id,
        newest_id,
        equal_id,
        first,
        first_cursor,
        second,
        second_cursor,
        final,
        final_cursor,
        before,
        after,
    ) = result
    assert [row["conversation_id"] for row in first] == [str(newest_id), str(middle_id)]
    assert first_cursor == f"{first[-1]['updated_at']}|{middle_id}"
    assert [row["conversation_id"] for row in second] == [str(oldest_id)]
    assert second_cursor == f"{second[-1]['updated_at']}|{oldest_id}"
    assert final == []
    assert final_cursor is None
    assert str(equal_id) not in {
        row["conversation_id"] for row in [*first, *second, *final]
    }
    assert before == after


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


def test_lifecycle_expected_activity_matches_instants_and_preserves_idempotent_retry(
    postgres_database,
):
    initial_activity = datetime(2026, 3, 1, 12, 0, 0, 123456, tzinfo=UTC)

    async def exercise(store: PostgresStore):
        exact_id = await store.create_conversation("owner-expected", "client-one", "exact")
        offset_id = await store.create_conversation("owner-expected", "client-two", "offset")
        with psycopg.connect(postgres_database) as conn:
            conn.execute(
                "UPDATE conversations SET updated_at = %s WHERE id = ANY(%s)",
                (initial_activity, [exact_id, offset_id]),
            )
            conn.commit()
        exact_expected = _row(postgres_database, exact_id)[2]
        offset_expected = _row(postgres_database, offset_id)[2]
        equivalent_offset = offset_expected.astimezone(timezone(timedelta(hours=-5)))

        exact_closed = await store.transition_conversation_lifecycle(
            conversation_id=exact_id,
            owner_id="owner-expected",
            lifecycle_state="closed",
            superseded_by_conversation_id=None,
            expected_updated_at=exact_expected,
        )
        exact_after = _row(postgres_database, exact_id)
        offset_closed = await store.transition_conversation_lifecycle(
            conversation_id=offset_id,
            owner_id="owner-expected",
            lifecycle_state="closed",
            superseded_by_conversation_id=None,
            expected_updated_at=equivalent_offset,
        )
        repeated = await store.transition_conversation_lifecycle(
            conversation_id=exact_id,
            owner_id="owner-expected",
            lifecycle_state="closed",
            superseded_by_conversation_id=None,
            expected_updated_at=exact_expected,
        )
        exact_after_retry = _row(postgres_database, exact_id)
        return (
            exact_expected,
            equivalent_offset,
            exact_closed,
            exact_after,
            offset_closed,
            repeated,
            exact_after_retry,
        )

    (
        exact_expected,
        equivalent_offset,
        exact_closed,
        exact_after,
        offset_closed,
        repeated,
        exact_after_retry,
    ) = _run(postgres_database, exercise)
    assert exact_expected == equivalent_offset
    assert exact_closed["lifecycle_state"] == "closed"
    assert offset_closed["lifecycle_state"] == "closed"
    assert exact_after[2] != exact_expected
    assert repeated == exact_closed
    assert exact_after_retry == exact_after


def test_message_append_invalidates_stale_lifecycle_activity_precondition(
    postgres_database,
):
    initial_activity = datetime(2026, 3, 1, 12, 0, 0, 123456, tzinfo=UTC)

    async def exercise(store: PostgresStore):
        source_id = await store.create_conversation("owner-cas", "client-one", "source")
        foreign_replacement_id = await store.create_conversation(
            "owner-foreign",
            "client-two",
            "foreign replacement",
        )
        with psycopg.connect(postgres_database) as conn:
            conn.execute(
                "UPDATE conversations SET updated_at = %s WHERE id = %s",
                (initial_activity, source_id),
            )
            conn.commit()
        expected_updated_at = _row(postgres_database, source_id)[2]
        foreign_before = _row(postgres_database, foreign_replacement_id)
        message_id = await store.add_message(
            conversation_id=source_id,
            owner_id="owner-cas",
            role="user",
            content="New durable activity.",
            client_id="client-one",
        )
        after_append = _row(postgres_database, source_id)
        with pytest.raises(ConversationLifecycleConflictError):
            await store.transition_conversation_lifecycle(
                conversation_id=source_id,
                owner_id="owner-cas",
                lifecycle_state="superseded",
                superseded_by_conversation_id=foreign_replacement_id,
                expected_updated_at=expected_updated_at,
            )
        after_conflict = _row(postgres_database, source_id)
        foreign_after = _row(postgres_database, foreign_replacement_id)
        return (
            source_id,
            message_id,
            expected_updated_at,
            after_append,
            after_conflict,
            foreign_before,
            foreign_after,
        )

    (
        source_id,
        message_id,
        expected_updated_at,
        after_append,
        after_conflict,
        foreign_before,
        foreign_after,
    ) = _run(postgres_database, exercise)
    assert isinstance(message_id, UUID)
    assert after_append[0:2] == ("open", None)
    assert after_append[2] > expected_updated_at
    assert after_conflict == after_append
    assert foreign_after == foreign_before
    assert _message_count(postgres_database, source_id) == 1


def test_matching_lifecycle_transition_prevents_later_message_append(
    postgres_database,
):
    initial_activity = datetime(2026, 3, 1, 12, 0, 0, 123456, tzinfo=UTC)

    async def exercise(store: PostgresStore):
        conversation_id = await store.create_conversation(
            "owner-transition-first",
            "client-one",
            "transition first",
        )
        with psycopg.connect(postgres_database) as conn:
            conn.execute(
                "UPDATE conversations SET updated_at = %s WHERE id = %s",
                (initial_activity, conversation_id),
            )
            conn.commit()
        expected_updated_at = _row(postgres_database, conversation_id)[2]
        closed = await store.transition_conversation_lifecycle(
            conversation_id=conversation_id,
            owner_id="owner-transition-first",
            lifecycle_state="closed",
            superseded_by_conversation_id=None,
            expected_updated_at=expected_updated_at,
        )
        after_transition = _row(postgres_database, conversation_id)
        with pytest.raises(ConversationNotOpenError):
            await store.add_message(
                conversation_id=conversation_id,
                owner_id="owner-transition-first",
                role="user",
                content="Rejected after close.",
                client_id="client-one",
            )
        after_append_rejection = _row(postgres_database, conversation_id)
        return conversation_id, closed, after_transition, after_append_rejection

    conversation_id, closed, after_transition, after_append_rejection = _run(
        postgres_database,
        exercise,
    )
    assert closed["lifecycle_state"] == "closed"
    assert after_transition[0:2] == ("closed", None)
    assert after_append_rejection == after_transition
    assert _message_count(postgres_database, conversation_id) == 0


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


def test_supplied_message_identity_exact_retry_is_durable_and_activity_stable(
    postgres_database,
):
    message_id = uuid4()
    metadata = {"surface": "web", "nested": {"alpha": 1, "beta": 2}}
    reordered_metadata = {"nested": {"beta": 2, "alpha": 1}, "surface": "web"}
    policy = {
        "memory_domains": ["technical"],
        "sensitivity": "low",
        "entity_ids": [],
        "relationship_ids": [],
        "relationship_scopes": [],
    }

    async def exercise(store: PostgresStore):
        conversation_id = await store.create_conversation(
            "owner-identity",
            "client-origin",
            "durable identity",
        )
        with psycopg.connect(postgres_database) as conn:
            conn.execute(
                "UPDATE conversations SET updated_at = '2026-01-01T00:00:00Z' WHERE id = %s",
                (conversation_id,),
            )
            conn.commit()
        before = _row(postgres_database, conversation_id)[2]
        first = await store.add_message(
            conversation_id=conversation_id,
            owner_id="owner-identity",
            role="user",
            content="Exact durable append.",
            client_id=None,
            metadata=metadata,
            policy_metadata=policy,
            message_id=message_id,
        )
        after_first = _row(postgres_database, conversation_id)[2]
        retry = await store.add_message(
            conversation_id=conversation_id,
            owner_id="owner-identity",
            role="user",
            content="Exact durable append.",
            client_id=None,
            metadata=reordered_metadata,
            policy_metadata=dict(reversed(list(policy.items()))),
            message_id=message_id,
        )
        after_retry = _row(postgres_database, conversation_id)[2]
        legacy_first = await store.add_message(
            conversation_id=conversation_id,
            owner_id="owner-identity",
            role="user",
            content="Repeated legacy append.",
        )
        legacy_second = await store.add_message(
            conversation_id=conversation_id,
            owner_id="owner-identity",
            role="user",
            content="Repeated legacy append.",
        )
        return (
            conversation_id,
            first,
            retry,
            legacy_first,
            legacy_second,
            before,
            after_first,
            after_retry,
        )

    result = _run(postgres_database, exercise)
    (
        conversation_id,
        first,
        retry,
        legacy_first,
        legacy_second,
        before,
        after_first,
        after_retry,
    ) = result
    assert first == retry == message_id
    assert legacy_first != legacy_second
    assert before < after_first == after_retry
    assert _message_count(postgres_database, conversation_id) == 3
    stored = _message_row(postgres_database, message_id)
    assert stored[:7] == (
        conversation_id,
        "owner-identity",
        None,
        "user",
        "Exact durable append.",
        metadata,
        policy,
    )

    async def retry_after_reopen(store: PostgresStore):
        return await store.add_message(
            conversation_id=conversation_id,
            owner_id="owner-identity",
            role="user",
            content="Exact durable append.",
            client_id=None,
            metadata=metadata,
            policy_metadata=policy,
            message_id=message_id,
        )

    before_reopen_retry = _row(postgres_database, conversation_id)[2]
    reopened_retry = _run(postgres_database, retry_after_reopen)
    assert reopened_retry == message_id
    assert _message_count(postgres_database, conversation_id) == 3
    assert _row(postgres_database, conversation_id)[2] == before_reopen_retry


def test_empty_metadata_retry_matches_existing_null_storage(postgres_database):
    message_id = uuid4()

    async def exercise(store: PostgresStore):
        conversation_id = await store.create_conversation("owner-empty", "client-one")
        first = await store.add_message(
            conversation_id=conversation_id,
            owner_id="owner-empty",
            role="user",
            content="Empty metadata normalization.",
            metadata=None,
            message_id=message_id,
        )
        after_first = _row(postgres_database, conversation_id)[2]
        retry = await store.add_message(
            conversation_id=conversation_id,
            owner_id="owner-empty",
            role="user",
            content="Empty metadata normalization.",
            metadata={},
            message_id=message_id,
        )
        return conversation_id, first, retry, after_first

    conversation_id, first, retry, after_first = _run(postgres_database, exercise)
    assert first == retry == message_id
    assert _message_count(postgres_database, conversation_id) == 1
    assert _message_row(postgres_database, message_id)[5] is None
    assert _row(postgres_database, conversation_id)[2] == after_first


@pytest.mark.parametrize(
    "mutation",
    ["conversation", "client", "role", "content", "metadata", "policy_metadata"],
)
def test_supplied_message_identity_mismatch_conflicts_without_mutation(
    postgres_database,
    mutation,
):
    message_id = uuid4()
    metadata = {"surface": "voice"}
    policy = {"memory_domains": ["personal"], "sensitivity": "low"}

    async def exercise(store: PostgresStore):
        conversation_id = await store.create_conversation("owner-conflict", "client-one")
        other_conversation_id = await store.create_conversation("owner-conflict", "client-two")
        await store.add_message(
            conversation_id=conversation_id,
            owner_id="owner-conflict",
            role="user",
            content="Original private content.",
            client_id="client-one",
            metadata=metadata,
            policy_metadata=policy,
            message_id=message_id,
        )
        original_row = _message_row(postgres_database, message_id)
        target_id = other_conversation_id if mutation == "conversation" else conversation_id
        target_before = _row(postgres_database, target_id)[2]
        append = {
            "conversation_id": target_id,
            "owner_id": "owner-conflict",
            "role": "assistant" if mutation == "role" else "user",
            "content": "Changed private content." if mutation == "content" else "Original private content.",
            "client_id": "client-two" if mutation == "client" else "client-one",
            "metadata": {"surface": "car"} if mutation == "metadata" else metadata,
            "policy_metadata": (
                {"memory_domains": ["technical"], "sensitivity": "high"}
                if mutation == "policy_metadata"
                else policy
            ),
            "message_id": message_id,
        }
        with pytest.raises(MessageAppendConflictError) as exc:
            await store.add_message(**append)
        return conversation_id, target_id, target_before, original_row, str(exc.value)

    conversation_id, target_id, target_before, original_row, detail = _run(
        postgres_database,
        exercise,
    )
    assert detail == "message_append_conflict"
    assert _message_row(postgres_database, message_id) == original_row
    assert _message_count(postgres_database, conversation_id) == 1
    assert _message_count(postgres_database, target_id) == (1 if target_id == conversation_id else 0)
    assert _row(postgres_database, target_id)[2] == target_before


def test_supplied_message_identity_owner_boundaries_precede_collision_inspection(
    postgres_database,
):
    message_id = uuid4()

    async def exercise(store: PostgresStore):
        owner_conversation = await store.create_conversation("owner-boundary", "client-one")
        foreign_conversation = await store.create_conversation("owner-foreign", "client-one")
        await store.add_message(
            conversation_id=foreign_conversation,
            owner_id="owner-foreign",
            role="user",
            content="PRIVATE FOREIGN CONTENT",
            message_id=message_id,
        )
        with pytest.raises(ConversationNotFoundError):
            await store.add_message(
                conversation_id=owner_conversation,
                owner_id="wrong-owner",
                role="user",
                content="PRIVATE FOREIGN CONTENT",
                message_id=message_id,
            )
        with pytest.raises(ConversationNotFoundError):
            await store.add_message(
                conversation_id=uuid4(),
                owner_id="owner-boundary",
                role="user",
                content="PRIVATE FOREIGN CONTENT",
                message_id=message_id,
            )
        with pytest.raises(MessageAppendConflictError) as exc:
            await store.add_message(
                conversation_id=owner_conversation,
                owner_id="owner-boundary",
                role="user",
                content="PRIVATE FOREIGN CONTENT",
                message_id=message_id,
            )
        return owner_conversation, foreign_conversation, str(exc.value)

    owner_conversation, foreign_conversation, detail = _run(postgres_database, exercise)
    assert detail == "message_append_conflict"
    assert _message_count(postgres_database, owner_conversation) == 0
    assert _message_count(postgres_database, foreign_conversation) == 1


@pytest.mark.parametrize("terminal_state", ["closed", "superseded"])
def test_exact_retry_survives_terminal_lifecycle_but_new_append_does_not(
    postgres_database,
    terminal_state,
):
    message_id = uuid4()

    async def exercise(store: PostgresStore):
        conversation_id = await store.create_conversation("owner-terminal", "client-one")
        replacement_id = await store.create_conversation("owner-terminal", "client-two")
        await store.add_message(
            conversation_id=conversation_id,
            owner_id="owner-terminal",
            role="user",
            content="Persisted before terminal lifecycle.",
            client_id="client-one",
            message_id=message_id,
        )
        await store.transition_conversation_lifecycle(
            conversation_id=conversation_id,
            owner_id="owner-terminal",
            lifecycle_state=terminal_state,
            superseded_by_conversation_id=(
                replacement_id if terminal_state == "superseded" else None
            ),
        )
        terminal_activity = _row(postgres_database, conversation_id)[2]
        retry = await store.add_message(
            conversation_id=conversation_id,
            owner_id="owner-terminal",
            role="user",
            content="Persisted before terminal lifecycle.",
            client_id="client-one",
            message_id=message_id,
        )
        with pytest.raises(MessageAppendConflictError):
            await store.add_message(
                conversation_id=conversation_id,
                owner_id="owner-terminal",
                role="user",
                content="Changed after terminal lifecycle.",
                client_id="client-one",
                message_id=message_id,
            )
        with pytest.raises(ConversationNotOpenError):
            await store.add_message(
                conversation_id=conversation_id,
                owner_id="owner-terminal",
                role="user",
                content="New supplied append after terminal lifecycle.",
                message_id=uuid4(),
            )
        with pytest.raises(ConversationNotOpenError):
            await store.add_message(
                conversation_id=conversation_id,
                owner_id="owner-terminal",
                role="user",
                content="Legacy append after terminal lifecycle.",
            )
        return conversation_id, retry, terminal_activity

    conversation_id, retry, terminal_activity = _run(postgres_database, exercise)
    assert retry == message_id
    assert _message_count(postgres_database, conversation_id) == 1
    assert _row(postgres_database, conversation_id)[2] == terminal_activity


def test_concurrent_exact_appends_converge_on_one_message(postgres_database):
    message_id = uuid4()

    async def exercise():
        setup = PostgresStore(postgres_database)
        await setup.open()
        try:
            conversation_id = await setup.create_conversation("owner-concurrent", "client-one")
        finally:
            await setup.close()
        with psycopg.connect(postgres_database) as conn:
            conn.execute(
                "UPDATE conversations SET updated_at = '2026-01-01T00:00:00Z' WHERE id = %s",
                (conversation_id,),
            )
            conn.commit()
        before = _row(postgres_database, conversation_id)[2]
        first_store = PostgresStore(postgres_database)
        second_store = PostgresStore(postgres_database)
        await first_store.open()
        await second_store.open()
        gate = asyncio.Event()

        async def append(store: PostgresStore):
            await gate.wait()
            return await store.add_message(
                conversation_id=conversation_id,
                owner_id="owner-concurrent",
                role="user",
                content="Concurrent exact append.",
                client_id="client-one",
                message_id=message_id,
            )

        try:
            tasks = [asyncio.create_task(append(first_store)), asyncio.create_task(append(second_store))]
            await asyncio.sleep(0)
            gate.set()
            results = await asyncio.gather(*tasks)
            after_creation = _row(postgres_database, conversation_id)[2]
            retry = await first_store.add_message(
                conversation_id=conversation_id,
                owner_id="owner-concurrent",
                role="user",
                content="Concurrent exact append.",
                client_id="client-one",
                message_id=message_id,
            )
            after_retry = _row(postgres_database, conversation_id)[2]
        finally:
            await first_store.close()
            await second_store.close()
        return conversation_id, results, retry, before, after_creation, after_retry

    conversation_id, results, retry, before, after_creation, after_retry = asyncio.run(
        exercise()
    )
    assert results == [message_id, message_id]
    assert retry == message_id
    assert _message_count(postgres_database, conversation_id) == 1
    assert before < after_creation == after_retry


def test_concurrent_mismatched_appends_have_one_winner(postgres_database):
    message_id = uuid4()

    async def exercise():
        setup = PostgresStore(postgres_database)
        await setup.open()
        try:
            conversation_id = await setup.create_conversation("owner-race", "client-one")
        finally:
            await setup.close()
        first_store = PostgresStore(postgres_database)
        second_store = PostgresStore(postgres_database)
        await first_store.open()
        await second_store.open()
        gate = asyncio.Event()

        async def append(store: PostgresStore, content: str):
            await gate.wait()
            return await store.add_message(
                conversation_id=conversation_id,
                owner_id="owner-race",
                role="user",
                content=content,
                message_id=message_id,
            )

        try:
            tasks = [
                asyncio.create_task(append(first_store, "First concurrent payload.")),
                asyncio.create_task(append(second_store, "Second concurrent payload.")),
            ]
            await asyncio.sleep(0)
            gate.set()
            results = await asyncio.gather(*tasks, return_exceptions=True)
        finally:
            await first_store.close()
            await second_store.close()
        return conversation_id, results

    conversation_id, results = asyncio.run(exercise())
    assert sum(result == message_id for result in results) == 1
    assert sum(isinstance(result, MessageAppendConflictError) for result in results) == 1
    assert _message_count(postgres_database, conversation_id) == 1
    assert _message_row(postgres_database, message_id)[4] in {
        "First concurrent payload.",
        "Second concurrent payload.",
    }


def test_supplied_message_insert_failure_rolls_back_activity(postgres_database):
    message_id = uuid4()

    async def exercise(store: PostgresStore):
        conversation_id = await store.create_conversation("owner-rollback", "client-one")
        with psycopg.connect(postgres_database) as conn:
            conn.execute(
                "UPDATE conversations SET updated_at = '2026-01-01T00:00:00Z' WHERE id = %s",
                (conversation_id,),
            )
            conn.commit()
        before = _row(postgres_database, conversation_id)[2]
        with pytest.raises(psycopg.errors.CheckViolation):
            await store.add_message(
                conversation_id=conversation_id,
                owner_id="owner-rollback",
                role="invalid",
                content="This insert must roll back.",
                message_id=message_id,
            )
        return conversation_id, before

    conversation_id, before = _run(postgres_database, exercise)
    assert _message_row(postgres_database, message_id) is None
    assert _message_count(postgres_database, conversation_id) == 0
    assert _row(postgres_database, conversation_id)[2] == before
