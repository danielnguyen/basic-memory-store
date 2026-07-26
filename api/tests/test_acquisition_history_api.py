from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import types
from uuid import UUID, uuid4

import pytest
import httpx
from fastapi.testclient import TestClient

import main as main_module


API_KEY = "testkey"
OWNER_ID = "owner-fixture"
SURFACE = "web"


class FakePG:
    def __init__(self, candidates, claim_records=None):
        self.candidates = candidates
        self.calls = []
        self.claim_records = claim_records or []
        self.claim_calls = []
        self.candidate_error = False
        self.claim_error = False

    async def open(self):
        return None

    async def close(self):
        return None

    async def list_assistant_trace_candidates(self, **kwargs):
        if self.candidate_error:
            raise RuntimeError("candidate store unavailable")
        self.calls.append(kwargs)
        return copy.deepcopy(self.candidates)

    async def list_claim_records(self, **kwargs):
        if self.claim_error:
            raise RuntimeError("claim store unavailable")
        self.claim_calls.append(kwargs)
        return [
            copy.deepcopy(record)
            for record in self.claim_records
            if record["owner_id"] == kwargs["owner_id"]
            and record["conversation_id"] == kwargs["conversation_id"]
            and record["assistant_message_id"] == kwargs["assistant_message_id"]
            and record["request_id"] == kwargs["request_id"]
        ][: kwargs["limit"]]


class FakeQdrant:
    def ping(self):
        return True


def _settings():
    return types.SimpleNamespace(
        memory_api_key=API_KEY,
        require_request_id=True,
        enforce_request_id_header_body_match=True,
        enable_trace_storage=True,
    )


def _digest(content: str) -> str:
    return "sha256:" + hashlib.sha256(content.encode("utf-8")).hexdigest()


def _manifest(
    *,
    message_id: str,
    content: str,
    strategy: str = "targeted_retrieval",
    task_shape: str = "targeted_lookup",
    status: str = "sufficient_for_declared_scope",
    include_next_steps: bool = True,
):
    manifest = {
        "enabled": True,
        "attempted": True,
        "status": status,
        "manifest_id": "evidence_manifest_fixture",
        "assistant_message_id": message_id,
        "response_digest": _digest(content),
        "shape": {
            "derivation_status": "derived",
            "task_shape": task_shape,
            "candidate_count": 1,
            "clarification_required": False,
            "reason_codes": ["shape_derived"],
        },
        "inventory": {
            "inventory_status": "complete_for_declared_scope",
            "inventory_source_count": 1,
            "declared_source_count": 1,
        },
        "plan": {
            "plan_id": "evidence_plan_fixture",
            "plan_status": "ready",
            "completeness_expectation": "targeted_scope",
            "contradiction_search_required": False,
            "selected_strategies": [strategy],
            "material_requirement_count": 2,
            "optional_requirement_count": 0,
            "limitation_codes": [],
        },
        "acquisition": {
            "strategy_attempted": strategy,
            "sources_considered": ["source_fixture"],
            "sources_selected": ["source_fixture"],
            "sources_used": ["source_fixture"],
            "source_references_retained": ["source_fixture:item_1"],
            "item_count": 1,
            "prompt_retained_item_count": 1,
            "dsa_outcome": "ok",
            "dsa_error_codes": [],
            "context_delivery_status": "retained",
            "requirement_facts": [],
        },
        "sufficiency": {
            "evaluation_id": "evidence_eval_fixture",
            "status": status,
            "reason_codes": ["material_requirements_satisfied"],
            "answer_constraints": ["answer_with_declared_scope"],
            "qualification_required": status == "sufficient_with_limitations",
            "additional_acquisition_required": status in {"insufficient", "unknown"},
        },
    }
    if include_next_steps:
        manifest["next_steps"] = {
            "selection_count": 1,
            "selections": [
                {
                    "selection_id": "evidence_next_step_fixture",
                    "selected_next_step": "answer_within_declared_scope",
                    "conclusion_disposition": "bounded_conclusion_allowed",
                    "provider_disposition": "allowed",
                    "reacquisition_guard": "not_applicable",
                    "clarification_target": None,
                    "reason_codes": ["declared_scope_sufficient"],
                    "additional_acquisition_executed": False,
                }
            ],
            "additional_acquisition_count": 0,
            "initial_attempt": None,
            "dependency_status": None,
        }
    return manifest


def _candidate(
    *,
    conversation_id: str,
    content: str = "The report supports the migration.",
    request_id: str = "original-request-1",
    message_id: str | None = None,
    manifest=None,
    trace: bool = True,
):
    message_id = message_id or str(uuid4())
    if manifest is None:
        manifest = _manifest(message_id=message_id, content=content)
    return {
        "message_id": message_id,
        "message_owner_id": OWNER_ID,
        "message_conversation_id": conversation_id,
        "message_role": "assistant",
        "message_content": content,
        "message_request_id": request_id,
        "message_created_at": "2026-07-20T00:00:00+00:00",
        "trace_id": str(uuid4()) if trace else None,
        "trace_request_id": request_id if trace else None,
        "trace_owner_id": OWNER_ID if trace else None,
        "trace_conversation_id": conversation_id if trace else None,
        "trace_surface": SURFACE if trace else None,
        "trace_status": "ok" if trace else None,
        "trace_prompt": (
            {
                "evidence_acquisition": manifest,
                "unrelated_prompt_metadata": "PRIVATE PROMPT SENTINEL",
            }
            if trace
            else None
        ),
        "trace_created_at": "2026-07-20T00:00:01+00:00" if trace else None,
        "profile": "PRIVATE PROFILE SENTINEL",
        "retrieval": "PRIVATE RETRIEVAL SENTINEL",
        "router": "PRIVATE ROUTER SENTINEL",
        "model": "PRIVATE MODEL SENTINEL",
        "fallback": "PRIVATE FALLBACK SENTINEL",
        "cost": "PRIVATE COST SENTINEL",
    }


