from __future__ import annotations

import asyncio
from hashlib import sha256
import types
from uuid import uuid4

import httpx
import pytest

import main as main_module


class FakeQdrant:
    def ping(self):
        return True


class FakePG:
    def __init__(self):
        self.create_calls = []
        self.error_code: str | None = None
        self.records: list[dict] = []

    async def open(self):
        return None

    async def close(self):
        return None

    async def create_claim_record(
        self,
        *,
        record,
        validate_association,
    ):
        self.create_calls.append(record)
        if self.error_code:
            raise main_module.ClaimRecordError(self.error_code)
        stored = {**record, "created_at": "2026-07-14T23:00:00+00:00"}
        self.records = [stored]
        return {"created": True, "record": stored}

    async def get_claim_record(self, *, claim_id, owner_id, conversation_id):
        return next(
            (
                record
                for record in self.records
                if record["claim_id"] == claim_id
                and record["owner_id"] == owner_id
                and record["conversation_id"] == conversation_id
            ),
            None,
        )

    async def list_claim_records(
        self,
        *,
        owner_id,
        conversation_id,
        assistant_message_id,
        request_id,
        limit,
    ):
        return [
            record
            for record in self.records
            if record["owner_id"] == owner_id
            and record["conversation_id"] == conversation_id
            and (assistant_message_id is None or record["assistant_message_id"] == assistant_message_id)
            and (request_id is None or record["request_id"] == request_id)
        ][:limit]


class ApiClient:
    def request(self, method: str, path: str, **kwargs):
        async def run():
            transport = httpx.ASGITransport(app=main_module.app)
            async with httpx.AsyncClient(
                transport=transport,
                base_url="http://testserver",
            ) as client:
                return await client.request(method, path, **kwargs)

        return asyncio.run(run())

    def post(self, path: str, **kwargs):
        return self.request("POST", path, **kwargs)

    def get(self, path: str, **kwargs):
        return self.request("GET", path, **kwargs)


def _settings():
    return types.SimpleNamespace(
        memory_api_key="testkey",
        require_request_id=True,
        enforce_request_id_header_body_match=True,
    )


def _body() -> dict:
    anchor = "The display setting was applied."
    return {
        "schema_version": "claim-record.v1",
        "request_id": "request-claim-1",
        "owner_id": "owner-claim-1",
        "conversation_id": str(uuid4()),
        "assistant_message_id": str(uuid4()),
        "surface": "desktop_private",
        "runtime_session_id": "session-claim-1",
        "runtime_turn_id": "turn-claim-1",
        "calibration_result": {
            "claim_id": "claim_0123456789abcdef0123456789abcdef",
            "claim_anchor": anchor,
            "claim_anchor_digest": "sha256:" + sha256(anchor.encode()).hexdigest(),
            "claim_class": "manufacturer_guidance",
            "calibration_status": "limited",
            "evidence_strength": "moderate",
            "confidence": "medium",
            "strongest_authority": "manufacturer_guidance",
            "freshness_summary": "current",
            "uncertainty_disclosure_required": True,
            "validated_evidence_references": [
                {
                    "ref_type": "external_source",
                    "ref_id": "source-manufacturer-1",
                    "owner_id": "owner-claim-1",
                    "conversation_id": None,
                    "support_kind": "direct",
                    "authority": "manufacturer_guidance",
                    "freshness_state": "active",
                }
            ],
            "limitation_codes": ["single_source"],
            "user_safe_summary": "This claim has one current recorded source.",
        },
    }


@pytest.fixture
def client_and_store(monkeypatch):
    store = FakePG()
    monkeypatch.setattr(main_module, "settings", _settings(), raising=True)
    monkeypatch.setattr(main_module, "pg", store, raising=True)
    monkeypatch.setattr(main_module, "qdrant", FakeQdrant(), raising=True)
    return ApiClient(), store


def _create(client: ApiClient, body: dict):
    return client.post(
        "/v1/internal/claim-records",
        headers={"X-API-Key": "testkey", "X-Request-ID": body["request_id"]},
        json=body,
    )


def test_create_claim_record_is_strict_and_preserves_calibration(client_and_store):
    client, store = client_and_store
    body = _body()

    response = _create(client, body)

    assert response.status_code == 200, response.text
    assert response.json()["created"] is True
    assert response.json()["record"]["claim_class"] == "manufacturer_guidance"
    assert response.json()["record"]["confidence"] == "medium"
    assert store.create_calls[0]["user_safe_summary"] == body["calibration_result"]["user_safe_summary"]


@pytest.mark.parametrize(
    ("path", "value"),
    [
        (("unexpected",), "value"),
        (("calibration_result", "snippet"), "private source text"),
        (("calibration_result", "raw_content"), "raw body"),
        (("calibration_result", "prompt"), "hidden prompt"),
        (("calibration_result", "reasoning"), "hidden reasoning"),
        (("calibration_result", "metadata"), {"arbitrary": True}),
        (("calibration_result", "validated_evidence_references", 0, "url"), "https://secret"),
    ],
)
def test_create_rejects_extra_or_raw_content_fields(client_and_store, path, value):
    client, store = client_and_store
    body = _body()
    target = body
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value

    response = _create(client, body)

    assert response.status_code == 422
    assert store.create_calls == []


