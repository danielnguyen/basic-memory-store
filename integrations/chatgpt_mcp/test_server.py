"""Boundary tests for the read-only BMS tool. No live BMS or tunnel required."""

import asyncio

import httpx
import pytest

from server import get_memory, search_memories, list_conversations, get_conversation_messages, mcp

MEMORY_ID = "550e8400-e29b-41d4-a716-446655440000"


def _mock_client(monkeypatch, handler):
    actual = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: actual(transport=httpx.MockTransport(handler), **kwargs))


def _configure(monkeypatch):
    monkeypatch.setenv("BMS_OWNER_ID", "daniel")
    monkeypatch.setenv("MEMORY_API_KEY", "TEST_SECRET_DO_NOT_USE")
    monkeypatch.setenv("BMS_API_URL", "http://basic-memory-store:8000")


def test_success_is_exactly_one_owner_bound_get(monkeypatch):
    _configure(monkeypatch)
    seen = []

    def respond(req):
        seen.append(req)
        return httpx.Response(200, json={"memory": {
            "memory_id": MEMORY_ID, "owner_id": "daniel", "summary": "Synthetic lighthouse memory",
            "memory_type": "core", "status": "active", "promotion_state": "promoted",
            "source_refs": [{"ref_type": "github_issue", "ref_id": "danielnguyen/test-repo#2"}],
            "explanation": {"private_debug_data": "not exposed"},
        }})

    _mock_client(monkeypatch, respond)
    result = asyncio.run(get_memory(MEMORY_ID))
    assert result["found"] is True
    assert result["summary"] == "Synthetic lighthouse memory"
    assert result["source_refs"][0]["ref_id"].endswith("#2")
    assert "explanation" not in result
    assert len(seen) == 1
    assert seen[0].method == "GET"
    assert seen[0].url.path == f"/v1/internal/memory/{MEMORY_ID}/debug"
    assert seen[0].url.params["owner_id"] == "daniel"
    assert seen[0].headers["X-API-Key"] == "TEST_SECRET_DO_NOT_USE"


def test_invalid_id_sends_no_request(monkeypatch):
    _configure(monkeypatch)
    _mock_client(monkeypatch, lambda req: (_ for _ in ()).throw(AssertionError("request sent")))
    assert asyncio.run(get_memory("../../v1/internal/memory/promote")) == {"found": False, "reason": "invalid_id"}


def test_wrong_owner_record_does_not_disclose(monkeypatch):
    _configure(monkeypatch)
    _mock_client(monkeypatch, lambda req: httpx.Response(200, json={"memory": {
        "memory_id": MEMORY_ID, "owner_id": "other", "summary": "Other person's secret",
    }}))
    result = asyncio.run(get_memory(MEMORY_ID))
    assert result == {"found": False, "reason": "not_found"}
    assert "secret" not in str(result)


def test_missing_memory_returns_not_found(monkeypatch):
    _configure(monkeypatch)
    _mock_client(monkeypatch, lambda req: httpx.Response(404))
    assert asyncio.run(get_memory(MEMORY_ID)) == {"found": False, "reason": "not_found"}


def test_service_errors_and_missing_key_do_not_leak_details(monkeypatch):
    _configure(monkeypatch)
    _mock_client(monkeypatch, lambda req: httpx.Response(503, json={"error": "private server context"}))
    assert asyncio.run(get_memory(MEMORY_ID)) == {"found": False, "reason": "bms_unavailable"}
    monkeypatch.delenv("MEMORY_API_KEY")
    assert asyncio.run(get_memory(MEMORY_ID)) == {"found": False, "reason": "not_configured"}


def test_only_named_reads_are_registered_with_bounded_schema():
    tools = {tool.name: tool for tool in asyncio.run(mcp.list_tools())}
    assert set(tools) == {"get_memory", "search_memories", "list_conversations", "get_conversation_messages"}
    for tool in tools.values():
        assert tool.annotations.read_only_hint is True
        assert tool.annotations.open_world_hint is False
    schema = tools["search_memories"].input_schema
    assert set(schema["properties"]) == {"query", "limit"}
    assert schema["required"] == ["query"]
    assert schema["properties"]["query"]["maxLength"] == 500
    assert schema["properties"]["limit"]["maximum"] == 10