def _request(
    *,
    conversation_id: str,
    content: str,
    target_mode: str = "immediate_previous",
    first_paragraph: str | None = None,
):
    body = {
        "schema_version": "acquisition-history-resolution.v1",
        "request_id": "lookup-request-1",
        "owner_id": OWNER_ID,
        "conversation_id": conversation_id,
        "surface": SURFACE,
        "target_mode": target_mode,
        "normalized_first_paragraph": (
            first_paragraph or "The report supports the migration."
        ),
    }
    if target_mode == "immediate_previous":
        body["response_digest"] = _digest(content)
    return body


def _post(monkeypatch, candidates, body):
    store = FakePG(candidates)
    monkeypatch.setattr(main_module, "settings", _settings(), raising=True)
    monkeypatch.setattr(main_module, "pg", store, raising=True)
    monkeypatch.setattr(main_module, "qdrant", FakeQdrant(), raising=True)
    with TestClient(main_module.app) as client:
        response = client.post(
            "/v1/internal/acquisition-history/resolve",
            headers={
                "X-API-Key": API_KEY,
                "X-Request-ID": body["request_id"],
            },
            json=body,
        )
    return response, store


def _claim_record(*, conversation_id: str, message_id: str, request_id: str):
    anchor = "The report supports the migration."
    return {
        "claim_id": "claim_fixture_1",
        "schema_version": "claim-record.v1",
        "owner_id": OWNER_ID,
        "conversation_id": conversation_id,
        "request_id": request_id,
        "assistant_message_id": message_id,
        "surface": SURFACE,
        "runtime_session_id": "runtime-session-1",
        "runtime_turn_id": "runtime-turn-1",
        "claim_anchor": anchor,
        "claim_anchor_digest": _digest(anchor),
        "claim_class": "source_backed_fact",
        "calibration_status": "supported",
        "evidence_strength": "strong",
        "confidence": "high",
        "strongest_authority": "trusted_integration",
        "freshness_summary": "current",
        "uncertainty_disclosure_required": False,
        "validated_evidence_references": [
            {
                "ref_type": "integration_event",
                "ref_id": "retained-event-1",
                "owner_id": OWNER_ID,
                "conversation_id": conversation_id,
                "support_kind": "direct",
                "authority": "trusted_integration",
                "freshness_state": "active",
            }
        ],
        "limitation_codes": [],
        "user_safe_summary": "A retained integration record directly supports it.",
        "created_at": "2026-07-20T00:00:02+00:00",
    }


def _immediate_request(*, conversation_id: str, explanation_kind: str):
    return {
        "schema_version": "immediate-history-resolution.v1",
        "request_id": "immediate-lookup-1",
        "owner_id": OWNER_ID,
        "conversation_id": conversation_id,
        "surface": SURFACE,
        "explanation_kind": explanation_kind,
    }


def _post_immediate(monkeypatch, store, body, *, authenticated=True):
    monkeypatch.setattr(main_module, "settings", _settings(), raising=True)
    monkeypatch.setattr(main_module, "pg", store, raising=True)
    monkeypatch.setattr(main_module, "qdrant", FakeQdrant(), raising=True)
    headers = {"X-Request-ID": body["request_id"]}
    if authenticated:
        headers["X-API-Key"] = API_KEY
    async def post():
        transport = httpx.ASGITransport(app=main_module.app)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://testserver",
        ) as client:
            return await client.post(
                "/v1/internal/immediate-history/resolve",
                headers=headers,
                json=body,
            )

    return asyncio.run(post())


def test_immediate_history_resolver_requires_internal_authentication(monkeypatch):
    conversation_id = str(uuid4())
    store = FakePG([_candidate(conversation_id=conversation_id)])

    response = _post_immediate(
        monkeypatch,
        store,
        _immediate_request(
            conversation_id=conversation_id,
            explanation_kind="support",
        ),
        authenticated=False,
    )

    assert response.status_code == 401
    assert store.calls == []
    assert store.claim_calls == []


