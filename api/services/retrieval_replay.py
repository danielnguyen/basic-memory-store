from __future__ import annotations

from dataclasses import dataclass
import difflib
import json
from pathlib import Path
from typing import Any, Awaitable, Callable
from uuid import UUID

from models import RetrievalBundle, RetrievalOptions
from services.retrieval import build_retrieval_bundle


BundleRunner = Callable[..., Awaitable[RetrievalBundle]]


@dataclass(frozen=True)
class SettingsOverride:
    base: Any
    overrides: dict[str, Any]

    def __getattr__(self, name: str) -> Any:
        if name in self.overrides:
            return self.overrides[name]
        return getattr(self.base, name)


def _source_ref(item: Any) -> dict[str, str] | None:
    value = item.get("source_ref") if isinstance(item, dict) else getattr(item, "source_ref", None)
    if value is None:
        return None
    if hasattr(value, "model_dump"):
        value = value.model_dump()
    if not isinstance(value, dict):
        return None
    ref_type = value.get("ref_type")
    ref_id = value.get("ref_id")
    if ref_type is None or ref_id is None:
        return None
    return {"ref_type": str(ref_type), "ref_id": str(ref_id)}


def _safe_item_summary(item: Any, *, id_key: str, score_key: str) -> dict[str, Any]:
    value = item if isinstance(item, dict) else item.model_dump()
    summary = {
        "id": str(value[id_key]),
        "owner_id": value.get("owner_id"),
        "evidence_role": value.get("evidence_role"),
        "source_ref": _source_ref(value),
        "source_availability": value.get("source_availability"),
        "source_checks": value.get("source_checks") or [],
        "score": value.get(score_key),
        "score_details": value.get("score_details") or {},
        "freshness_state": value.get("freshness_state"),
        "durable_status": value.get("durable_status"),
        "source_kind": value.get("source_kind"),
        "confidence": value.get("confidence"),
        "supersedes": value.get("supersedes"),
        "superseded_by": value.get("superseded_by"),
        "qualification_reasons": value.get("qualification_reasons") or [],
    }
    return {key: item_value for key, item_value in summary.items() if item_value is not None}


def summarize_bundle(bundle: RetrievalBundle) -> dict[str, Any]:
    recent = [_safe_item_summary(item, id_key="message_id", score_key="score") for item in bundle.recent]
    semantic = [_safe_item_summary(item, id_key="message_id", score_key="score") for item in bundle.semantic]
    artifacts = [
        _safe_item_summary(item, id_key="artifact_id", score_key="relevance_score")
        for item in bundle.artifact_refs
    ]
    source_refs = [
        item["source_ref"]
        for item in [*recent, *semantic, *artifacts]
        if item["source_ref"] is not None
    ]
    return {
        "recent_ids": [item["id"] for item in recent],
        "semantic_ids": [item["id"] for item in semantic],
        "artifact_ids": [item["id"] for item in artifacts],
        "recent": recent,
        "semantic": semantic,
        "artifacts": artifacts,
        "semantic_count": len(bundle.semantic),
        "artifact_count": len(bundle.artifact_refs),
        "source_refs": source_refs,
        "token_estimate_total": bundle.token_estimate_total,
        "retrieval_debug": bundle.retrieval_debug,
    }


def compare_bundle_summaries(raw: dict[str, Any], augmented: dict[str, Any]) -> dict[str, Any]:
    raw_semantic = raw["semantic_ids"]
    augmented_semantic = augmented["semantic_ids"]
    return {
        "same_semantic_order": raw_semantic == augmented_semantic,
        "raw_only_semantic_ids": [item for item in raw_semantic if item not in augmented_semantic],
        "augmented_only_semantic_ids": [item for item in augmented_semantic if item not in raw_semantic],
        "artifact_delta": augmented["artifact_count"] - raw["artifact_count"],
        "token_delta": (augmented.get("token_estimate_total") or 0) - (raw.get("token_estimate_total") or 0),
    }


async def replay_raw_vs_augmented(
    *,
    pg: Any,
    qdrant: Any,
    settings: Any,
    owner_id: str,
    conversation_id: UUID,
    client_id: str | None,
    query: str,
    opts: RetrievalOptions,
    runner: BundleRunner = build_retrieval_bundle,
) -> dict[str, Any]:
    raw_settings = SettingsOverride(settings, {"retrieval_artifact_k": 0})
    raw_bundle = await runner(
        pg=pg,
        qdrant=qdrant,
        settings=raw_settings,
        owner_id=owner_id,
        conversation_id=conversation_id,
        client_id=client_id,
        query=query,
        opts=opts,
    )
    augmented_bundle = await runner(
        pg=pg,
        qdrant=qdrant,
        settings=settings,
        owner_id=owner_id,
        conversation_id=conversation_id,
        client_id=client_id,
        query=query,
        opts=opts,
    )
    raw_summary = summarize_bundle(raw_bundle)
    augmented_summary = summarize_bundle(augmented_bundle)
    opts_payload = opts.model_dump() if hasattr(opts, "model_dump") else opts.dict()
    return {
        "query": query,
        "retrieval_options": opts_payload,
        "raw": raw_summary,
        "augmented": augmented_summary,
        "comparison": compare_bundle_summaries(raw_summary, augmented_summary),
    }


def structural_diff(expected: Any, actual: Any) -> str:
    expected_text = json.dumps(expected, indent=2, sort_keys=True).splitlines()
    actual_text = json.dumps(actual, indent=2, sort_keys=True).splitlines()
    return "\n".join(
        difflib.unified_diff(
            expected_text,
            actual_text,
            fromfile="expected",
            tofile="actual",
            lineterm="",
        )
    )


def load_replay_corpus(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("schema_version") != 1:
        raise ValueError("unsupported replay corpus schema_version")
    scenarios = payload.get("scenarios")
    if not isinstance(scenarios, list) or not scenarios:
        raise ValueError("replay corpus must contain at least one scenario")
    required_scenario_keys = {"name", "categories", "request", "retrieval", "fixture", "expected"}
    required_expected_keys = {
        "raw",
        "augmented",
        "comparison",
        "provenance",
        "outcome",
        "contract",
    }
    for index, scenario in enumerate(scenarios):
        if not isinstance(scenario, dict):
            raise ValueError(f"replay scenario {index} must be an object")
        missing = required_scenario_keys - set(scenario)
        if missing:
            raise ValueError(
                f"replay scenario {index} is missing required fields: {', '.join(sorted(missing))}"
            )
        expected = scenario["expected"]
        if not isinstance(expected, dict):
            raise ValueError(f"replay scenario {index} expected snapshot must be an object")
        missing_expected = required_expected_keys - set(expected)
        if missing_expected:
            raise ValueError(
                "replay scenario "
                f"{index} expected snapshot is missing: {', '.join(sorted(missing_expected))}"
            )
    return payload
