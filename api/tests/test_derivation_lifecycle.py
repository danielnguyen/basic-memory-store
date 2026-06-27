import uuid
from pathlib import Path

import pytest

from services.derivation_lifecycle import (
    EPISODE_RECIPE_KIND,
    MEMORY_RECIPE_KIND,
    classify_rebuildability,
    inspect_row,
    invalidate_derived,
    replay_derived,
)


NOW = "2026-01-01T00:00:00+00:00"


def _memory_row(memory_id: str, *, recipe: dict | None = None, summary: str = "Use concise answers") -> dict:
    explanation = {"rationale": "bounded fixture"}
    if recipe is not None:
        explanation["derivation_recipe"] = recipe
    return {
        "memory_id": memory_id,
        "owner_id": "owner",
        "memory_type": "preference",
        "summary": summary,
        "source_refs_json": [{"ref_type": "message", "ref_id": "msg-1", "support_kind": "direct"}],
        "source_ref_hash": "hash",
        "scores_json": {"utility": 0.8},
        "promotion_state": "promoted",
        "status": "active",
        "supersedes_memory_id": None,
        "superseded_by_memory_id": None,
        "last_reinforced_at": None,
        "expires_at": None,
        "derivation_version": "memory-promotion-v1",
        "confidence": 0.8,
        "explanation_json": explanation,
        "generation_trace_id": "rid-source",
        "created_at": NOW,
        "updated_at": NOW,
    }


def _episode_row(episode_id: str, *, recipe: dict | None = None) -> dict:
    explanation = {"rationale": "bounded fixture"}
    if recipe is not None:
        explanation["derivation_recipe"] = recipe
    return {
        "episode_id": episode_id,
        "owner_id": "owner",
        "title": "Milestone",
        "summary": "A bounded milestone happened.",
        "episode_type": "milestone",
        "trigger_json": {"kind": "manual"},
        "outcome": "completed",
        "significance": "useful",
        "unresolved_json": {},
        "source_refs_json": [{"ref_type": "message", "ref_id": "msg-1", "support_kind": "direct"}],
        "source_ref_hash": "hash",
        "episode_key": "episode-key",
        "callback_candidates_json": [],
        "time_window_json": {"start": "2026-01-01"},
        "participants_json": ["operator"],
        "status": "active",
        "derivation_version": "episode-construction-v1",
        "confidence": 0.8,
        "explanation_json": explanation,
        "generation_trace_id": "rid-source",
        "created_at": NOW,
        "updated_at": NOW,
    }