def test_immediate_support_resolves_exact_newest_assistant_claim(monkeypatch):
    conversation_id = str(uuid4())
    newest = _candidate(
        conversation_id=conversation_id,
        content=(
            "The report supports the migration.\n\n"
            "PRIVATE ASSISTANT RESPONSE TAIL"
        ),
    )
    older = _candidate(
        conversation_id=conversation_id,
        content="An older supported response.",
        request_id="original-request-old",
    )
    claim = _claim_record(
        conversation_id=conversation_id,
        message_id=newest["message_id"],
        request_id=newest["message_request_id"],
    )
    store = FakePG([newest, older], [claim])

    response = _post_immediate(
        monkeypatch,
        store,
        _immediate_request(
            conversation_id=conversation_id,
            explanation_kind="support",
        ),
    )

    assert response.status_code == 200, response.text
    result = response.json()
    assert result["resolution_status"] == "resolved"
    assert result["reason_code"] == "support_record_resolved"
    assert result["record"]["record_kind"] == "support"
    assert result["record"]["assistant_message_id"] == newest["message_id"]
    assert result["record"]["original_request_id"] == newest["message_request_id"]
    assert result["record"]["support_record"] == claim
    assert result["record"]["acquisition_record"] is None
    assert "PRIVATE ASSISTANT RESPONSE TAIL" not in response.text
    assert store.calls == [
        {
            "owner_id": OWNER_ID,
            "conversation_id": UUID(conversation_id),
            "limit": 1,
        }
    ]
    assert store.claim_calls == [
        {
            "owner_id": OWNER_ID,
            "conversation_id": conversation_id,
            "assistant_message_id": newest["message_id"],
            "request_id": newest["message_request_id"],
            "limit": 2,
        }
    ]


def test_immediate_acquisition_resolves_without_client_history_hints(monkeypatch):
    conversation_id = str(uuid4())
    candidate = _candidate(conversation_id=conversation_id)
    store = FakePG([candidate])

    response = _post_immediate(
        monkeypatch,
        store,
        _immediate_request(
            conversation_id=conversation_id,
            explanation_kind="acquisition",
        ),
    )

    assert response.status_code == 200, response.text
    result = response.json()
    assert result["resolution_status"] == "resolved"
    assert result["reason_code"] == "acquisition_record_resolved"
    assert result["record"]["record_kind"] == "acquisition"
    assert result["record"]["support_record"] is None
    assert result["record"]["acquisition_record"]["acquisition_manifest"] == (
        candidate["trace_prompt"]["evidence_acquisition"]
    )
    assert store.calls[0]["limit"] == 1
    assert store.claim_calls == []


def test_immediate_support_missing_on_newest_never_scans_backward(monkeypatch):
    conversation_id = str(uuid4())
    newest = _candidate(
        conversation_id=conversation_id,
        content="Newest response without retained support.",
        request_id="original-request-newest",
    )
    older = _candidate(conversation_id=conversation_id)
    older_claim = _claim_record(
        conversation_id=conversation_id,
        message_id=older["message_id"],
        request_id=older["message_request_id"],
    )
    store = FakePG([newest, older], [older_claim])

    response = _post_immediate(
        monkeypatch,
        store,
        _immediate_request(
            conversation_id=conversation_id,
            explanation_kind="support",
        ),
    )

    assert response.status_code == 200
    assert response.json()["resolution_status"] == "no_record"
    assert response.json()["reason_code"] == "support_record_not_found"
    assert response.json()["record"] is None
    assert store.calls[0]["limit"] == 1
    assert store.claim_calls[0]["assistant_message_id"] == newest["message_id"]


def test_immediate_acquisition_missing_on_newest_never_scans_backward(monkeypatch):
    conversation_id = str(uuid4())
    newest = _candidate(
        conversation_id=conversation_id,
        content="Newest response without a trace.",
        trace=False,
    )
    older = _candidate(conversation_id=conversation_id)
    store = FakePG([newest, older])

    response = _post_immediate(
        monkeypatch,
        store,
        _immediate_request(
            conversation_id=conversation_id,
            explanation_kind="acquisition",
        ),
    )

    assert response.status_code == 200
    assert response.json()["resolution_status"] == "no_record"
    assert response.json()["reason_code"] == "acquisition_record_not_found"
    assert response.json()["record"] is None
    assert store.calls[0]["limit"] == 1


def test_immediate_support_multiple_records_is_bounded_ambiguous(monkeypatch):
    conversation_id = str(uuid4())
    candidate = _candidate(conversation_id=conversation_id)
    claim = _claim_record(
        conversation_id=conversation_id,
        message_id=candidate["message_id"],
        request_id=candidate["message_request_id"],
    )
    second_claim = copy.deepcopy(claim)
    second_claim["claim_id"] = "claim_fixture_2"
    store = FakePG([candidate], [claim, second_claim])

    response = _post_immediate(
        monkeypatch,
        store,
        _immediate_request(
            conversation_id=conversation_id,
            explanation_kind="support",
        ),
    )

    assert response.status_code == 200
    assert response.json()["resolution_status"] == "ambiguous"
    assert response.json()["match_count"] == 2
    assert response.json()["reason_code"] == "support_record_ambiguous"
    assert response.json()["record"] is None
    assert store.claim_calls[0]["limit"] == 2