def _search_memory(**changes):
    return {
        "memory_id": MEMORY_ID, "owner_id": "daniel", "summary": "Synthetic silver lighthouse",
        "memory_type": "core", "status": "active", "promotion_state": "promoted",
        "freshness_state": "active", "confidence": 0.9, "expires_at": None,
        "updated_at": "2026-01-01T00:00:00Z",
        "source_refs": [{"ref_type": "message", "ref_id": "m-1", "metadata": {"token": "SECRET"}}],
        "explanation": {"token": "SECRET"}, **changes,
    }


def test_search_is_one_fixed_owner_bound_get(monkeypatch):
    _configure(monkeypatch)
    seen = []

    def respond(req):
        seen.append(req)
        return httpx.Response(200, json={"memories": [_search_memory()], "events": ["SECRET"]})

    _mock_client(monkeypatch, respond)
    query = "What did I save about the silver lighthouse?"
    result = asyncio.run(search_memories(query, 3))
    assert result["ok"] is True
    assert result["memories"][0]["summary"] == "Synthetic silver lighthouse"
    assert result["memories"][0]["source_refs"] == [{"ref_type": "message", "ref_id": "m-1", "support_kind": "direct"}]
    assert "SECRET" not in str(result)
    assert "owner_id" not in result["memories"][0]
    assert len(seen) == 1
    req = seen[0]
    assert req.method == "GET"
    assert req.url.path == "/v1/internal/memory/search"
    assert dict(req.url.params) == {"owner_id": "daniel", "query": query, "limit": "3"}
    assert req.headers["X-API-Key"] == "TEST_SECRET_DO_NOT_USE"


@pytest.mark.parametrize("query,limit", [("", 5), ("   ", 5), ("x" * 501, 5), ("silver", 0),
                                          ("silver", 11), ("silver", True), ("silver", 1.5)])
def test_search_invalid_inputs_do_not_send_http(monkeypatch, query, limit):
    _configure(monkeypatch)
    _mock_client(monkeypatch, lambda req: (_ for _ in ()).throw(AssertionError("request sent")))
    assert asyncio.run(search_memories(query, limit)) == {"ok": False, "reason": "invalid_query"}


def test_search_empty_is_success_and_configuration_failure_is_distinct(monkeypatch):
    _configure(monkeypatch)
    _mock_client(monkeypatch, lambda req: httpx.Response(200, json={"memories": []}))
    assert asyncio.run(search_memories("unknown")) == {"ok": True, "memories": []}
    monkeypatch.delenv("BMS_OWNER_ID")
    assert asyncio.run(search_memories("unknown")) == {"ok": False, "reason": "not_configured"}


@pytest.mark.parametrize("changes", [
    {"owner_id": "other"}, {"memory_id": "invalid"}, {"summary": "x" * 4001},
    {"status": "stale"}, {"status": "superseded"}, {"promotion_state": "suppressed"},
    {"freshness_state": "unknown_freshness"}, {"expires_at": "2020-01-01T00:00:00Z"},
    {"expires_at": "2035-01-01T00:00:00"}, {"expires_at": "invalid"},
    {"updated_at": None}, {"source_refs": [{"ref_type": "message"}]},
    {"source_refs": [{}] * 51}, {"confidence": 2},
])
def test_search_rejects_entire_response_on_wrong_owner_or_malformed_record(monkeypatch, changes):
    _configure(monkeypatch)
    _mock_client(monkeypatch, lambda req: httpx.Response(200, json={"memories": [_search_memory(), _search_memory(**changes)]}))
    assert asyncio.run(search_memories("silver")) == {"ok": False, "reason": "bms_unavailable"}


@pytest.mark.parametrize("payload", [None, [], {}, {"memories": None}, {"memories": [_search_memory()] * 6}])
def test_search_rejects_malformed_or_oversized_envelope(monkeypatch, payload):
    _configure(monkeypatch)
    _mock_client(monkeypatch, lambda req: httpx.Response(200, json=payload))
    assert asyncio.run(search_memories("silver")) == {"ok": False, "reason": "bms_unavailable"}


@pytest.mark.parametrize("status", [401, 403, 404, 302, 500, 503, 422])
def test_search_http_failure_does_not_leak_body_or_credentials(monkeypatch, status):
    _configure(monkeypatch)
    _mock_client(monkeypatch, lambda req: httpx.Response(status, text="TEST_SECRET_DO_NOT_USE private details"))
    assert asyncio.run(search_memories("silver")) == {
        "ok": False, "reason": "invalid_query" if status == 422 else "bms_unavailable",
    }


