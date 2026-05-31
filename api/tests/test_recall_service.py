from services.recall import select_recall_decision


def test_recall_policy_is_deterministic_and_allows_explicit_callback():
    context = {"scene_id": "debugging", "surface": "vscode", "urgency": "medium", "sensitivity": "low"}
    candidate = {
        "candidate_id": "mem-1",
        "candidate_type": "memory_item",
        "summary": "Prior passthrough issue",
        "relevance_score": 0.86,
        "salience_score": 0.70,
        "recency_score": 0.40,
        "metadata": {"explicit_callback_allowed": True},
    }

    first = select_recall_decision(context=context, candidate=candidate)
    second = select_recall_decision(context=context, candidate=candidate)

    assert first == second
    assert first["mentionability_score"] == 0.751
    assert first["decision"] == "mention"
    assert first["mention_strategy"] == "light_callback"
    assert first["prompt_eligible"] is True
    assert first["reason_json"]["rule_id"] == "light_callback_allowed"


def test_recall_policy_suppresses_below_relevance_threshold():
    out = select_recall_decision(
        context={"surface": "vscode", "sensitivity": "low"},
        candidate={"candidate_id": "mem-1", "candidate_type": "memory_item", "relevance_score": 0.34},
    )

    assert out["decision"] == "suppress"
    assert out["mention_strategy"] == "none"
    assert out["prompt_eligible"] is False
    assert out["reason_json"]["rule_id"] == "below_relevance_threshold"


def test_recall_policy_high_sensitivity_suppresses_or_caps_to_implicit():
    suppressed = select_recall_decision(
        context={"surface": "vscode", "sensitivity": "high"},
        candidate={"candidate_id": "episode-1", "candidate_type": "episode", "relevance_score": 0.95},
    )
    capped = select_recall_decision(
        context={"surface": "vscode", "sensitivity": "high"},
        candidate={
            "candidate_id": "episode-1",
            "candidate_type": "episode",
            "relevance_score": 0.95,
            "metadata": {"allow_sensitive_mention": True, "explicit_callback_allowed": True},
        },
    )

    assert suppressed["decision"] == "suppress"
    assert suppressed["prompt_eligible"] is False
    assert suppressed["reason_json"]["rule_id"] == "high_sensitivity_suppression"
    assert capped["decision"] == "implicit_only"
    assert capped["mention_strategy"] == "implicit"
    assert capped["prompt_eligible"] is False
    assert capped["reason_json"]["rule_id"] == "high_sensitivity_implicit_cap"


def test_recall_policy_explicit_callback_requires_metadata_and_nonurgent_context():
    out = select_recall_decision(
        context={"surface": "vscode", "urgency": "low", "sensitivity": "low"},
        candidate={
            "candidate_id": "episode-1",
            "candidate_type": "episode",
            "relevance_score": 0.95,
            "salience_score": 0.9,
            "recency_score": 0.8,
            "metadata": {"explicit_callback_allowed": True},
        },
    )

    assert out["mentionability_score"] == 0.915
    assert out["decision"] == "mention"
    assert out["mention_strategy"] == "explicit_callback"
    assert out["prompt_eligible"] is True
    assert out["reason_json"]["rule_id"] == "explicit_callback_allowed"
