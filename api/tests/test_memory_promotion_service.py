from services.memory_promotion import evaluate_promotion_candidate


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