def test_search_timeout_and_invalid_json_are_bounded_failures(monkeypatch):
    _configure(monkeypatch)

    def timeout(req):
        raise httpx.ReadTimeout("TEST_SECRET_DO_NOT_USE", request=req)

    _mock_client(monkeypatch, timeout)
    assert asyncio.run(search_memories("silver")) == {"ok": False, "reason": "bms_unavailable"}
    # Replace the transport without stacking the patched client factory.
    monkeypatch.undo()
    _configure(monkeypatch)
    _mock_client(monkeypatch, lambda req: httpx.Response(200, text="not JSON"))
    assert asyncio.run(search_memories("silver")) == {"ok": False, "reason": "bms_unavailable"}


def test_sdk_dispatches_search_and_rejects_write_tool(monkeypatch):
    _configure(monkeypatch)
    _mock_client(monkeypatch, lambda req: httpx.Response(200, json={"memories": []}))
    result = asyncio.run(mcp.call_tool("search_memories", {"query": "lighthouse"}))
    assert result.is_error is False
    assert result.structured_content == {"ok": True, "memories": []}
    with pytest.raises(Exception, match="Unknown tool"):
        asyncio.run(mcp.call_tool("promote_memory", {"summary": "unapproved"}))


CID = "550e8400-e29b-41d4-a716-446655440001"
WHEN = "2026-01-01T00:00:00Z"


def _candidate(**changes):
    return {"conversation_id": CID, "owner_id": "daniel", "client_id": "actual-gateway",
            "title": "Synthetic dialogue", "lifecycle_state": "closed",
            "created_at": WHEN, "updated_at": WHEN, **changes}


def _transcript(**changes):
    return {"conversation_id": CID, "owner_id": "daniel", "has_more": False,
            "next_cursor": None, "messages": [{"message_id": MEMORY_ID,
            "conversation_id": CID, "owner_id": "daniel", "role": "assistant",
            "content": "Saved α text.\n  Exact spacing", "created_at": WHEN,
            "speaker_profile_ref": "daniel:vega:1", "metadata": {"key": "SECRET"}}], **changes}


def test_conversation_sdk_inventory_and_dispatch(monkeypatch):
    _configure(monkeypatch)
    tools = {t.name: t for t in asyncio.run(mcp.list_tools())}
    for name in ("list_conversations", "get_conversation_messages"):
        props = tools[name].input_schema["properties"]
        assert not {"owner_id", "url", "method", "api_key"} & set(props)
        assert props["limit"]["maximum"] == 50
        assert "explicit" in tools[name].description
    _mock_client(monkeypatch, lambda req: httpx.Response(200, json=_transcript()))
    result = asyncio.run(mcp.call_tool("get_conversation_messages", {"conversation_id": CID}))
    assert not result.is_error
    assert result.structured_content["messages"][0]["content"] == "Saved α text.\n  Exact spacing"
    assert "SECRET" not in str(result)


def test_candidate_discovery_and_filters(monkeypatch):
    _configure(monkeypatch)
    seen = []
    next_cursor = f"{WHEN}|{CID}"
    def respond(req):
        seen.append(req)
        return httpx.Response(200, json={"conversations": [_candidate()], "next_cursor": next_cursor})
    _mock_client(monkeypatch, respond)
    result = asyncio.run(list_conversations())
    assert result["ok"] and result["conversations"][0]["client_id"] == "actual-gateway"
    assert dict(seen[0].url.params) == {"owner_id": "daniel", "limit": "20"}
    result = asyncio.run(list_conversations(client_id="actual-gateway", updated_since="2025-12-31T19:00:00-05:00",
                                            updated_before="2026-01-02T00:00:00Z", limit=1))
    assert result["ok"] and result["next_cursor"] == next_cursor
    assert all(r.method == "GET" and r.url.path == "/v1/conversations" for r in seen)
    assert seen[-1].headers["X-API-Key"] == "TEST_SECRET_DO_NOT_USE"
    # A repeated/non-advancing page is invalid, never silently loop.
    assert asyncio.run(list_conversations(cursor=next_cursor))["reason"] == "bms_unavailable"


