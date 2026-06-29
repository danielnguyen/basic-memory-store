from __future__ import annotations

import difflib
import json
from pathlib import Path
from typing import Any, Awaitable, Callable
from uuid import UUID

from models import RetrievalBundle, RetrievalOptions
from services.retrieval_debug import compare_bundle_summaries, summarize_bundle
from services.retrieval import build_retrieval_bundle


BundleRunner = Callable[..., Awaitable[RetrievalBundle]]


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
    raw_bundle = await runner(
        pg=pg,
        qdrant=qdrant,
        settings=settings,
        owner_id=owner_id,
        conversation_id=conversation_id,
        client_id=client_id,
        query=query,
        opts=opts,
        include_artifacts=False,
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