class FakePG:
    def __init__(self, tmp_path: Path):
        self.derived_text_id = str(uuid.uuid4())
        self.artifact_id = str(uuid.uuid4())
        self.memory_id = str(uuid.uuid4())
        self.episode_id = str(uuid.uuid4())
        self.suggestion_id = str(uuid.uuid4())
        self.event_id = str(uuid.uuid4())
        self.file_path = tmp_path / "source.txt"
        self.file_path.write_text("alpha beta gamma\nsecond line", encoding="utf-8")
        self.updated_text_params = []
        self.updated_suggestions = []
        self.memory_replacements = []
        self.events = {}
        self.memory_recipe_summary = "Use concise answers"
        self.derived_text_params = {
            "derivation_type": "chunk",
            "derivation_version": "file-chunk-v1",
            "chunking_algorithm_version": "fixed-overlap-text-v1",
            "chunking_algorithm": "fixed-overlap-text",
            "status": "active",
            "source_refs": [{"ref_type": "artifact", "ref_id": self.artifact_id, "support_kind": "direct"}],
            "chunk_index": 0,
            "chunk_size": 1200,
            "chunk_overlap": 120,
            "char_start": 0,
            "char_end": len("alpha beta gamma\nsecond line"),
        }

    async def get_derived_text_for_owner(self, derived_text_id, owner_id):
        if str(derived_text_id) != self.derived_text_id or owner_id != "owner":
            return None
        return {
            "derived_text_id": self.derived_text_id,
            "artifact_id": self.artifact_id,
            "owner_id": "owner",
            "kind": "chunk",
            "language": None,
            "text": "alpha beta gamma\nsecond line",
            "derivation_params": self.derived_text_params,
            "created_at": NOW,
        }

    async def update_derived_text_params(self, *, derived_text_id, owner_id, derivation_params):
        self.derived_text_params = derivation_params
        self.updated_text_params.append(derivation_params)
        row = await self.get_derived_text_for_owner(derived_text_id, owner_id)
        return {**row, "derivation_params": derivation_params}

    async def get_artifact(self, artifact_id):
        if str(artifact_id) != self.artifact_id:
            return None
        return {"artifact_id": self.artifact_id, "owner_id": "owner", "object_uri": f"file://{self.file_path}"}

    async def create_derived_text(self, **kwargs):
        return {"derived_text_id": str(uuid.uuid4()), **kwargs}

    async def get_proactive_suggestion(self, suggestion_id):
        if str(suggestion_id) != self.suggestion_id:
            return None
        if self.event_id not in self.events:
            self.events[self.event_id] = {
                "event_log_id": self.event_id,
                "owner_id": "owner",
                "source_type": "portfolio",
                "source_event_id": "portfolio-event-1",
                "event_type": "allocation_drift",
                "payload_json": {"account": "retirement", "summary": "drift", "allocation_drift_pct": 0.09},
            }
        return {
            "suggestion_id": self.suggestion_id,
            "owner_id": "owner",
            "source_event_log_id": self.event_id,
            "source_type": "portfolio",
            "kind": "portfolio_drift_review",
            "status": "pending",
            "title": "Portfolio allocation drift crossed threshold",
            "body": "Retirement allocation drifted beyond your threshold. Review the portfolio?",
            "explanation_json": {"derivation_version": "proactive-rules-v1", "rule": "portfolio_drift_review", "threshold": 0.05, "observed_drift": 0.09},
            "evidence_json": {
                "source_refs": [{"ref_type": "event_log", "ref_id": self.event_id, "support_kind": "direct"}],
                "source_event_log_id": self.event_id,
                "threshold": 0.05,
            },
            "target_surface": "telegram",
            "delivery_status": "not_attempted",
            "created_at": NOW,
            "updated_at": NOW,
        }

    async def update_proactive_suggestion_evidence(self, *, suggestion_id, owner_id, evidence_json):
        self.updated_suggestions.append(evidence_json)
        row = await self.get_proactive_suggestion(suggestion_id)
        return {**row, "evidence_json": evidence_json}

    async def get_event_ingest_log(self, event_log_id):
        return self.events.get(str(event_log_id))

    async def get_memory_debug(self, memory_id, owner_id):
        if str(memory_id) != self.memory_id or owner_id != "owner":
            return None
        recipe = {
            "kind": MEMORY_RECIPE_KIND,
            "memory_type": "preference",
            "summary": self.memory_recipe_summary,
            "source_refs": [{"ref_type": "message", "ref_id": "msg-1", "support_kind": "direct"}],
            "scores": {"utility": 0.8},
            "derivation_version": "memory-promotion-v1",
        }
        return {"memory": _memory_row(self.memory_id, recipe=recipe), "events": []}

    async def transition_memory_item(self, **kwargs):
        row = _memory_row(str(kwargs["memory_id"]), recipe=None)
        row["status"] = kwargs["new_status"]
        return {"memory": row, "changed": True, "events_appended": ["state_changed"]}

    async def promote_memory_item(self, **kwargs):
        replacement_id = str(uuid.uuid4())
        self.memory_replacements.append(kwargs)
        return {
            "memory": _memory_row(replacement_id, recipe=kwargs["explanation_json"]["derivation_recipe"], summary=kwargs["summary"]),
            "created": True,
            "updated": False,
            "reinforced": False,
            "superseded": True,
            "events_appended": ["superseded", "created", "promoted"],
        }

    async def get_episode_debug(self, episode_id, owner_id):
        if str(episode_id) != self.episode_id or owner_id != "owner":
            return None
        recipe = {
            "kind": EPISODE_RECIPE_KIND,
            "title": "Milestone",
            "summary": "A bounded milestone happened.",
            "episode_type": "milestone",
            "source_refs": [{"ref_type": "message", "ref_id": "msg-1", "support_kind": "direct"}],
            "trigger": {"kind": "manual"},
            "time_window": {"start": "2026-01-01"},
            "outcome": "completed",
            "significance": "useful",
            "participants": ["operator"],
            "derivation_version": "episode-construction-v1",
        }
        return {"episode": _episode_row(self.episode_id, recipe=recipe), "links": [], "events": []}


def test_classification_is_stable_and_truthful():
    assert classify_rebuildability("derived_text", {"artifact_id": "a", "derivation_params": {
        "derivation_version": "file-chunk-v1",
        "chunking_algorithm_version": "fixed-overlap-text-v1",
        "chunk_size": 500,
        "chunk_overlap": 50,
        "chunk_index": 0,
        "char_start": 0,
        "char_end": 10,
    }})["classification"] == "rebuildable"
    assert classify_rebuildability("derived_text", {"artifact_id": "a", "derivation_params": {"derivation_version": "file-chunk-v1"}})["classification"] == "not_rebuildable"
    assert classify_rebuildability("proactive_suggestion", {})["classification"] == "replay_only"
    assert classify_rebuildability("memory_item", _memory_row("m"))["classification"] == "not_rebuildable"
    assert classify_rebuildability("episode", _episode_row("e"))["classification"] == "not_rebuildable"