@pytest.mark.parametrize("kwargs", [
    {"limit": 0}, {"limit": 51}, {"limit": True}, {"limit": 1.5},
    {"cursor": "forged"}, {"cursor": "x" * 161}, {"client_id": " "},
    {"client_id": "x" * 201}, {"updated_since": "2026-01-01"},
    {"updated_since": "bad"}, {"updated_since": WHEN, "updated_before": WHEN},
])
def test_candidate_invalid_input_is_local(monkeypatch, kwargs):
    _configure(monkeypatch)
    _mock_client(monkeypatch, lambda req: pytest.fail("HTTP attempted"))
    assert asyncio.run(list_conversations(**kwargs)) == {"ok": False, "reason": "invalid_query"}


@pytest.mark.parametrize("kwargs", [
    {"conversation_id": "../admin"}, {"limit": 51}, {"limit": True},
    {"cursor": f"{MEMORY_ID}|{WHEN}|{MEMORY_ID}"},
    {"cursor": f"{CID}|2026-01-01|{MEMORY_ID}"},
    {"created_since": "2026-01-01"}, {"created_since": WHEN, "created_before": WHEN},
])
def test_transcript_invalid_input_is_local(monkeypatch, kwargs):
    _configure(monkeypatch)
    _mock_client(monkeypatch, lambda req: pytest.fail("HTTP attempted"))
    assert asyncio.run(get_conversation_messages(**{"conversation_id": CID, **kwargs})) == {"ok": False, "reason": "invalid_query"}


@pytest.mark.parametrize("changes", [{"owner_id": "other"}, {"conversation_id": "bad"},
    {"updated_at": "2026-01-01"}, {"created_at": "bad"}, {"title": "x" * 2001}])
def test_candidates_reject_mixed_malformed_response(monkeypatch, changes):
    _configure(monkeypatch)
    _mock_client(monkeypatch, lambda req: httpx.Response(200, json={
        "conversations": [_candidate(), _candidate(**changes)], "next_cursor": None}))
    assert asyncio.run(list_conversations()) == {"ok": False, "reason": "bms_unavailable"}


@pytest.mark.parametrize("changes", [{"owner_id": "other"}, {"conversation_id": MEMORY_ID},
    {"role": "system"}, {"role": "tool"}, {"created_at": "2026-01-01"},
    {"content": "x" * 65537}, {"speaker_profile_ref": "forged reference"},
    {"role": "user", "speaker_profile_ref": "daniel:vega:1"}])
def test_transcript_rejects_malformed_or_foreign_message(monkeypatch, changes):
    _configure(monkeypatch)
    value = _transcript()
    value["messages"][0].update(changes)
    _mock_client(monkeypatch, lambda req: httpx.Response(200, json=value))
    assert asyncio.run(get_conversation_messages(CID)) == {"ok": False, "reason": "bms_unavailable"}


@pytest.mark.parametrize("changes", [{"owner_id": "other"}, {"conversation_id": MEMORY_ID},
    {"has_more": True}, {"has_more": False, "next_cursor": f"{CID}|{WHEN}|{MEMORY_ID}"},
    {"has_more": True, "next_cursor": f"{MEMORY_ID}|{WHEN}|{MEMORY_ID}"},
    {"messages": _transcript()["messages"] * 2}])
def test_transcript_rejects_envelope_and_ordering(monkeypatch, changes):
    _configure(monkeypatch)
    _mock_client(monkeypatch, lambda req: httpx.Response(200, json=_transcript(**changes)))
    assert asyncio.run(get_conversation_messages(CID)) == {"ok": False, "reason": "bms_unavailable"}


def test_transcript_pagination_and_empty(monkeypatch):
    _configure(monkeypatch)
    seen = []
    next_cursor = f"{CID}|{WHEN}|{MEMORY_ID}"
    def respond(req):
        seen.append(req)
        if "cursor" in req.url.params:
            return httpx.Response(200, json=_transcript(messages=[]))
        return httpx.Response(200, json=_transcript(has_more=True, next_cursor=next_cursor))
    _mock_client(monkeypatch, respond)
    result = asyncio.run(get_conversation_messages(CID, limit=1))
    assert result["has_more"] and result["next_cursor"] == next_cursor
    result = asyncio.run(get_conversation_messages(CID, cursor=next_cursor))
    assert result["ok"] and result["messages"] == [] and not result["has_more"]
    assert all(r.method == "GET" and r.url.path == f"/v1/conversations/{CID}/messages" for r in seen)
    assert all(r.url.params["owner_id"] == "daniel" for r in seen)


