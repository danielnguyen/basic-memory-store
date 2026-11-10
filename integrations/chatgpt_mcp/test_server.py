"""Boundary tests for the read-only BMS tool. No live BMS or tunnel required."""

import asyncio

import httpx
import pytest

from server import get_memory, search_memories, mcp

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
    assert set(tools) == {"get_memory", "search_memories"}
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
