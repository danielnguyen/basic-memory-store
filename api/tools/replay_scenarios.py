from __future__ import annotations

import argparse
import asyncio
from copy import deepcopy
import json
from pathlib import Path
import sys
from types import SimpleNamespace
from typing import Any
from uuid import UUID

from models import RetrievalOptions
from services.retrieval_replay import (
    load_replay_corpus,
    replay_raw_vs_augmented,
    structural_diff,
)


DEFAULT_CORPUS = Path(__file__).resolve().parents[1] / "replay" / "retrieval_scenarios.v1.json"


class FixturePG:
    def __init__(self, fixture: dict[str, Any], owner_id: str) -> None:
        self.fixture = fixture
        self.owner_id = owner_id

    async def get_message_snippets_by_ids(self, ids: list[UUID]) -> list[dict[str, Any]]:
        by_id = {item["message_id"]: item for item in self.fixture.get("message_sources", [])}
        return [deepcopy(by_id[str(item)]) for item in ids if str(item) in by_id]

    async def get_message_owner(self, message_id: UUID) -> str | None:
        wanted = str(message_id)
        for item in [
            *self.fixture.get("message_sources", []),
            *self.fixture.get("recent_messages", []),
        ]:
            if item.get("message_id") == wanted:
                return item.get("owner_id") or self.owner_id
        return None

    async def get_derived_text_snippets_by_ids(self, ids: list[UUID]) -> list[dict[str, Any]]:
        if self.fixture.get("derived_text_status") == "unavailable" and ids:
            raise RuntimeError("fixture derived text store unavailable")
        by_id = {item["derived_text_id"]: item for item in self.fixture.get("artifact_sources", [])}
        rows = [deepcopy(by_id[str(item)]) for item in ids if str(item) in by_id]
        for row in rows:
            row.setdefault("owner_id", self.owner_id)
            row.setdefault("kind", "chunk")
            params = row.setdefault("derivation_params", {})
            params.setdefault(
                "source_refs",
                [{
                    "ref_type": "artifact",
                    "ref_id": row["artifact_id"],
                    "support_kind": "direct",
                }],
            )
        return rows

    async def get_artifact(self, artifact_id: UUID) -> dict[str, Any] | None:
        wanted = str(artifact_id)
        for item in self.fixture.get("artifact_sources", []):
            if item.get("artifact_id") == wanted:
                return {
                    "artifact_id": wanted,
                    "owner_id": item.get("artifact_owner_id") or item.get("owner_id") or self.owner_id,
                }
        for item in self.fixture.get("source_records", []):
            if item.get("ref_type") == "artifact" and item.get("ref_id") == wanted:
                if item.get("availability") == "missing":
                    return None
                if item.get("availability") == "unavailable":
                    raise RuntimeError("fixture source lookup unavailable")
                return {"artifact_id": wanted, "owner_id": item.get("owner_id") or self.owner_id}
        return None

    async def get_event_ingest_log(self, event_log_id: UUID) -> dict[str, Any] | None:
        wanted = str(event_log_id)
        for item in self.fixture.get("source_records", []):
            if item.get("ref_type") == "event_log" and item.get("ref_id") == wanted:
                if item.get("availability") == "missing":
                    return None
                return {"event_log_id": wanted, "owner_id": item.get("owner_id") or self.owner_id}
        return None

    async def get_memory_debug(self, memory_id: UUID, owner_id: str) -> dict[str, Any] | None:
        wanted = str(memory_id)
        for item in self.fixture.get("source_records", []):
            if item.get("ref_type") == "memory_item" and item.get("ref_id") == wanted:
                if item.get("availability") == "missing" or item.get("owner_id", owner_id) != owner_id:
                    return None
                return {"memory": {"memory_id": wanted, "owner_id": owner_id}, "events": []}
        return None

    async def get_derived_text_for_owner(self, *, derived_text_id: UUID, owner_id: str) -> dict[str, Any] | None:
        wanted = str(derived_text_id)
        for item in self.fixture.get("artifact_sources", []):
            if item.get("derived_text_id") == wanted and (item.get("owner_id") or self.owner_id) == owner_id:
                return deepcopy(item)
        return None

    async def get_recent_message_items(
        self,
        *,
        conversation_id: UUID,
        limit: int,
    ) -> list[dict[str, Any]]:
        if self.fixture.get("canonical_status") == "unavailable":
            raise RuntimeError("fixture canonical store unavailable")
        return deepcopy(self.fixture.get("recent_messages", []))[:limit]

    async def get_memory_items_for_source_refs(
        self,
        *,
        owner_id: str,
        source_refs: list[dict[str, str]],
    ) -> dict[tuple[str, str], dict[str, Any]]:
        allowed = {(item["ref_type"], item["ref_id"]) for item in source_refs}
        out: dict[tuple[str, str], dict[str, Any]] = {}
        for item in self.fixture.get("memory_items", []):
            key = (item["ref_type"], item["ref_id"])
            if key in allowed:
                out[key] = deepcopy(item["memory"])
        return out


