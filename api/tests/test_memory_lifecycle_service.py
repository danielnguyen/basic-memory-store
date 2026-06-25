import pytest

from services.memory_lifecycle import bounded_transition_reason, effective_freshness_state


@pytest.mark.parametrize(
    ("memory_item", "expected"),
    [
        ({"status": "active", "promotion_state": "promoted"}, "active"),
        ({"status": "parked", "promotion_state": "promoted"}, "parked"),
        ({"status": "stale", "promotion_state": "promoted"}, "stale"),
        ({"status": "contradicted", "promotion_state": "promoted"}, "forgotten_or_demoted"),
        (
            {"status": "contradicted", "promotion_state": "promoted", "supersedes_memory_id": "replacement"},
            "corrected",
        ),
        (
            {"status": "contradicted", "promotion_state": "promoted", "superseded_by_memory_id": "replacement"},
            "corrected",
        ),
        ({"status": "corrected", "promotion_state": "promoted"}, "corrected"),
        ({"status": "invalidated", "promotion_state": "promoted"}, "forgotten_or_demoted"),
        ({"status": "superseded", "promotion_state": "promoted"}, "superseded"),
        ({"status": "expired", "promotion_state": "promoted"}, "stale"),
        ({"status": "retracted", "promotion_state": "promoted"}, "forgotten_or_demoted"),
        ({"status": "forgotten_or_demoted", "promotion_state": "promoted"}, "forgotten_or_demoted"),
        ({"status": "active", "promotion_state": "decayed"}, "forgotten_or_demoted"),
        ({"status": "rebuilding", "promotion_state": "promoted"}, "unknown_freshness"),
        ({"status": "legacy_surprise", "promotion_state": "promoted"}, "unknown_freshness"),
        ({"status": None, "promotion_state": "promoted"}, "unknown_freshness"),
        (None, "unknown_freshness"),
    ],
)
def test_effective_freshness_mapping(memory_item, expected):
    assert effective_freshness_state(memory_item) == expected


def test_transition_reason_is_bounded_and_drops_nested_private_content():
    reason = bounded_transition_reason(
        code="reviewed",
        metadata={
            "note": "x" * 500,
            "count": 2,
            "private": {"memory_text": "must not leak"},
            **{f"extra_{index}": index for index in range(10)},
        },
        request_id="rid-1",
        previous_status="active",
        new_status="stale",
        related_memory_id=None,
    )

    assert len(reason["reason_metadata"]) <= 8
    assert reason["reason_metadata"]["note"] == "x" * 160
    assert "private" not in reason["reason_metadata"]