def test_immediate_acquisition_privacy_failure_returns_no_record_data(monkeypatch):
    conversation_id = str(uuid4())
    candidate = _candidate(conversation_id=conversation_id)
    candidate["trace_prompt"]["evidence_acquisition"]["acquisition"][
        "raw_payload"
    ] = "PRIVATE ACQUISITION SENTINEL"
    store = FakePG([candidate])

    response = _post_immediate(
        monkeypatch,
        store,
        _immediate_request(
            conversation_id=conversation_id,
            explanation_kind="acquisition",
        ),
    )

    assert response.status_code == 200
    assert response.json()["resolution_status"] == "invalid"
    assert response.json()["reason_code"] == "acquisition_record_invalid"
    assert response.json()["record"] is None
    assert "PRIVATE ACQUISITION SENTINEL" not in response.text


def test_immediate_acquisition_surface_mismatch_fails_closed(monkeypatch):
    conversation_id = str(uuid4())
    candidate = _candidate(conversation_id=conversation_id)
    candidate["trace_surface"] = "other-surface"
    store = FakePG([candidate])

    response = _post_immediate(
        monkeypatch,
        store,
        _immediate_request(
            conversation_id=conversation_id,
            explanation_kind="acquisition",
        ),
    )

    assert response.status_code == 200
    assert response.json()["resolution_status"] == "invalid"
    assert response.json()["reason_code"] == "acquisition_record_invalid"
    assert response.json()["record"] is None


@pytest.mark.parametrize(
    "mutation",
    ["owner", "conversation", "role", "content", "request_id"],
)
def test_immediate_newest_message_scope_or_shape_mismatch_fails_closed(
    monkeypatch,
    mutation,
):
    conversation_id = str(uuid4())
    candidate = _candidate(conversation_id=conversation_id)
    if mutation == "owner":
        candidate["message_owner_id"] = "other-owner"
    elif mutation == "conversation":
        candidate["message_conversation_id"] = str(uuid4())
    elif mutation == "role":
        candidate["message_role"] = "user"
    elif mutation == "content":
        candidate["message_content"] = None
    else:
        candidate["message_request_id"] = None
    store = FakePG([candidate])

    response = _post_immediate(
        monkeypatch,
        store,
        _immediate_request(
            conversation_id=conversation_id,
            explanation_kind="support",
        ),
    )

    assert response.status_code == 200
    assert response.json()["resolution_status"] == "invalid"
    assert response.json()["reason_code"] == "immediate_response_invalid"
    assert response.json()["record"] is None
    assert store.claim_calls == []


@pytest.mark.parametrize(
    "mutation",
    [
        "owner",
        "conversation",
        "message",
        "request",
        "surface",
        "anchor",
        "digest",
        "evidence_owner",
    ],
)
def test_immediate_support_record_scope_mismatch_fails_closed(monkeypatch, mutation):
    conversation_id = str(uuid4())
    candidate = _candidate(conversation_id=conversation_id)
    claim = _claim_record(
        conversation_id=conversation_id,
        message_id=candidate["message_id"],
        request_id=candidate["message_request_id"],
    )
    if mutation == "owner":
        claim["owner_id"] = "other-owner"
    elif mutation == "conversation":
        claim["conversation_id"] = str(uuid4())
    elif mutation == "message":
        claim["assistant_message_id"] = str(uuid4())
    elif mutation == "request":
        claim["request_id"] = "other-request"
    elif mutation == "surface":
        claim["surface"] = "other-surface"
    elif mutation == "anchor":
        claim["claim_anchor"] = "A different assistant response."
    elif mutation == "digest":
        claim["claim_anchor_digest"] = "sha256:" + ("f" * 64)
    else:
        claim["validated_evidence_references"][0]["owner_id"] = "other-owner"
    store = FakePG([candidate])

    async def return_unscoped_record(**kwargs):
        store.claim_calls.append(kwargs)
        return [copy.deepcopy(claim)]

    store.list_claim_records = return_unscoped_record
    response = _post_immediate(
        monkeypatch,
        store,
        _immediate_request(
            conversation_id=conversation_id,
            explanation_kind="support",
        ),
    )

    assert response.status_code == 200
    assert response.json()["resolution_status"] == "invalid"
    assert response.json()["reason_code"] == "support_record_invalid"
    assert response.json()["record"] is None


@pytest.mark.parametrize("failure", ["candidate", "claim"])
def test_immediate_history_store_failure_is_bounded_unavailable(monkeypatch, failure):
    conversation_id = str(uuid4())
    candidate = _candidate(conversation_id=conversation_id)
    store = FakePG([candidate])
    if failure == "candidate":
        store.candidate_error = True
    else:
        store.claim_error = True

    response = _post_immediate(
        monkeypatch,
        store,
        _immediate_request(
            conversation_id=conversation_id,
            explanation_kind="support",
        ),
    )

    assert response.status_code == 200
    assert response.json()["resolution_status"] == "unavailable"
    assert response.json()["reason_code"] == "history_store_unavailable"
    assert response.json()["record"] is None


@pytest.mark.parametrize(
    "extra_field",
    [
        "previous_assistant_text",
        "response_digest",
        "normalized_first_paragraph",
        "assistant_message_id",
        "claim_id",
        "trace_id",
        "acquisition_manifest_id",
    ],
)
def test_immediate_request_rejects_client_owned_history_hints(
    monkeypatch,
    extra_field,
):
    conversation_id = str(uuid4())
    body = _immediate_request(
        conversation_id=conversation_id,
        explanation_kind="support",
    )
    body[extra_field] = "client-supplied-history"
    store = FakePG([_candidate(conversation_id=conversation_id)])

    response = _post_immediate(monkeypatch, store, body)

    assert response.status_code == 422
    assert store.calls == []
    assert store.claim_calls == []