class FixtureQdrant:
    def __init__(self, fixture: dict[str, Any]) -> None:
        self.fixture = fixture

    async def search(self, **_: Any) -> list[SimpleNamespace]:
        if self.fixture.get("vector_status") == "unavailable":
            raise RuntimeError("fixture vector dependency unavailable")
        return [SimpleNamespace(**item) for item in self.fixture.get("semantic_hits", [])]

    async def search_artifact_chunks(self, **_: Any) -> list[SimpleNamespace]:
        if self.fixture.get("artifact_status") == "unavailable":
            raise RuntimeError("fixture artifact dependency unavailable")
        return [SimpleNamespace(**item) for item in self.fixture.get("artifact_hits", [])]


def _provenance(snapshot: dict[str, Any]) -> dict[str, Any]:
    return {
        "raw_source_refs": snapshot["raw"]["source_refs"],
        "augmented_source_refs": snapshot["augmented"]["source_refs"],
        "raw_freshness": [
            {"id": item["id"], "freshness_state": item["freshness_state"]}
            for item in [*snapshot["raw"]["recent"], *snapshot["raw"]["semantic"]]
        ],
        "augmented_freshness": [
            {"id": item["id"], "freshness_state": item["freshness_state"]}
            for item in [
                *snapshot["augmented"]["recent"],
                *snapshot["augmented"]["semantic"],
                *snapshot["augmented"]["artifacts"],
            ]
        ],
        "truth_qualification": snapshot["augmented"]["diagnostics"].get("truth_qualification", {}),
    }


def _summary_view(summary: dict[str, Any]) -> dict[str, Any]:
    debug = summary["retrieval_debug"]
    return {
        "recent_ids": summary["recent_ids"],
        "semantic_ids": summary["semantic_ids"],
        "artifact_ids": summary["artifact_ids"],
        "recent": summary["recent"],
        "semantic": summary["semantic"],
        "artifacts": summary["artifacts"],
        "source_refs": summary["source_refs"],
        "token_estimate_total": summary["token_estimate_total"],
        "diagnostics": {
            "vector_status": debug["vector_status"],
            "semantic_invalid_hit_ids": debug["semantic_invalid_hit_ids"],
            "semantic_missing_source_count": debug["semantic_missing_source_count"],
            "fallback_to_raw_reasons": debug["fallback_to_raw_reasons"],
            "artifact_status": debug["artifact_status"],
            "artifact_invalid_hit_ids": debug["artifact_invalid_hit_ids"],
            "artifact_missing_source_count": debug["artifact_missing_source_count"],
            "artifact_omission_reasons": debug["artifact_omission_reasons"],
            "truth_qualification": debug.get("truth_qualification", {}),
        },
    }


def _outcome(snapshot: dict[str, Any]) -> dict[str, Any]:
    augmented_debug = snapshot["augmented"]["retrieval_debug"]
    reasons = list(augmented_debug.get("fallback_to_raw_reasons") or [])
    reasons.extend(augmented_debug.get("artifact_omission_reasons") or [])
    reasons = list(dict.fromkeys(reasons))
    if "augmented_retrieval_failed" in reasons:
        fallback = "raw_canonical"
    elif any(
        reason in {"vector_unavailable", "malformed_vector_result", "missing_canonical_source"}
        for reason in reasons
    ):
        fallback = "raw_canonical"
    elif any(reason.startswith("artifact_") or reason == "missing_derivative_source" for reason in reasons):
        fallback = "partial_without_artifacts"
    else:
        fallback = "none"
    return {
        "status": "degraded" if reasons else "ok",
        "fallback": fallback,
        "reasons": reasons,
    }