def test_create_rejects_digest_mismatch(client_and_store):
    client, store = client_and_store
    body = _body()
    body["calibration_result"]["claim_anchor_digest"] = "sha256:" + "0" * 64

    response = _create(client, body)

    assert response.status_code == 422
    assert response.json()["detail"] == "claim_anchor_digest_mismatch"
    assert store.create_calls == []


def test_create_rejects_duplicate_or_cross_scope_evidence(client_and_store):
    client, store = client_and_store
    duplicate = _body()
    duplicate["calibration_result"]["validated_evidence_references"] *= 2
    cross_owner = _body()
    cross_owner["calibration_result"]["validated_evidence_references"][0]["owner_id"] = "other"
    cross_conversation = _body()
    cross_conversation["calibration_result"]["validated_evidence_references"][0][
        "conversation_id"
    ] = str(uuid4())

    assert _create(client, duplicate).status_code == 422
    assert _create(client, cross_owner).status_code == 422
    assert _create(client, cross_conversation).status_code == 422
    assert store.create_calls == []


def test_create_sorts_evidence_and_rejects_duplicate_limitations(client_and_store):
    client, store = client_and_store
    body = _body()
    later = body["calibration_result"]["validated_evidence_references"][0]
    earlier = {
        **later,
        "ref_type": "artifact",
        "ref_id": str(uuid4()),
    }
    body["calibration_result"]["validated_evidence_references"] = [later, earlier]

    response = _create(client, body)

    assert response.status_code == 200
    assert [
        reference["ref_type"]
        for reference in store.create_calls[0]["validated_evidence_references"]
    ] == ["artifact", "external_source"]

    duplicate_limitations = _body()
    duplicate_limitations["calibration_result"]["limitation_codes"] = [
        "single_source",
        "single_source",
    ]
    assert _create(client, duplicate_limitations).status_code == 422


def test_create_rejects_unbounded_anchor_identifiers_and_reference_count(client_and_store):
    client, store = client_and_store
    overlong_anchor = _body()
    overlong_anchor["calibration_result"]["claim_anchor"] = "x" * 501
    overlong_identifier = _body()
    overlong_identifier["runtime_turn_id"] = "x" * 121
    too_many_references = _body()
    template = too_many_references["calibration_result"]["validated_evidence_references"][0]
    too_many_references["calibration_result"]["validated_evidence_references"] = [
        {**template, "ref_id": f"source-{index}"}
        for index in range(17)
    ]

    assert _create(client, overlong_anchor).status_code == 422
    assert _create(client, overlong_identifier).status_code == 422
    assert _create(client, too_many_references).status_code == 422
    assert store.create_calls == []


@pytest.mark.parametrize(
    ("code", "status"),
    [
        ("conversation_not_found", 404),
        ("assistant_message_not_assistant", 422),
        ("assistant_message_request_mismatch", 422),
        ("request_trace_not_found", 404),
        ("request_trace_scope_mismatch", 422),
        ("evidence_reference_not_in_trace", 422),
        ("evidence_reference_not_found", 404),
        ("claim_record_conflict", 409),
    ],
)
def test_bounded_service_errors_map_to_http(client_and_store, code, status):
    client, store = client_and_store
    store.error_code = code

    response = _create(client, _body())

    assert response.status_code == status
    assert response.json() == {"detail": code}


def test_read_and_list_return_only_bounded_claim_record(client_and_store):
    client, store = client_and_store
    body = _body()
    assert _create(client, body).status_code == 200

    one = client.get(
        f"/v1/internal/claim-records/{body['calibration_result']['claim_id']}",
        params={"owner_id": body["owner_id"], "conversation_id": body["conversation_id"]},
        headers={"X-API-Key": "testkey"},
    )
    listed = client.get(
        "/v1/internal/claim-records",
        params={"owner_id": body["owner_id"], "conversation_id": body["conversation_id"]},
        headers={"X-API-Key": "testkey"},
    )

    assert one.status_code == 200
    assert listed.status_code == 200
    encoded = str({"one": one.json(), "listed": listed.json()})
    for private_key in (
        "content",
        "trace",
        "prompt",
        "snippet",
        "credential",
        "metadata",
        "reasoning",
    ):
        assert private_key not in encoded.lower()


def test_read_is_owner_and_conversation_scoped(client_and_store):
    client, _ = client_and_store
    body = _body()
    assert _create(client, body).status_code == 200
    claim_id = body["calibration_result"]["claim_id"]

    for params in (
        {"owner_id": "other", "conversation_id": body["conversation_id"]},
        {"owner_id": body["owner_id"], "conversation_id": str(uuid4())},
    ):
        response = client.get(
            f"/v1/internal/claim-records/{claim_id}",
            params=params,
            headers={"X-API-Key": "testkey"},
        )
        assert response.status_code == 404
        assert response.json() == {"detail": "claim_record_not_found"}