@pytest.mark.asyncio
async def test_owner_scoped_invalidation_is_bounded_and_idempotent(tmp_path):
    pg = FakePG(tmp_path)
    first = await invalidate_derived(
        pg,
        derived_class="derived_text",
        derived_id=uuid.UUID(pg.derived_text_id),
        owner_id="owner",
        request_id="rid-invalidate",
        reason_code="source_changed",
        metadata={"private_text": "x" * 500, "nested": {"raw": "drop"}},
    )
    second = await invalidate_derived(
        pg,
        derived_class="derived_text",
        derived_id=uuid.UUID(pg.derived_text_id),
        owner_id="owner",
        request_id="rid-invalidate",
        reason_code="source_changed",
        metadata={"private_text": "x" * 500},
    )

    assert first["changed"] is True
    assert second["changed"] is False
    lifecycle = pg.updated_text_params[-1]["lifecycle"]
    assert len(lifecycle["events"]) == 1
    assert lifecycle["events"][0]["metadata"]["private_text"] == "x" * 160
    assert "nested" not in lifecycle["events"][0].get("metadata", {})


@pytest.mark.asyncio
async def test_validation_replay_is_deterministic_and_does_not_mutate(tmp_path):
    pg = FakePG(tmp_path)
    first = await replay_derived(
        pg,
        derived_class="derived_text",
        derived_id=uuid.UUID(pg.derived_text_id),
        owner_id="owner",
        request_id="rid-replay",
        requested_derivation_version=None,
        persist_replacement=False,
    )
    second = await replay_derived(
        pg,
        derived_class="derived_text",
        derived_id=uuid.UUID(pg.derived_text_id),
        owner_id="owner",
        request_id="rid-replay",
        requested_derivation_version=None,
        persist_replacement=False,
    )

    assert first["replay"]["result"] == "identical"
    assert first["replay"]["candidate"]["normalized_output_hash"] == second["replay"]["candidate"]["normalized_output_hash"]
    assert pg.updated_text_params == []


@pytest.mark.asyncio
async def test_persistable_memory_rebuild_uses_recipe_and_supersedes(tmp_path):
    pg = FakePG(tmp_path)
    pg.memory_recipe_summary = "Use direct answers"
    result = await replay_derived(
        pg,
        derived_class="memory_item",
        derived_id=uuid.UUID(pg.memory_id),
        owner_id="owner",
        request_id="rid-rebuild",
        requested_derivation_version="memory-promotion-v1",
        persist_replacement=True,
    )

    assert result["replay"]["result"] == "replaced"
    assert result["replay"]["replacement_id"]
    assert pg.memory_replacements[0]["supersedes_memory_id"] == uuid.UUID(pg.memory_id)
    assert pg.memory_replacements[0]["derivation_version"] == "memory-promotion-v1"


@pytest.mark.asyncio
async def test_replay_rejects_unsupported_requested_version_before_mutation(tmp_path):
    pg = FakePG(tmp_path)
    result = await replay_derived(
        pg,
        derived_class="memory_item",
        derived_id=uuid.UUID(pg.memory_id),
        owner_id="owner",
        request_id="rid-rebuild",
        requested_derivation_version="memory-promotion-v2",
        persist_replacement=True,
    )

    assert result["replay"]["result"] == "unsupported"
    assert result["replay"]["failure_reason"] == "unsupported_derivation_version"
    assert pg.memory_replacements == []


@pytest.mark.asyncio
async def test_proactive_replay_only_does_not_create_delivery_side_effects(tmp_path):
    pg = FakePG(tmp_path)
    result = await replay_derived(
        pg,
        derived_class="proactive_suggestion",
        derived_id=uuid.UUID(pg.suggestion_id),
        owner_id="owner",
        request_id="rid-replay",
        requested_derivation_version=None,
        persist_replacement=True,
    )

    assert result["rebuildability"] == "replay_only"
    assert result["replay"]["result"] == "identical"
    assert pg.updated_suggestions
    assert (await pg.get_proactive_suggestion(uuid.UUID(pg.suggestion_id)))["delivery_status"] == "not_attempted"


@pytest.mark.asyncio
async def test_proactive_replay_is_reevaluated_from_source_event(tmp_path):
    pg = FakePG(tmp_path)
    before = await replay_derived(
        pg,
        derived_class="proactive_suggestion",
        derived_id=uuid.UUID(pg.suggestion_id),
        owner_id="owner",
        request_id="rid-replay-1",
        requested_derivation_version=None,
        persist_replacement=False,
    )
    pg.events[pg.event_id]["payload_json"]["account"] = "taxable"
    after = await replay_derived(
        pg,
        derived_class="proactive_suggestion",
        derived_id=uuid.UUID(pg.suggestion_id),
        owner_id="owner",
        request_id="rid-replay-2",
        requested_derivation_version=None,
        persist_replacement=False,
    )

    assert before["replay"]["candidate"]["normalized_output_hash"] != after["replay"]["candidate"]["normalized_output_hash"]