def test_acquisition_history_resolver_requires_internal_authentication(monkeypatch):
    conversation_id = str(uuid4())
    content = "The report supports the migration."
    store = FakePG([_candidate(conversation_id=conversation_id, content=content)])
    monkeypatch.setattr(main_module, "settings", _settings(), raising=True)
    monkeypatch.setattr(main_module, "pg", store, raising=True)
    monkeypatch.setattr(main_module, "qdrant", FakeQdrant(), raising=True)
    with TestClient(main_module.app) as client:
        response = client.post(
            "/v1/internal/acquisition-history/resolve",
            headers={"X-Request-ID": "lookup-request-1"},
            json=_request(conversation_id=conversation_id, content=content),
        )
    assert response.status_code == 401
    assert store.calls == []


def test_immediate_mult_paragraph_response_resolves_exact_manifest(monkeypatch):
    conversation_id = str(uuid4())
    content = (
        "The report   supports\n the migration.\n\n"
        "This reflects only the bounded source checked."
    )
    candidate = _candidate(conversation_id=conversation_id, content=content)
    body = _request(
        conversation_id=conversation_id,
        content=content,
        first_paragraph="The report supports the migration.",
    )

    response, store = _post(monkeypatch, [candidate], body)

    assert response.status_code == 200, response.text
    result = response.json()
    assert result["resolution_status"] == "resolved"
    assert result["reason_code"] == "immediate_response_resolved"
    assert result["match_count"] == 1
    assert result["record"]["response_digest"] == _digest(content)
    assert result["record"]["normalized_first_paragraph"] == (
        "The report supports the migration."
    )
    assert result["record"]["acquisition_manifest"] == candidate[
        "trace_prompt"
    ]["evidence_acquisition"]
    serialized = json.dumps(result, sort_keys=True)
    assert "This reflects only the bounded source checked." not in serialized
    for sentinel in (
        "PRIVATE PROMPT SENTINEL",
        "PRIVATE PROFILE SENTINEL",
        "PRIVATE RETRIEVAL SENTINEL",
        "PRIVATE ROUTER SENTINEL",
        "PRIVATE MODEL SENTINEL",
        "PRIVATE FALLBACK SENTINEL",
        "PRIVATE COST SENTINEL",
    ):
        assert sentinel not in serialized
    assert store.calls == [
        {
            "owner_id": OWNER_ID,
            "conversation_id": UUID(conversation_id),
            "limit": 1,
        }
    ]


@pytest.mark.parametrize("mismatch", ["digest", "paragraph"])
def test_immediate_mismatch_never_scans_backward(monkeypatch, mismatch):
    conversation_id = str(uuid4())
    newest_content = "A newer unrelated response."
    older_content = "The report supports the migration."
    body = _request(conversation_id=conversation_id, content=newest_content)
    if mismatch == "digest":
        body["response_digest"] = _digest(older_content)
        body["normalized_first_paragraph"] = newest_content
    else:
        body["normalized_first_paragraph"] = older_content

    response, store = _post(
        monkeypatch,
        [
            _candidate(conversation_id=conversation_id, content=newest_content),
            _candidate(conversation_id=conversation_id, content=older_content),
        ],
        body,
    )

    assert response.status_code == 200
    assert response.json()["resolution_status"] == "no_record"
    assert response.json()["reason_code"] == "immediate_response_mismatch"
    assert response.json()["record"] is None
    assert store.calls[0]["limit"] == 1


@pytest.mark.parametrize("stored_content", [None, "", {"not": "a string"}])
def test_immediate_missing_or_malformed_message_content_fails_closed(
    monkeypatch,
    stored_content,
):
    conversation_id = str(uuid4())
    expected_content = "The report supports the migration."
    candidate = _candidate(
        conversation_id=conversation_id,
        content=expected_content,
    )
    candidate["message_content"] = stored_content
    response, store = _post(
        monkeypatch,
        [candidate],
        _request(
            conversation_id=conversation_id,
            content=expected_content,
        ),
    )
    assert response.status_code == 200
    assert response.json()["resolution_status"] == "no_record"
    assert response.json()["reason_code"] == "immediate_response_mismatch"
    assert response.json()["record"] is None
    assert store.calls[0]["limit"] == 1


@pytest.mark.parametrize("missing", ["trace", "manifest", "malformed"])
def test_immediate_missing_or_invalid_newest_never_scans_backward(
    monkeypatch,
    missing,
):
    conversation_id = str(uuid4())
    content = "The report supports the migration."
    newest = _candidate(conversation_id=conversation_id, content=content)
    if missing == "trace":
        newest = _candidate(
            conversation_id=conversation_id,
            content=content,
            trace=False,
        )
    elif missing == "manifest":
        newest["trace_prompt"] = {}
    else:
        newest["trace_prompt"]["evidence_acquisition"][
            "assistant_message_id"
        ] = str(uuid4())

    response, store = _post(
        monkeypatch,
        [newest, _candidate(conversation_id=conversation_id, content=content)],
        _request(conversation_id=conversation_id, content=content),
    )

    assert response.status_code == 200
    assert response.json()["resolution_status"] in {"no_record", "invalid"}
    assert response.json()["record"] is None
    assert store.calls[0]["limit"] == 1


