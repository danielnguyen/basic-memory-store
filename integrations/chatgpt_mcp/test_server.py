"""Boundary tests for the read-only BMS tool. No live BMS or tunnel required."""

import asyncio

import httpx

from server import get_memory, mcp

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


def test_get_memory_is_only_registered_tool_and_read_only():
    tools = mcp._tool_manager.list_tools()
    assert len(tools) == 1
    assert tools[0].name == "get_memory"
    assert tools[0].annotations.read_only_hint is True
    assert tools[0].annotations.open_world_hint is False
