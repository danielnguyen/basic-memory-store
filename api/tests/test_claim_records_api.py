from __future__ import annotations

import asyncio
from hashlib import sha256
import re
import types
from uuid import uuid4

import httpx
import pytest

import main as main_module


LEGACY_CLAIM_RECORD_KEYS = {
    "claim_id",
    "schema_version",
    "owner_id",
    "conversation_id",
    "request_id",
    "assistant_message_id",
    "surface",
    "runtime_session_id",
    "runtime_turn_id",
    "claim_anchor",
    "claim_anchor_digest",
    "claim_class",
    "calibration_status",
    "evidence_strength",
    "confidence",
    "strongest_authority",
    "freshness_summary",
    "uncertainty_disclosure_required",
    "validated_evidence_references",
    "limitation_codes",
    "user_safe_summary",
    "created_at",
}
V2_CLAIM_RECORD_KEYS = LEGACY_CLAIM_RECORD_KEYS | {
    "presented_to_user",
    "support",
}


class FakeQdrant:
    def ping(self):
        return True


class FakePG:
    def __init__(self):
        self.create_calls = []
        self.error_code: str | None = None
        self.records: list[dict] = []
        self.association: dict | None = None

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
        if self.association is not None:
            validate_association(record, self.association)
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


def _v2_body() -> dict:
    body = _body()
    body["schema_version"] = "claim-record.v2"
    body["presented_to_user"] = False
    result = body["calibration_result"]
    result["claim_id"] = "claim_shadow_0123456789abcdef0123456789abcdef"
    result["claim_anchor"] = "The bounded values have a mechanically derived mean."
    result["claim_anchor_digest"] = "sha256:" + sha256(
        result["claim_anchor"].encode()
    ).hexdigest()
    result["claim_class"] = "runtime_inference"
    result["calibration_status"] = "limited"
    result["evidence_strength"] = "weak"
    result["confidence"] = "unknown"
    result["strongest_authority"] = "unknown"
    result["freshness_summary"] = "unknown"
    result["uncertainty_disclosure_required"] = True
    result["validated_evidence_references"][0].update(
        {
            "ref_id": "source-neutral-1",
            "authority": "unknown",
            "support_kind": "contextual",
            "freshness_state": "unknown_freshness",
        }
    )
    result["limitation_codes"] = ["inference_dominant"]
    result["user_safe_summary"] = "The evaluated claim depends on interpreted inputs."
    body["support"] = {
        "claim_digest": result["claim_anchor_digest"],
        "supporting_evidence_ref_ids": ["source-neutral-1"],
        "counterevidence_ref_ids": [],
        "material_exclusions": [],
        "executed_derivations": [
            {
                "derivation_id": "derivation-mean-1",
                "operation": "mean",
                "canonical_inputs": ["0.5", "0.75"],
                "canonical_result": "0.625",
                "execution_digest": "sha256:" + "1" * 64,
                "executor_version": "decimal-v1",
                "supporting_evidence_ref_ids": ["source-neutral-1"],
                "input_basis": "model_interpreted",
            }
        ],
        "material_scope_limitations": ["interpretation-dependent-input"],
        "calibration_status": "limited",
        "conclusion_disposition": "qualified",
        "qualification_required": True,
        "limitation_codes": ["interpretation-dependent-derivation"],
    }
    return body


def _v2_association(body: dict) -> dict:
    assistant_content = (
        body["calibration_result"]["claim_anchor"]
        if body["presented_to_user"]
        else "The visible legacy answer remains unchanged."
    )
    return {
        "existing": None,
        "conversation": {"owner_id": body["owner_id"]},
        "assistant_message": {
            "owner_id": body["owner_id"],
            "conversation_id": body["conversation_id"],
            "role": "assistant",
            "metadata": {"request_id": body["request_id"]},
            "content": assistant_content,
        },
        "trace": {
            "owner_id": body["owner_id"],
            "conversation_id": body["conversation_id"],
            "surface": body["surface"],
            "status": "ok",
            "references": [
                {"ref_type": "external_source", "ref_id": "source-neutral-1"}
            ],
            "prompt": {
                "general_evidence_reasoning": {
                    "claim_digest": body["support"]["claim_digest"],
                    "runtime_session_id": body["runtime_session_id"],
                    "runtime_turn_id": body["runtime_turn_id"],
                    "presented_to_user": body["presented_to_user"],
                }
            },
        },
        "local_references": {},
    }