@pytest.mark.parametrize(
    "content",
    [
        "Context heading\n\nThe report supports the migration.",
        "- Context note\n\nThe report supports the migration.",
        "\n\nThe report supports the migration.",
        "The report was reviewed.\n\nThe report supports the migration.",
    ],
)
def test_immediate_claim_later_or_after_prefix_fails_closed(monkeypatch, content):
    conversation_id = str(uuid4())
    response, store = _post(
        monkeypatch,
        [_candidate(conversation_id=conversation_id, content=content)],
        _request(
            conversation_id=conversation_id,
            content=content,
            first_paragraph="The report supports the migration.",
        ),
    )
    assert response.status_code == 200
    assert response.json()["resolution_status"] == "no_record"
    assert response.json()["record"] is None
    assert store.calls[0]["limit"] == 1


def test_quoted_exact_older_first_paragraph_resolves_without_claim(monkeypatch):
    conversation_id = str(uuid4())
    target = "The report supports the migration."
    candidate = _candidate(
        conversation_id=conversation_id,
        content=f"{target}\n\nA retained policy boundary.",
    )
    response, store = _post(
        monkeypatch,
        [
            _candidate(
                conversation_id=conversation_id,
                content="A newer unrelated response.",
            ),
            candidate,
        ],
        _request(
            conversation_id=conversation_id,
            content=target,
            target_mode="quoted_first_paragraph",
            first_paragraph=target,
        ),
    )

    assert response.status_code == 200
    assert response.json()["reason_code"] == "quoted_response_resolved"
    assert response.json()["record"]["original_request_id"] == (
        candidate["message_request_id"]
    )
    assert store.calls[0]["limit"] == 50
    assert not hasattr(store, "list_claim_records")


@pytest.mark.parametrize(
    "target",
    [
        "the report supports the migration.",
        "report supports",
        "The report supports the migration safely.",
    ],
)
def test_quoted_matching_is_case_sensitive_exact_and_non_fuzzy(monkeypatch, target):
    conversation_id = str(uuid4())
    response, _ = _post(
        monkeypatch,
        [_candidate(conversation_id=conversation_id)],
        _request(
            conversation_id=conversation_id,
            content=target,
            target_mode="quoted_first_paragraph",
            first_paragraph=target,
        ),
    )
    assert response.status_code == 200
    assert response.json()["resolution_status"] == "no_record"
    assert response.json()["reason_code"] == "quoted_response_not_found"


def test_quoted_duplicate_exact_matches_are_ambiguous(monkeypatch):
    conversation_id = str(uuid4())
    content = "The report supports the migration."
    response, _ = _post(
        monkeypatch,
        [
            _candidate(conversation_id=conversation_id, content=content),
            _candidate(conversation_id=conversation_id, content=content),
        ],
        _request(
            conversation_id=conversation_id,
            content=content,
            target_mode="quoted_first_paragraph",
        ),
    )
    assert response.status_code == 200
    assert response.json()["resolution_status"] == "ambiguous"
    assert response.json()["match_count"] == 2
    assert response.json()["record"] is None


def test_degraded_trace_with_valid_association_resolves(monkeypatch):
    conversation_id = str(uuid4())
    content = "The report supports the migration."
    candidate = _candidate(conversation_id=conversation_id, content=content)
    candidate["trace_status"] = "degraded"
    response, _ = _post(
        monkeypatch,
        [candidate],
        _request(conversation_id=conversation_id, content=content),
    )
    assert response.status_code == 200
    assert response.json()["resolution_status"] == "resolved"
    assert response.json()["record"]["trace_status"] == "degraded"


def test_quoted_search_window_is_bounded_to_fifty(monkeypatch):
    conversation_id = str(uuid4())
    candidates = [
        _candidate(
            conversation_id=conversation_id,
            content=f"Unrelated response {index}.",
        )
        for index in range(50)
    ]
    candidates.append(_candidate(conversation_id=conversation_id))
    response, store = _post(
        monkeypatch,
        candidates,
        _request(
            conversation_id=conversation_id,
            content="The report supports the migration.",
            target_mode="quoted_first_paragraph",
        ),
    )
    assert response.status_code == 200
    assert response.json()["resolution_status"] == "no_record"
    assert store.calls[0]["limit"] == 50