async def run_scenario(scenario: dict[str, Any]) -> dict[str, Any]:
    request = scenario["request"]
    retrieval = scenario["retrieval"]
    fixture = scenario["fixture"]
    settings = SimpleNamespace(
        retrieval_artifact_k=retrieval.get("artifact_k", 3),
        retrieval_artifact_max_snippet_chars=500,
        retrieval_recent_half_life_days=14,
        retrieval_balanced_half_life_days=45,
        retrieval_historical_half_life_days=365,
        retrieval_conversation_boost=0.08,
        retrieval_pinned_bias=0.12,
        retrieval_missing_penalty_cap=0.15,
        recent_turns=10,
    )
    try:
        replay = await replay_raw_vs_augmented(
            pg=FixturePG(fixture, request["owner_id"]),
            qdrant=FixtureQdrant(fixture),
            settings=settings,
            owner_id=request["owner_id"],
            conversation_id=UUID(request["conversation_id"]),
            client_id=request.get("client_id"),
            query=retrieval["query"],
            opts=RetrievalOptions(**retrieval.get("options", {})),
        )
    except Exception:
        if fixture.get("canonical_status") != "unavailable":
            raise
        return {
            "raw": {
                "recent_ids": [],
                "semantic_ids": [],
                "artifact_ids": [],
                "recent": [],
                "semantic": [],
                "artifacts": [],
                "source_refs": [],
                "token_estimate_total": None,
                "diagnostics": {"status": "failed", "reason": "canonical_retrieval_failed"},
            },
            "augmented": {
                "recent_ids": [],
                "semantic_ids": [],
                "artifact_ids": [],
                "recent": [],
                "semantic": [],
                "artifacts": [],
                "source_refs": [],
                "token_estimate_total": None,
                "diagnostics": {"status": "not_run"},
            },
            "comparison": {
                "contract_version": "raw-retrieval-debug.v1",
                "status": "failed",
                "reason": "canonical_retrieval_failed",
                "shared_normalized_input": True,
                "normalization_applied": retrieval["query"].strip() != retrieval["query"],
            },
            "provenance": {
                "raw_source_refs": [],
                "augmented_source_refs": [],
                "raw_freshness": [],
                "augmented_freshness": [],
                "truth_qualification": {},
            },
            "outcome": {
                "status": "failed",
                "fallback": "none",
                "reasons": ["canonical_retrieval_failed"],
            },
            "contract": {
                "request_id": request["request_id"],
                "conversation_id": request["conversation_id"],
                "owner_id": request["owner_id"],
                "client_id": request.get("client_id"),
                "surface": request["surface"],
                "profile": request.get("profile"),
                "consumer_context": scenario.get("consumer_context", {}),
            },
        }
    replay_view = {
        **replay,
        "raw": _summary_view(replay["raw"]),
        "augmented": _summary_view(replay["augmented"]),
    }
    return {
        "raw": replay_view["raw"],
        "augmented": replay_view["augmented"],
        "comparison": replay["comparison"],
        "provenance": _provenance(replay_view),
        "outcome": _outcome(replay),
        "contract": {
            "request_id": request["request_id"],
            "conversation_id": request["conversation_id"],
            "owner_id": request["owner_id"],
            "client_id": request.get("client_id"),
            "surface": request["surface"],
            "profile": request.get("profile"),
            "consumer_context": scenario.get("consumer_context", {}),
        },
    }


async def run_corpus(path: Path) -> tuple[list[dict[str, Any]], list[str]]:
    corpus = load_replay_corpus(path)
    results: list[dict[str, Any]] = []
    failures: list[str] = []
    for scenario in corpus["scenarios"]:
        actual = await run_scenario(scenario)
        results.append({"name": scenario["name"], "snapshot": actual})
        expected = scenario.get("expected")
        if expected != actual:
            failures.append(f"{scenario['name']}:\n{structural_diff(expected, actual)}")
    return results, failures


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Replay deterministic retrieval scenarios.")
    parser.add_argument("--corpus", type=Path, default=DEFAULT_CORPUS)
    parser.add_argument(
        "--show-actual",
        action="store_true",
        help="Print normalized actual snapshots even when comparisons fail.",
    )
    parser.add_argument(
        "--update-expected",
        action="store_true",
        help="Replace expected snapshots in the selected corpus with normalized actual output.",
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        if args.update_expected:
            corpus = load_replay_corpus(args.corpus)
            for scenario in corpus["scenarios"]:
                scenario["expected"] = asyncio.run(run_scenario(scenario))
            args.corpus.write_text(
                json.dumps(corpus, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
        results, failures = asyncio.run(run_corpus(args.corpus))
    except Exception as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, sort_keys=True))
        return 2
    if args.show_actual:
        print(json.dumps({"schema_version": 1, "scenarios": results}, indent=2, sort_keys=True))
    if failures:
        print("\n\n".join(failures), file=sys.stderr)
        return 1
    print(json.dumps({"ok": True, "scenario_count": len(results)}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
