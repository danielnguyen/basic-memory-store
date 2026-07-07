from services.derived_contract import memory_item_contract_view
from services.derivation_versions import MEMORY_ITEM_DERIVATION_VERSION
from services.memory_items import normalize_source_refs, source_ref_hash
from services.memory_promotion import evaluate_promotion_candidate


def test_source_ref_hash_is_deterministic_for_input_order():
    refs_a = [
        {"ref_type": "message", "ref_id": "m-2", "support_kind": "direct"},
        {"ref_type": "event_log", "ref_id": "e-1", "support_kind": "supporting"},
    ]
    refs_b = list(reversed(refs_a))

    assert normalize_source_refs(refs_a) == normalize_source_refs(refs_b)
    assert source_ref_hash(refs_a) == source_ref_hash(refs_b)


def test_source_ref_hash_includes_metadata_when_present():
    base = [{"ref_type": "message", "ref_id": "m-1", "support_kind": "direct"}]
    with_metadata = [
        {
            "ref_type": "message",
            "ref_id": "m-1",
            "support_kind": "direct",
            "metadata": {"field": "summary"},
        }
    ]

    assert source_ref_hash(base) != source_ref_hash(with_metadata)


def test_memory_item_contract_view_exposes_r37_fields():
    out = memory_item_contract_view(
        {
            "memory_id": "mem-1",
            "owner_id": "owner",
            "memory_type": "core",
            "source_refs_json": [{"ref_type": "message", "ref_id": "m-1"}],
            "derivation_version": MEMORY_ITEM_DERIVATION_VERSION,
            "status": "active",
            "confidence": 0.9,
            "explanation_json": {"rationale": "explicit user instruction"},
            "generation_trace_id": "rid-1",
            "created_at": "2026-01-01T00:00:00+00:00",
        }
    )

    assert out["derived_id"] == "mem-1"
    assert out["owner_id"] == "owner"
    assert out["derivation_type"] == "core"
    assert out["source_refs"] == [
        {"ref_type": "message", "ref_id": "m-1", "support_kind": "direct"}
    ]
    assert out["explanation"] == "explicit user instruction"


def test_memory_item_contract_view_uses_neutral_compatibility_default():
    out = memory_item_contract_view(
        {
            "memory_id": "mem-1",
            "owner_id": "owner",
            "memory_type": "core",
            "source_refs_json": [{"ref_type": "message", "ref_id": "m-1"}],
            "derivation_version": None,
            "status": "active",
            "confidence": None,
            "explanation_json": {},
            "generation_trace_id": None,
            "created_at": "2026-01-01T00:00:00+00:00",
        }
    )

    assert out["derivation_version"] == MEMORY_ITEM_DERIVATION_VERSION
    assert out["compatibility_defaults"] == ["derivation_version"]


def test_recurring_operational_preference_promotes_to_core():
    out = evaluate_promotion_candidate(
        {
            "summary": "I prefer concise operational answers for this project",
            "occurrence_count": 3,
            "project_id": "ccp",
            "source_refs": [{"ref_type": "message", "ref_id": "m-1"}],
        }
    )

    assert out["decision"] == "promote"
    assert out["target_memory_type"] == "core"
    assert out["factor_scores"]["recurrence"] == 1.0
    assert out["factor_scores"]["project_relevance"] >= 0.8
    assert out["factor_scores"]["future_usefulness"] > 0


def test_explicit_remember_promotes_even_with_single_evidence_item():
    out = evaluate_promotion_candidate(
        {
            "summary": "Please remember this deployment preference",
            "source_refs": [{"ref_type": "message", "ref_id": "m-2"}],
        }
    )

    assert out["decision"] == "promote"
    assert out["factor_scores"]["explicit_user_instruction"] == 1.0


def test_corrected_fact_updates_or_replaces_existing_memory():
    out = evaluate_promotion_candidate(
        {
            "summary": "Correction: use the new staging URL instead",
            "supersedes_memory_id": "mem-old",
            "source_refs": [{"ref_type": "message", "ref_id": "m-3"}],
        }
    )

    assert out["decision"] == "update"
    assert out["factor_scores"]["correction_significance"] >= 0.75


def test_trivial_one_off_suppresses_or_defers():
    out = evaluate_promotion_candidate({"summary": "nice", "source_refs": [{"ref_type": "message", "ref_id": "m-4"}]})

    assert out["decision"] == "suppress"
    assert "trivial" in out["suppression_reasons"]


def test_procedural_workflow_gets_procedural_signal():
    out = evaluate_promotion_candidate(
        {
            "summary": "Workflow: when tests fail, run the focused pytest file first",
            "source_refs": [{"ref_type": "message", "ref_id": "m-5"}],
        }
    )

    assert out["decision"] == "promote"
    assert out["target_memory_type"] == "procedural"
    assert out["factor_scores"]["procedural_value"] >= 0.6


def test_unsupported_candidate_does_not_promote():
    out = evaluate_promotion_candidate({"summary": "Unsupported claim should not become truth", "unsupported": True})

    assert out["decision"] == "suppress"
    assert "unsupported" in out["suppression_reasons"]
