from __future__ import annotations

from dataclasses import dataclass
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


def _ids(items: list[Any], key: str) -> list[str]:
    out: list[str] = []
    for item in items:
        if isinstance(item, dict):
            value = item.get(key)
        else:
            value = getattr(item, key, None)
        if value is not None:
            out.append(str(value))
    return out


def summarize_bundle(bundle: RetrievalBundle) -> dict[str, Any]:
    return {
        "recent_ids": _ids(bundle.recent, "message_id"),
        "semantic_ids": _ids(bundle.semantic, "message_id"),
        "artifact_ids": _ids(bundle.artifact_refs, "artifact_id"),
        "semantic_count": len(bundle.semantic),
        "artifact_count": len(bundle.artifact_refs),
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
