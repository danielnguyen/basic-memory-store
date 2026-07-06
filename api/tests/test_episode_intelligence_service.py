from services.episode_intelligence import evaluate_episode_callback, extract_episode_decisions


def _payload(text: str, *, item_id: str = "msg-1", **extra):
    return {
        "request_id": "rid-episode",
        "owner_id": "owner",
        "scene": {"scene_id": "coding"},
        "source_items": [
            {
                "message_id": item_id,
                "role": "assistant",
                "content": text,
                **extra,
            }
        ],
    }


def test_episode_extraction_accepts_milestone_reversal_correction_failure_workflow_and_lesson():
    cases = [
        ("Wave 4A PR merged and the project milestone completed.", "project_milestone_completed"),
        ("We reversed the plan and changed strategy instead of expanding scope.", "planning_decision_reversed"),
        ("Correction for next time: use the sync preflight and avoid stale state.", "correction_future_relevance"),
        ("The failure was blocked because of stale state, then fixed with a clean mitigation.", "successful_mitigation"),
        ("From now on, the workflow is to run sync preflight every time.", "recurring_workflow_established"),
        ("Lesson learned: avoid unsupported claims because it prevents future risk.", "useful_lesson_extracted"),
    ]

    for index, (text, episode_type) in enumerate(cases):
        decisions = extract_episode_decisions(_payload(text, item_id=f"msg-{index}"))

        assert decisions[0]["decision"] == "accept"
        assert decisions[0]["episode_type"] == episode_type
        assert decisions[0]["source_refs"][0]["ref_id"] == f"msg-{index}"
        assert len(decisions[0]["summary"]) <= 320
        assert "episode_key" in decisions[0]


def test_episode_extraction_rejects_low_value_and_unsupported_candidates():
    low_value = extract_episode_decisions(_payload("thanks"))
    unsupported = extract_episode_decisions(_payload("A major incident happened", unsupported=True))

    assert low_value[0]["decision"] == "reject"
    assert "low_value_generic_chat" in low_value[0]["reasons"]
    assert unsupported[0]["decision"] == "reject"
    assert "unsupported_claim" in unsupported[0]["reasons"]


def test_episode_extraction_defers_missing_evidence_and_rejects_cross_owner_sources():
    missing = extract_episode_decisions({"request_id": "rid", "owner_id": "owner", "source_items": [{"content": "Wave completed milestone."}]})
    cross_owner = extract_episode_decisions(
        {
            "request_id": "rid",
            "owner_id": "owner-a",
            "source_items": [{"owner_id": "owner-b", "message_id": "m-1", "content": "Wave completed milestone."}],
        }
    )

    assert missing[0]["decision"] == "defer"
    assert "missing_evidence" in missing[0]["reasons"]
    assert cross_owner[0]["decision"] == "reject"
    assert "cross_owner_source" in cross_owner[0]["reasons"]


def test_episode_extraction_identity_is_deterministic_for_duplicate_input():
    payload = _payload("Wave 4A PR merged and the project milestone completed.", item_id="msg-stable")

    first = extract_episode_decisions(payload)[0]
    second = extract_episode_decisions(payload)[0]

    assert first["decision"] == "accept"
    assert first["episode_key"] == second["episode_key"]
    assert first["decision_id"] == second["decision_id"]


def test_callback_evaluator_accepts_useful_and_unresolved_context():
    out = evaluate_episode_callback(
        context={"scene_id": "coding"},
        candidate={
            "episode_id": "ep-1",
            "title": "Failure mitigated",
            "summary": "A retry mitigation resolved a repeated failure.",
            "episode_type": "successful_mitigation",
            "confidence": 0.86,
            "relevance_score": 0.78,
            "continuity_value": 0.75,
            "recency_score": 0.7,
            "scene": {"scene_id": "coding"},
            "unresolved": {"follow_up": "verify replay"},
        },
    )

    assert out["decision"] == "include"
    assert out["callback_strategy"] == "explicit_callback"
    assert "unresolved_follow_up_relevant" in out["reasons"]


def test_callback_evaluator_suppresses_or_defers_bad_callbacks():
    stale = evaluate_episode_callback(
        context={"scene_id": "coding"},
        candidate={"episode_id": "ep-stale", "confidence": 0.8, "relevance_score": 0.8, "continuity_value": 0.6, "recency_score": 0.05},
    )
    low_confidence = evaluate_episode_callback(
        context={"scene_id": "coding"},
        candidate={"episode_id": "ep-low", "confidence": 0.2, "relevance_score": 0.9, "continuity_value": 0.7},
    )
    scene_bad = evaluate_episode_callback(
        context={"scene_id": "coding"},
        candidate={"episode_id": "ep-scene", "confidence": 0.8, "relevance_score": 0.6, "continuity_value": 0.7, "scene": {"scene_id": "personal"}},
    )
    awkward = evaluate_episode_callback(
        context={"scene_id": "coding"},
        candidate={"episode_id": "ep-awkward", "confidence": 0.8, "relevance_score": 0.8, "continuity_value": 0.7, "awkwardness_score": 0.9},
    )

    assert stale["decision"] == "defer"
    assert "stale_episode" in stale["reasons"]
    assert low_confidence["decision"] == "suppress"
    assert "low_confidence" in low_confidence["reasons"]
    assert scene_bad["decision"] == "suppress"
    assert "scene_inappropriate" in scene_bad["reasons"]
    assert awkward["decision"] == "suppress"
    assert "awkward_or_tangential" in awkward["reasons"]
