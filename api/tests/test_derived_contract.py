import pytest

from services.derived_contract import derived_text_contract_view, proactive_suggestion_contract_view


def test_derived_text_contract_view_uses_existing_artifact_provenance():
    out = derived_text_contract_view(
        {
            "derived_text_id": "dt-1",
            "artifact_id": "a-1",
            "owner_id": "owner",
            "kind": "chunk",
            "derivation_params": {"derivation_version": "chunk-v1"},
            "created_at": "2026-01-01T00:00:00+00:00",
        }
    )

    assert out["derived_id"] == "dt-1"
    assert out["derivation_type"] == "chunk"
    assert out["source_refs"] == [{"ref_type": "artifact", "ref_id": "a-1", "support_kind": "direct"}]
    assert out["derivation_version"] == "chunk-v1"
    assert out["status"] == "active"
    assert out["generation_trace_id"] is None
    assert out["compatibility_defaults"] == ["status"]


def test_proactive_suggestion_contract_view_uses_event_source_ref():
    out = proactive_suggestion_contract_view(
        {
            "suggestion_id": "s-1",
            "owner_id": "owner",
            "source_event_log_id": "event-1",
            "kind": "nudge",
            "status": "pending",
            "evidence_json": {},
            "explanation_json": {"rationale": "calendar event needs follow up"},
            "created_at": "2026-01-01T00:00:00+00:00",
        }
    )

    assert out["derived_id"] == "s-1"
    assert out["owner_id"] == "owner"
    assert out["source_refs"] == [{"ref_type": "event_log", "ref_id": "event-1", "support_kind": "direct"}]
    assert out["explanation"] == "calendar event needs follow up"


def test_derived_text_contract_view_rejects_missing_identifier():
    with pytest.raises(ValueError, match="missing an identifier"):
        derived_text_contract_view({"artifact_id": "a-1", "kind": "chunk"})


@pytest.mark.parametrize(
    "source_refs",
    [
        [],
        [{"ref_type": "", "ref_id": "a-1"}],
        [{"ref_type": "artifact", "ref_id": ""}],
        [{"ref_type": "artifact", "ref_id": "a-1", "metadata": {"nested": {"text": "private"}}}],
    ],
)
def test_derived_text_contract_view_rejects_or_drops_malformed_provenance(source_refs):
    row = {
        "derived_text_id": "dt-1",
        "artifact_id": None,
        "owner_id": "owner",
        "kind": "chunk",
        "created_at": "2026-01-01T00:00:00+00:00",
        "derivation_params": {"source_refs": source_refs},
    }
    if source_refs and source_refs[0].get("metadata"):
        assert derived_text_contract_view(row)["source_refs"][0].get("metadata") is None
    else:
        with pytest.raises(ValueError, match="source_ref"):
            derived_text_contract_view(row)


def test_contract_view_rejects_missing_owner():
    with pytest.raises(ValueError, match="owner_id"):
        derived_text_contract_view(
            {
                "derived_text_id": "dt-1",
                "artifact_id": "a-1",
                "kind": "chunk",
                "created_at": "2026-01-01T00:00:00+00:00",
            }
        )