@pytest.mark.parametrize("tool,args", [(list_conversations, ()), (get_conversation_messages, (CID,))])
@pytest.mark.parametrize("failure", [401, 404, 413, 302, 500, 503, "json", "timeout", "oversized"])
def test_conversation_failures_are_sanitized(monkeypatch, tool, args, failure):
    _configure(monkeypatch)
    seen = []
    def respond(req):
        seen.append(req)
        if failure == "timeout":
            raise httpx.ReadTimeout("SECRET", request=req)
        if failure == "oversized":
            return httpx.Response(200, content=b" " * 262145)
        return httpx.Response(failure if isinstance(failure, int) else 200,
                              text="SECRET", headers={"Location": "http://unrelated.invalid"})
    _mock_client(monkeypatch, respond)
    expected = ({404: "not_found", 413: "transcript_message_too_large"}.get(failure, "bms_unavailable")
                if tool is get_conversation_messages else "bms_unavailable")
    assert asyncio.run(tool(*args)) == {"ok": False, "reason": expected}
    assert len(seen) == 1


def test_candidate_empty_and_unconfigured(monkeypatch):
    _configure(monkeypatch)
    _mock_client(monkeypatch, lambda req: httpx.Response(200, json={"conversations": [], "next_cursor": None}))
    assert asyncio.run(list_conversations()) == {"ok": True, "conversations": [], "next_cursor": None}
    monkeypatch.delenv("BMS_OWNER_ID")
    assert asyncio.run(list_conversations())["reason"] == "not_configured"
    assert asyncio.run(get_conversation_messages(CID))["reason"] == "not_configured"


@pytest.mark.parametrize("payload", [None, [], {}, {"conversations": None},
    {"conversations": [_candidate()] * 51}, {"conversations": [], "next_cursor": f"{WHEN}|{CID}"}])
def test_candidates_reject_bad_envelopes(monkeypatch, payload):
    _configure(monkeypatch)
    _mock_client(monkeypatch, lambda req: httpx.Response(200, json=payload))
    assert asyncio.run(list_conversations()) == {"ok": False, "reason": "bms_unavailable"}


def test_transcript_rejects_over_limit_and_filters(monkeypatch):
    _configure(monkeypatch)
    _mock_client(monkeypatch, lambda req: httpx.Response(200, json=_transcript()))
    assert asyncio.run(get_conversation_messages(CID, created_before=WHEN))["reason"] == "bms_unavailable"
    assert asyncio.run(get_conversation_messages(CID, created_since="2026-01-02T00:00:00Z"))["reason"] == "bms_unavailable"
    assert asyncio.run(get_conversation_messages(CID, cursor=f"{CID}|{WHEN}|{MEMORY_ID}"))["reason"] == "bms_unavailable"


def test_candidates_reject_wrong_client_and_time(monkeypatch):
    _configure(monkeypatch)
    _mock_client(monkeypatch, lambda req: httpx.Response(200, json={
        "conversations": [_candidate()], "next_cursor": f"{WHEN}|{CID}"}))
    assert asyncio.run(list_conversations(client_id="other"))["reason"] == "bms_unavailable"
    assert asyncio.run(list_conversations(updated_before=WHEN))["reason"] == "bms_unavailable"
    assert asyncio.run(list_conversations(updated_since="2026-01-02T00:00:00Z"))["reason"] == "bms_unavailable"


def test_sdk_caller_owner_cannot_change_fixed_owner(monkeypatch):
    _configure(monkeypatch)
    seen = []
    def respond(req):
        seen.append(req)
        return httpx.Response(200, json=_transcript())
    _mock_client(monkeypatch, respond)
    result = asyncio.run(mcp.call_tool("get_conversation_messages", {"conversation_id": CID, "owner_id": "other"}))
    # SDK may reject unknown arguments or ignore them; neither grants scope.
    assert all(req.url.params["owner_id"] == "daniel" for req in seen)
    if not result.is_error:
        assert result.structured_content["ok"]