@pytest.mark.parametrize(
    "mutation,reason_code",
    [
        ("message_owner", "manifest_association_invalid"),
        ("message_conversation", "manifest_association_invalid"),
        ("message_role", "manifest_association_invalid"),
        ("message_request", "assistant_message_request_mismatch"),
        ("message_request_nonstring", "assistant_message_request_mismatch"),
        ("trace_request", "assistant_message_request_mismatch"),
        ("trace_owner", "trace_scope_mismatch"),
        ("trace_conversation", "trace_scope_mismatch"),
        ("trace_surface", "trace_scope_mismatch"),
        ("trace_status", "manifest_association_invalid"),
        ("manifest_message", "manifest_association_invalid"),
        ("manifest_digest", "manifest_association_invalid"),
        ("manifest_digest_missing", "manifest_association_invalid"),
        ("attempted", "manifest_association_invalid"),
    ],
)
def test_trace_and_manifest_association_fail_closed(
    monkeypatch,
    mutation,
    reason_code,
):
    conversation_id = str(uuid4())
    content = "The report supports the migration."
    candidate = _candidate(conversation_id=conversation_id, content=content)
    manifest = candidate["trace_prompt"]["evidence_acquisition"]
    if mutation == "message_owner":
        candidate["message_owner_id"] = "other-owner"
    elif mutation == "message_conversation":
        candidate["message_conversation_id"] = str(uuid4())
    elif mutation == "message_role":
        candidate["message_role"] = "user"
    elif mutation == "message_request":
        candidate["message_request_id"] = None
    elif mutation == "message_request_nonstring":
        candidate["message_request_id"] = 123
    elif mutation == "trace_request":
        candidate["trace_request_id"] = "other-request"
    elif mutation == "trace_owner":
        candidate["trace_owner_id"] = "other-owner"
    elif mutation == "trace_conversation":
        candidate["trace_conversation_id"] = str(uuid4())
    elif mutation == "trace_surface":
        candidate["trace_surface"] = "other-surface"
    elif mutation == "trace_status":
        candidate["trace_status"] = "failed"
    elif mutation == "manifest_message":
        manifest["assistant_message_id"] = str(uuid4())
    elif mutation == "manifest_digest":
        manifest["response_digest"] = "sha256:" + ("f" * 64)
    elif mutation == "manifest_digest_missing":
        manifest["response_digest"] = None
    else:
        manifest["attempted"] = False

    response, _ = _post(
        monkeypatch,
        [candidate],
        _request(conversation_id=conversation_id, content=content),
    )
    assert response.status_code == 200
    result = response.json()
    assert result["resolution_status"] == "invalid"
    assert result["reason_code"] == reason_code
    assert result["record"] is None
    serialized = json.dumps(result, sort_keys=True)
    assert content not in serialized
    assert candidate["message_id"] not in serialized
    retained_digest = manifest.get("response_digest")
    if isinstance(retained_digest, str):
        assert retained_digest not in serialized


@pytest.mark.parametrize(
    "strategy,task_shape,status,include_next_steps",
    [
        ("targeted_retrieval", "targeted_lookup", "sufficient_for_declared_scope", True),
        ("exact_fetch", "targeted_lookup", "sufficient_for_declared_scope", True),
        ("hybrid", "cross_source_comparison", "sufficient_for_declared_scope", True),
        ("hybrid", "bounded_exhaustive_review", "sufficient_for_declared_scope", True),
        ("targeted_retrieval", "targeted_lookup", "sufficient_with_limitations", True),
        ("targeted_retrieval", "targeted_lookup", "insufficient", True),
        ("targeted_retrieval", "targeted_lookup", "unknown", True),
        ("targeted_retrieval", "targeted_lookup", "sufficient_for_declared_scope", False),
    ],
)
def test_supported_no_claim_history_manifests_resolve(
    monkeypatch,
    strategy,
    task_shape,
    status,
    include_next_steps,
):
    conversation_id = str(uuid4())
    content = (
        "The available evidence remains incomplete."
        if status in {"insufficient", "unknown"}
        else "The report supports the migration."
    )
    message_id = str(uuid4())
    manifest = _manifest(
        message_id=message_id,
        content=content,
        strategy=strategy,
        task_shape=task_shape,
        status=status,
        include_next_steps=include_next_steps,
    )
    candidate = _candidate(
        conversation_id=conversation_id,
        content=content,
        message_id=message_id,
        manifest=manifest,
    )
    response, _ = _post(
        monkeypatch,
        [candidate],
        _request(
            conversation_id=conversation_id,
            content=content,
            first_paragraph=content,
        ),
    )
    assert response.status_code == 200, response.text
    assert response.json()["resolution_status"] == "resolved"
    assert response.json()["record"]["acquisition_manifest"] == manifest


def test_privacy_suppressed_aggregate_manifest_resolves_without_reconstruction(
    monkeypatch,
):
    conversation_id = str(uuid4())
    content = "The report supports the migration."
    candidate = _candidate(conversation_id=conversation_id, content=content)
    acquisition = candidate["trace_prompt"]["evidence_acquisition"]["acquisition"]
    acquisition.update(
        {
            "source_identifiers_suppressed": True,
            "sources_considered": [],
            "sources_considered_count": 2,
            "sources_selected": [],
            "sources_selected_count": 1,
            "sources_used": [],
            "source_references_retained": [],
            "source_references_retained_count": 1,
        }
    )
    response, _ = _post(
        monkeypatch,
        [candidate],
        _request(conversation_id=conversation_id, content=content),
    )
    manifest = response.json()["record"]["acquisition_manifest"]
    assert manifest["acquisition"]["sources_considered"] == []
    assert manifest["acquisition"]["sources_considered_count"] == 2
    assert "source_fixture" not in json.dumps(manifest)