def _bind_visible_claim(association: dict, visible_content: str) -> None:
    association["assistant_message"]["content"] = visible_content
    first_paragraph = re.split(r"\r?\n[ \t]*\r?\n", visible_content, maxsplit=1)[0]
    normalized = " ".join(first_paragraph.split())
    reasoning = association["trace"]["prompt"]["general_evidence_reasoning"]
    reasoning["presentation"] = {
        "enabled": True,
        "status": "presented",
        "visible_claim_digest": "sha256:"
        + sha256(normalized.encode("utf-8")).hexdigest(),
    }


def _manifest_association(body: dict, assistant_content: str) -> dict:
    manifest_id = body["acquisition_manifest_id"]
    return {
        "existing": None,
        "conversation": {"owner_id": body["owner_id"]},
        "assistant_message": {
            "owner_id": body["owner_id"],
            "conversation_id": body["conversation_id"],
            "role": "assistant",
            "metadata": {"request_id": body["request_id"]},
            "content": assistant_content,
        },
        "trace": {
            "owner_id": body["owner_id"],
            "conversation_id": body["conversation_id"],
            "surface": body["surface"],
            "status": "ok",
            "references": [
                {
                    "ref_type": "external_source",
                    "ref_id": "source-manufacturer-1",
                }
            ],
            "prompt": {
                "evidence_acquisition": {
                    "attempted": True,
                    "status": "sufficient_for_declared_scope",
                    "manifest_id": manifest_id,
                    "assistant_message_id": body["assistant_message_id"],
                    "response_digest": (
                        "sha256:"
                        + sha256(assistant_content.encode("utf-8")).hexdigest()
                    ),
                    "plan": {"plan_status": "ready"},
                    "sufficiency": {
                        "status": "sufficient_for_declared_scope",
                    },
                }
            },
        },
        "local_references": {},
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
    assert set(response.json()["record"]) == LEGACY_CLAIM_RECORD_KEYS
    assert store.create_calls[0]["acquisition_manifest_id"] is None
    assert store.create_calls[0]["user_safe_summary"] == body["calibration_result"]["user_safe_summary"]


def test_v2_shadow_support_record_round_trips_bounded_authority_skeleton(
    client_and_store,
):
    client, store = client_and_store
    body = _v2_body()
    store.association = _v2_association(body)

    response = _create(client, body)

    assert response.status_code == 200, response.text
    record = response.json()["record"]
    assert set(record) == V2_CLAIM_RECORD_KEYS
    assert record["presented_to_user"] is False
    assert record["support"] == body["support"]
    assert record["support"]["executed_derivations"][0]["input_basis"] == "model_interpreted"
    assert "The visible legacy answer" not in str(record)


def test_v2_presented_support_record_requires_visible_claim_association(
    client_and_store,
):
    client, store = client_and_store
    body = _v2_body()
    body["presented_to_user"] = True
    store.association = _v2_association(body)

    response = _create(client, body)

    assert response.status_code == 200, response.text
    record = response.json()["record"]
    assert record["presented_to_user"] is True
    assert record["claim_anchor"] == body["calibration_result"]["claim_anchor"]
    assert record["support"] == body["support"]


def test_v2_presented_support_record_accepts_trace_bound_visible_claim(
    client_and_store,
):
    client, store = client_and_store
    body = _v2_body()
    body["presented_to_user"] = True
    store.association = _v2_association(body)
    visible_content = (
        "The bounded mean is 0.4635.\n\n"
        "Some source values had to be interpreted."
    )
    _bind_visible_claim(store.association, visible_content)

    response = _create(client, body)

    assert response.status_code == 200, response.text
    record = response.json()["record"]
    assert record["claim_anchor"] == body["calibration_result"]["claim_anchor"]
    assert record["claim_anchor_digest"] == body["support"]["claim_digest"]
    assert visible_content not in str(record)
    assert "visible_claim_digest" not in str(record)


@pytest.mark.parametrize(
    "mutation",
    [
        lambda presentation: presentation.pop("visible_claim_digest"),
        lambda presentation: presentation.update(visible_claim_digest="sha256:bad"),
        lambda presentation: presentation.update(
            visible_claim_digest="sha256:" + "f" * 64
        ),
        lambda presentation: presentation.update(enabled=False),
        lambda presentation: presentation.update(status="not_presented"),
    ],
)
def test_v2_presented_visible_claim_rejects_invalid_trace_binding(
    client_and_store,
    mutation,
):
    client, store = client_and_store
    body = _v2_body()
    body["presented_to_user"] = True
    store.association = _v2_association(body)
    _bind_visible_claim(store.association, "The bounded mean is 0.4635.")
    presentation = store.association["trace"]["prompt"][
        "general_evidence_reasoning"
    ]["presentation"]
    mutation(presentation)

    response = _create(client, body)

    assert response.status_code == 422
    assert response.json()["detail"] == "shadow_claim_not_in_trace"
    assert "0.4635" not in response.text


def test_v2_visible_claim_digest_tracks_only_normalized_first_paragraph(
    client_and_store,
):
    client, store = client_and_store
    body = _v2_body()
    body["presented_to_user"] = True
    store.association = _v2_association(body)
    _bind_visible_claim(
        store.association,
        "The bounded   mean\nis 0.4635.\r\n \t\r\nPRIVATE QUALIFICATION A",
    )
    store.association["assistant_message"]["content"] = (
        "The bounded mean is 0.4635.\n\nPRIVATE QUALIFICATION B"
    )

    response = _create(client, body)

    assert response.status_code == 200, response.text
    serialized = response.text
    assert "PRIVATE QUALIFICATION" not in serialized

    store.association["assistant_message"]["content"] = (
        "The bounded mean is 0.4636.\n\nPRIVATE QUALIFICATION B"
    )
    changed = _create(client, body)
    assert changed.status_code == 422
    assert changed.json()["detail"] == "shadow_claim_not_in_trace"


@pytest.mark.parametrize(
    ("mutation", "expected_fragment"),
    [
        (
            lambda result: result.update(confidence="medium"),
            "v2_compatibility_projection_not_neutral",
        ),
        (
            lambda result: result.update(strongest_authority="trusted_integration"),
            "v2_compatibility_projection_not_neutral",
        ),
        (
            lambda result: result["validated_evidence_references"][0].update(
                support_kind="direct"
            ),
            "v2_evidence_projection_not_neutral",
        ),
        (
            lambda result: result["validated_evidence_references"][0].update(
                authority="trusted_integration"
            ),
            "v2_evidence_projection_not_neutral",
        ),
    ],
)
def test_v2_rejects_authority_escalation_through_legacy_projection(
    client_and_store,
    mutation,
    expected_fragment,
):
    client, _ = client_and_store
    body = _v2_body()
    mutation(body["calibration_result"])

    response = _create(client, body)

    assert response.status_code == 422
    assert expected_fragment in response.text


@pytest.mark.parametrize(
    ("mutation", "expected_fragment"),
    [
        (lambda body: body.pop("support"), "v2_support_required"),
        (
            lambda body: body["support"].update(
                claim_digest="sha256:" + "2" * 64
            ),
            "support_claim_digest_mismatch",
        ),
        (
            lambda body: body["support"].update(
                supporting_evidence_ref_ids=["source-not-in-record"]
            ),
            "derivation_evidence_reference_unknown",
        ),
        (
            lambda body: body["support"].update(raw_source_body="PRIVATE SOURCE"),
            "extra_forbidden",
        ),
        (
            lambda body: body["support"].update(provider_scratchpad="PRIVATE THOUGHT"),
            "extra_forbidden",
        ),
    ],
)
def test_v2_contract_rejects_false_association_and_unbounded_metadata(
    client_and_store,
    mutation,
    expected_fragment,
):
    client, _ = client_and_store
    body = _v2_body()
    mutation(body)

    response = _create(client, body)

    assert response.status_code == 422
    assert expected_fragment in response.text


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("claim_digest", "sha256:" + "3" * 64),
        ("runtime_session_id", "session-other"),
        ("runtime_turn_id", "turn-other"),
        ("presented_to_user", True),
    ],
)
def test_v2_shadow_record_requires_exact_trace_association(
    client_and_store,
    field,
    value,
):
    client, store = client_and_store
    body = _v2_body()
    store.association = _v2_association(body)
    store.association["trace"]["prompt"]["general_evidence_reasoning"][field] = value

    response = _create(client, body)

    assert response.status_code == 422
    assert response.json()["detail"] == "shadow_claim_not_in_trace"


