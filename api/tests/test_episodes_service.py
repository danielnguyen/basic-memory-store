from services.derived_contract import episode_contract_view
from services.episodes import episode_key, normalize_json_map, normalize_source_refs, source_ref_hash


def test_episode_source_ref_hash_is_deterministic_for_input_order():
    refs_a = [
        {"ref_type": "message", "ref_id": "m-2", "support_kind": "direct"},
        {"ref_type": "event_log", "ref_id": "e-1", "support_kind": "supporting"},
    ]
    refs_b = list(reversed(refs_a))

    assert normalize_source_refs(refs_a) == normalize_source_refs(refs_b)
    assert source_ref_hash(refs_a) == source_ref_hash(refs_b)


def test_episode_key_is_stable_for_descriptive_field_changes():
    refs = [{"ref_type": "message", "ref_id": "m-1", "support_kind": "direct"}]
    source_hash = source_ref_hash(refs)
    first = episode_key(
        episode_type="milestone",
        source_ref_hash_value=source_hash,
        trigger_json={"kind": "manual"},
        time_window_json={"start": "2026-01-01", "end": "2026-01-02"},
    )
    second = episode_key(
        episode_type="milestone",
        source_ref_hash_value=source_hash,
        trigger_json={"kind": "manual"},
        time_window_json={"end": "2026-01-02", "start": "2026-01-01"},
    )

    assert first == second


def test_episode_key_changes_for_structural_identity_changes():
    refs = [{"ref_type": "message", "ref_id": "m-1", "support_kind": "direct"}]
    source_hash = source_ref_hash(refs)
    baseline = episode_key(
        episode_type="milestone",
        source_ref_hash_value=source_hash,
        trigger_json={"kind": "manual"},
        time_window_json={"start": "2026-01-01", "end": "2026-01-02"},
    )
    changed_type = episode_key(
        episode_type="reversal",
        source_ref_hash_value=source_hash,
        trigger_json={"kind": "manual"},
        time_window_json={"start": "2026-01-01", "end": "2026-01-02"},
    )
    changed_trigger = episode_key(
        episode_type="milestone",
        source_ref_hash_value=source_hash,
        trigger_json={"kind": "operator"},
        time_window_json={"start": "2026-01-01", "end": "2026-01-02"},
    )

    assert baseline != changed_type
    assert baseline != changed_trigger


def test_episode_contract_view_exposes_r37_fields():
    out = episode_contract_view(
        {
            "episode_id": "ep-1",
            "owner_id": "owner",
            "episode_type": "milestone",
            "source_refs_json": [{"ref_type": "message", "ref_id": "m-1"}],
            "derivation_version": "r21-m0-v1",
            "status": "active",
            "confidence": 0.9,
            "explanation_json": {"rationale": "manual incident capture"},
            "generation_trace_id": "rid-1",
            "created_at": "2026-01-01T00:00:00+00:00",
        }
    )

    assert out["derived_id"] == "ep-1"
    assert out["owner_id"] == "owner"
    assert out["derivation_type"] == "milestone"
    assert out["source_refs"] == [
        {"ref_type": "message", "ref_id": "m-1", "support_kind": "direct"}
    ]
    assert out["explanation"] == "manual incident capture"


def test_normalize_json_map_is_deterministic():
    assert normalize_json_map({"b": 2, "a": {"d": 4, "c": 3}}) == {"a": {"c": 3, "d": 4}, "b": 2}