@pytest.mark.parametrize(
    "private_key",
    [
        "content",
        "raw_payload",
        "private_provider_response",
        "source_config_path",
        "credential_reference",
        "unrestricted_url",
    ],
)
def test_private_nested_manifest_keys_fail_closed(monkeypatch, private_key):
    conversation_id = str(uuid4())
    content = "The report supports the migration."
    candidate = _candidate(conversation_id=conversation_id, content=content)
    candidate["trace_prompt"]["evidence_acquisition"]["acquisition"][
        private_key
    ] = "PRIVATE SENTINEL"
    response, _ = _post(
        monkeypatch,
        [candidate],
        _request(conversation_id=conversation_id, content=content),
    )
    assert response.status_code == 200
    assert response.json()["resolution_status"] == "invalid"
    assert response.json()["reason_code"] == "manifest_privacy_boundary_invalid"
    assert response.json()["record"] is None
    assert "PRIVATE SENTINEL" not in response.text


@pytest.mark.parametrize(
    "private_key",
    [
        "api_key",
        "apiKey",
        "APIKey",
        "api-key",
        "API Key",
        "apikey",
        "access_token",
        "accessToken",
        "Access Token",
        "refresh-token",
        "RefreshToken",
        "auth_token",
        "authorization",
        "Authorization",
        "authorization_header",
        "AuthorizationHeader",
        "bearer_token",
        "Bearer Token",
        "password",
        "passwd",
        "passphrase",
        "client_secret",
        "clientSecret",
        "Client Secret",
        "private_key",
        "privateKey",
        "signing_key",
        "session_token",
        "session_cookie",
        "set_cookie",
    ],
)
@pytest.mark.parametrize(
    "location",
    ["acquisition", "next_steps", "nested_acquisition"],
)
def test_credential_aliases_fail_closed_at_every_manifest_depth(
    monkeypatch,
    private_key,
    location,
):
    conversation_id = str(uuid4())
    content = "The report supports the migration."
    candidate = _candidate(conversation_id=conversation_id, content=content)
    manifest = candidate["trace_prompt"]["evidence_acquisition"]
    if location == "nested_acquisition":
        manifest["acquisition"]["bounded_metadata"] = {
            private_key: "PRIVATE SECRET SENTINEL"
        }
    else:
        manifest[location][private_key] = "PRIVATE SECRET SENTINEL"

    response, _ = _post(
        monkeypatch,
        [candidate],
        _request(conversation_id=conversation_id, content=content),
    )

    assert response.status_code == 200
    result = response.json()
    assert result["resolution_status"] == "invalid"
    assert result["reason_code"] == "manifest_privacy_boundary_invalid"
    assert result["record"] is None
    assert "PRIVATE SECRET SENTINEL" not in response.text


def test_post_next_step_manifest_structural_keys_remain_valid(monkeypatch):
    conversation_id = str(uuid4())
    content = "The report supports the migration."
    candidate = _candidate(conversation_id=conversation_id, content=content)
    manifest = candidate["trace_prompt"]["evidence_acquisition"]
    selection = manifest["next_steps"]["selections"][0]
    selection.update(
        {
            "evaluation_id": "evidence_eval_fixture",
            "evidence_plan_id": "evidence_plan_fixture",
            "acquisition_manifest_id": "evidence_manifest_fixture",
        }
    )

    response, _ = _post(
        monkeypatch,
        [candidate],
        _request(conversation_id=conversation_id, content=content),
    )

    assert response.status_code == 200, response.text
    result = response.json()
    assert result["resolution_status"] == "resolved"
    assert result["record"]["acquisition_manifest"] == manifest


@pytest.mark.parametrize("unbounded", ["oversized", "deep", "extra_top_level"])
def test_unbounded_manifest_fails_closed(monkeypatch, unbounded):
    conversation_id = str(uuid4())
    content = "The report supports the migration."
    candidate = _candidate(conversation_id=conversation_id, content=content)
    manifest = candidate["trace_prompt"]["evidence_acquisition"]
    if unbounded == "oversized":
        manifest["acquisition"]["warnings"] = ["x" * 1001]
    elif unbounded == "deep":
        nested = {}
        manifest["acquisition"]["bounded"] = nested
        for _ in range(10):
            nested["nested"] = {}
            nested = nested["nested"]
    else:
        manifest["private_metadata"] = {}
    response, _ = _post(
        monkeypatch,
        [candidate],
        _request(conversation_id=conversation_id, content=content),
    )
    assert response.status_code == 200
    assert response.json()["reason_code"] == "manifest_privacy_boundary_invalid"


@pytest.mark.parametrize(
    "updates",
    [
        {"extra": "forbidden"},
        {"target_mode": "immediate_previous", "response_digest": None},
        {
            "target_mode": "quoted_first_paragraph",
            "response_digest": "sha256:" + ("a" * 64),
        },
    ],
)
def test_request_contract_rejects_extra_and_conflicting_fields(monkeypatch, updates):
    conversation_id = str(uuid4())
    content = "The report supports the migration."
    body = _request(conversation_id=conversation_id, content=content)
    body.update(updates)
    response, store = _post(
        monkeypatch,
        [_candidate(conversation_id=conversation_id, content=content)],
        body,
    )
    assert response.status_code == 422
    assert store.calls == []