@pytest.mark.parametrize(
    ("presented_to_user", "assistant_content", "trace_presented_to_user"),
    [
        (True, "A different visible claim.", True),
        (True, "The bounded values have a mechanically derived mean.", False),
        (False, "The visible legacy answer remains unchanged.", True),
    ],
)
def test_v2_presentation_state_and_visible_claim_must_match_trace_and_message(
    client_and_store,
    presented_to_user,
    assistant_content,
    trace_presented_to_user,
):
    client, store = client_and_store
    body = _v2_body()
    body["presented_to_user"] = presented_to_user
    store.association = _v2_association(body)
    store.association["assistant_message"]["content"] = assistant_content
    reasoning = store.association["trace"]["prompt"]["general_evidence_reasoning"]
    reasoning["presented_to_user"] = trace_presented_to_user

    response = _create(client, body)

    assert response.status_code == 422
    assert response.json()["detail"] == "shadow_claim_not_in_trace"


def test_v1_rejects_v2_fields_without_changing_legacy_output(client_and_store):
    client, _ = client_and_store
    body = _body()
    body["presented_to_user"] = False

    response = _create(client, body)

    assert response.status_code == 422
    assert "v1_support_fields_forbidden" in response.text


def test_optional_acquisition_manifest_id_round_trips(client_and_store):
    client, store = client_and_store
    body = _body()
    body["acquisition_manifest_id"] = "evidence_manifest_0123456789abcdef0123456789abcdef"

    created = _create(client, body)
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

    assert created.status_code == 200
    assert one.status_code == 200
    assert listed.status_code == 200
    assert created.json()["record"]["acquisition_manifest_id"] == body["acquisition_manifest_id"]
    assert one.json()["acquisition_manifest_id"] == body["acquisition_manifest_id"]
    assert listed.json()["records"][0]["acquisition_manifest_id"] == body["acquisition_manifest_id"]
    assert store.create_calls[0]["acquisition_manifest_id"] == body["acquisition_manifest_id"]


