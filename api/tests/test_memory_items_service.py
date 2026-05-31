from services.derived_contract import memory_item_contract_view
from services.memory_items import normalize_source_refs, source_ref_hash


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
            "derivation_version": "r20-mvp-v1",
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
    assert out["source_refs"] == [{"ref_type": "message", "ref_id": "m-1"}]
    assert out["explanation"] == "explicit user instruction"