def test_create_accepts_bounded_response_manifest_without_exposing_response(
    client_and_store,
):
    client, store = client_and_store
    body = _body()
    body["acquisition_manifest_id"] = (
        "evidence_manifest_0123456789abcdef0123456789abcdef"
    )
    claim_anchor = body["calibration_result"]["claim_anchor"]
    assistant_content = (
        f"{claim_anchor}\n\n"
        "This reflects only the targeted sources checked, not a complete search "
        "of every possible source."
    )
    store.association = _manifest_association(body, assistant_content)

    response = _create(client, body)

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["record"]["claim_anchor"] == claim_anchor
    assert payload["record"]["acquisition_manifest_id"] == (
        body["acquisition_manifest_id"]
    )
    encoded = str(payload)
    assert assistant_content not in encoded
    assert "response_digest" not in encoded
    assert set(payload["record"]) == (
        LEGACY_CLAIM_RECORD_KEYS | {"acquisition_manifest_id"}
    )


@pytest.mark.parametrize(
    "manifest_id",
    [
        "",
        "evidence manifest",
        "https://manifest.invalid",
        "manifest?secret=value",
        "x" * 121,
    ],
)
def test_acquisition_manifest_id_is_strictly_bounded(
    client_and_store,
    manifest_id,
):
    client, store = client_and_store
    body = _body()
    body["acquisition_manifest_id"] = manifest_id

    response = _create(client, body)

    assert response.status_code == 422
    assert store.create_calls == []


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
        ("acquisition_manifest_not_in_trace", 422),
        ("acquisition_manifest_association_mismatch", 422),
        ("acquisition_manifest_not_eligible", 422),
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
    assert set(one.json()) == LEGACY_CLAIM_RECORD_KEYS
    assert set(listed.json()["records"][0]) == LEGACY_CLAIM_RECORD_KEYS
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
