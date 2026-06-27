from __future__ import annotations

import asyncio
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any
from uuid import UUID, uuid4

from services.derivation_lifecycle import replay_derived
from services.proactive import PROACTIVE_DERIVATION_VERSION, build_portfolio_suggestion_candidate


DEFAULT_CORPUS = Path(__file__).resolve().parents[1] / "replay" / "derivation_scenarios.v1.json"
REQUIRED_SCENARIOS = {
    "derived_text_identical_replay",
    "derived_text_changed_source",
    "derived_text_missing_source",
    "proactive_deterministic_reevaluation",
    "proactive_missing_source_event",
    "memory_item_current_subtype_unsupported",
    "memory_item_unsupported_persist_terminal",
    "episode_current_subtype_unsupported",
    "explicitly_unsupported_subtype",
    "cross_owner_rejection",
    "injected_persistence_failure",
    "repeated_request_idempotency",
}
DERIVED_CLASSES = {"derived_text", "proactive_suggestion", "memory_item", "episode"}
RESULTS = {"identical", "replaced", "unsupported", "failed", "not_found"}


def _iso() -> str:
    return "2026-01-01T00:00:00+00:00"


class ScenarioStore:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.owner_id = "owner"
        self.artifact_id = str(uuid4())
        self.derived_text_id = str(uuid4())
        self.memory_id = str(uuid4())
        self.episode_id = str(uuid4())
        self.event_id = str(uuid4())
        self.suggestion_id = str(uuid4())
        self.created_derived_text: list[dict[str, Any]] = []
        self.memory_replacements: list[dict[str, Any]] = []
        self.episode_replacements: list[dict[str, Any]] = []
        self.fail_next_derived_text_create = False
        self.source_path = self.root / "source.txt"
        self.source_path.write_text("alpha beta gamma\nsecond line", encoding="utf-8")
        self.derived_text_params = {
            "derivation_type": "chunk",
            "derivation_version": "file-chunk-v1",
            "chunking_algorithm": "fixed-overlap-text",
            "chunking_algorithm_version": "fixed-overlap-text-v1",
            "chunk_size": 1200,
            "chunk_overlap": 120,
            "status": "active",
            "source_refs": [{"ref_type": "artifact", "ref_id": self.artifact_id, "support_kind": "direct"}],
            "chunk_index": 0,
            "char_start": 0,
            "char_end": len("alpha beta gamma\nsecond line"),
        }
        self.event_log = {
            "event_log_id": self.event_id,
            "owner_id": self.owner_id,
            "source_type": "portfolio",
            "source_event_id": "portfolio-event-1",
            "event_type": "allocation_drift",
            "payload_json": {"account": "retirement", "symbol": "ETF", "summary": "Drift", "allocation_drift_pct": 0.09},
        }
        proactive = build_portfolio_suggestion_candidate(
            owner_id=self.owner_id,
            event_log=self.event_log,
            threshold=0.05,
            delivery_surface="telegram",
            generation_trace_id="scenario-seed",
        )
        self.suggestion = {
            "suggestion_id": self.suggestion_id,
            "owner_id": self.owner_id,
            "source_event_log_id": self.event_id,
            "source_type": proactive["source_type"],
            "kind": proactive["kind"],
            "status": "pending",
            "title": proactive["title"],
            "body": proactive["body"],
            "explanation_json": proactive["explanation_json"],
            "evidence_json": proactive["evidence_json"],
            "target_surface": proactive["target_surface"],
            "delivery_status": "not_attempted",
            "created_at": _iso(),
            "updated_at": _iso(),
        }
        self.memory = self._memory_row(self.memory_id, "Use concise answers")
        self.episode = self._episode_row(self.episode_id, "Milestone")

    def _memory_row(self, memory_id: str, summary: str) -> dict[str, Any]:
        return {
            "memory_id": memory_id,
            "owner_id": self.owner_id,
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
            "explanation_json": {"rationale": "caller authored fixture"},
            "generation_trace_id": "seed",
            "created_at": _iso(),
            "updated_at": _iso(),
        }

    def _episode_row(self, episode_id: str, title: str) -> dict[str, Any]:
        return {
            "episode_id": episode_id,
            "owner_id": self.owner_id,
            "title": title,
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
            "explanation_json": {"rationale": "caller authored fixture"},
            "generation_trace_id": "seed",
            "created_at": _iso(),
            "updated_at": _iso(),
        }

    async def get_artifact(self, artifact_id: UUID) -> dict[str, Any] | None:
        if str(artifact_id) != self.artifact_id:
            return None
        return {"artifact_id": self.artifact_id, "owner_id": self.owner_id, "object_uri": f"file://{self.source_path}"}

    async def get_derived_text_for_owner(self, derived_text_id: UUID, owner_id: str) -> dict[str, Any] | None:
        if str(derived_text_id) != self.derived_text_id or owner_id != self.owner_id:
            return None
        return {
            "derived_text_id": self.derived_text_id,
            "artifact_id": self.artifact_id,
            "owner_id": self.owner_id,
            "kind": "chunk",
            "language": None,
            "text": "alpha beta gamma\nsecond line",
            "derivation_params": self.derived_text_params,
            "created_at": _iso(),
        }

    async def update_derived_text_params(self, *, derived_text_id: UUID, owner_id: str, derivation_params: dict[str, Any]) -> dict[str, Any] | None:
        self.derived_text_params = derivation_params
        return await self.get_derived_text_for_owner(derived_text_id, owner_id)

    async def append_derived_text_lifecycle_event(self, *, derived_text_id: UUID, owner_id: str, event: dict[str, Any]) -> dict[str, Any] | None:
        from services.derivation_lifecycle import append_lifecycle_event

        updated, _ = append_lifecycle_event(self.derived_text_params, event)
        self.derived_text_params = updated
        return await self.get_derived_text_for_owner(derived_text_id, owner_id)

    async def create_derived_text(self, **kwargs: Any) -> dict[str, Any]:
        if self.fail_next_derived_text_create:
            self.fail_next_derived_text_create = False
            raise RuntimeError("injected_persistence_failure")
        row = {"derived_text_id": str(uuid4()), **kwargs, "created_at": _iso()}
        self.created_derived_text.append(row)
        return row

    async def get_event_ingest_log(self, event_log_id: UUID) -> dict[str, Any] | None:
        return self.event_log if str(event_log_id) == self.event_id else None

    async def get_proactive_suggestion(self, suggestion_id: UUID) -> dict[str, Any] | None:
        return self.suggestion if str(suggestion_id) == self.suggestion_id else None

    async def update_proactive_suggestion_evidence(self, *, suggestion_id: UUID, owner_id: str, evidence_json: dict[str, Any]) -> dict[str, Any] | None:
        self.suggestion = {**self.suggestion, "evidence_json": evidence_json}
        return self.suggestion

    async def get_memory_debug(self, memory_id: UUID, owner_id: str) -> dict[str, Any] | None:
        if str(memory_id) != self.memory_id or owner_id != self.owner_id:
            return None
        return {"memory": self.memory, "events": self.memory.setdefault("_events", [])}

    async def append_memory_lifecycle_event(self, *, memory_id: UUID, owner_id: str, event_type: str, reason_json: dict[str, Any]) -> dict[str, Any] | None:
        self.memory.setdefault("_events", []).append({"event_type": event_type, "reason_json": reason_json})
        return await self.get_memory_debug(memory_id, owner_id)

    async def transition_memory_item(self, *, memory_id: UUID, owner_id: str, new_status: str, reason_code: str, reason_metadata: dict[str, Any], request_id: str, related_memory_id: UUID | None) -> dict[str, Any] | None:
        if str(memory_id) != self.memory_id or owner_id != self.owner_id:
            return None
        self.memory["status"] = new_status
        self.memory.setdefault("_events", []).append({
            "event_type": "state_changed",
            "reason_json": {
                "request_id": request_id,
                "reason_code": reason_code,
                "new_status": new_status,
                "reason_metadata": reason_metadata,
            },
        })
        return {"memory": self.memory, "changed": True}

    async def promote_memory_item(self, **kwargs: Any) -> dict[str, Any]:
        replacement_id = str(uuid4())
        self.memory["status"] = "superseded"
        self.memory["superseded_by_memory_id"] = replacement_id
        row = self._memory_row(replacement_id, kwargs["summary"])
        row["supersedes_memory_id"] = self.memory_id
        self.memory_replacements.append(row)
        return {"memory": row, "events_appended": ["superseded", "created", "promoted"]}

    async def get_episode_debug(self, episode_id: UUID, owner_id: str) -> dict[str, Any] | None:
        if str(episode_id) != self.episode_id or owner_id != self.owner_id:
            return None
        return {"episode": self.episode, "events": self.episode.setdefault("_events", []), "links": []}

    async def append_episode_lifecycle_event(self, *, episode_id: UUID, owner_id: str, event_type: str, reason_json: dict[str, Any]) -> dict[str, Any] | None:
        self.episode.setdefault("_events", []).append({"event_type": event_type, "reason_json": reason_json})
        return await self.get_episode_debug(episode_id, owner_id)

    async def transition_episode_status(self, *, episode_id: UUID, owner_id: str, new_status: str, request_id: str, reason_json: dict[str, Any]) -> dict[str, Any] | None:
        if str(episode_id) != self.episode_id or owner_id != self.owner_id:
            return None
        self.episode["status"] = new_status
        self.episode.setdefault("_events", []).append({"event_type": "updated", "reason_json": {**reason_json, "request_id": request_id}})
        return {"episode": self.episode, "changed": True}

    async def replace_episode(self, **kwargs: Any) -> dict[str, Any]:
        replacement_id = str(uuid4())
        self.episode["status"] = "superseded"
        row = self._episode_row(replacement_id, kwargs["title"])
        row["supersedes_episode_id"] = self.episode_id
        self.episode_replacements.append(row)
        return {"episode": row}


def load_corpus(path: Path = DEFAULT_CORPUS) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("schema_version") != 1:
        raise ValueError("unsupported derivation replay corpus schema_version")
    scenarios = payload.get("scenarios")
    if not isinstance(scenarios, list) or not scenarios:
        raise ValueError("derivation replay corpus must contain scenarios")
    names = {item.get("name") for item in scenarios if isinstance(item, dict)}
    missing = REQUIRED_SCENARIOS - names
    if missing:
        raise ValueError(f"missing derivation replay scenarios: {', '.join(sorted(missing))}")
    banned = set(payload.get("privacy_banned_fields") or [])
    for index, scenario in enumerate(scenarios):
        if not isinstance(scenario, dict):
            raise ValueError(f"scenario {index} must be an object")
        if scenario.get("derived_class") not in DERIVED_CLASSES:
            raise ValueError(f"scenario {scenario.get('name')} has unsupported derived_class")
        expected = scenario.get("expected")
        if not isinstance(expected, dict) or expected.get("result") not in RESULTS:
            raise ValueError(f"scenario {scenario.get('name')} has unsupported expected result")
        leaked = banned & set(expected.get("diagnostic_fields") or [])
        if leaked:
            raise ValueError(f"scenario {scenario.get('name')} exposes banned diagnostic fields: {', '.join(sorted(leaked))}")
    return payload


async def _call(store: ScenarioStore, *, derived_class: str, derived_id: str, persist: bool, request_id: str = "scenario-request", owner_id: str = "owner") -> dict[str, Any]:
    out = await replay_derived(
        store,
        derived_class=derived_class,
        derived_id=UUID(derived_id),
        owner_id=owner_id,
        request_id=request_id,
        requested_derivation_version=None,
        persist_replacement=persist,
    )
    return {"result": "not_found"} if out is None else out["replay"]


async def _run_named(name: str, root: Path) -> dict[str, Any]:
    store = ScenarioStore(root)
    if name == "derived_text_identical_replay":
        replay = await _call(store, derived_class="derived_text", derived_id=store.derived_text_id, persist=False)
    elif name == "derived_text_changed_source":
        store.source_path.write_text("changed source text with a new deterministic boundary", encoding="utf-8")
        replay = await _call(store, derived_class="derived_text", derived_id=store.derived_text_id, persist=True)
    elif name == "derived_text_missing_source":
        store.source_path.unlink()
        replay = await _call(store, derived_class="derived_text", derived_id=store.derived_text_id, persist=False)
    elif name == "proactive_deterministic_reevaluation":
        store.event_log["payload_json"]["account"] = "taxable"
        replay = await _call(store, derived_class="proactive_suggestion", derived_id=store.suggestion_id, persist=False)
    elif name == "proactive_missing_source_event":
        store.event_id = str(uuid4())
        replay = await _call(store, derived_class="proactive_suggestion", derived_id=store.suggestion_id, persist=False)
    elif name == "memory_item_current_subtype_unsupported":
        replay = await _call(store, derived_class="memory_item", derived_id=store.memory_id, persist=False)
    elif name == "memory_item_unsupported_persist_terminal":
        replay = await _call(store, derived_class="memory_item", derived_id=store.memory_id, persist=True)
    elif name == "episode_current_subtype_unsupported":
        replay = await _call(store, derived_class="episode", derived_id=store.episode_id, persist=False)
    elif name == "explicitly_unsupported_subtype":
        store.memory["explanation_json"] = {}
        replay = await _call(store, derived_class="memory_item", derived_id=store.memory_id, persist=False)
    elif name == "cross_owner_rejection":
        replay = await _call(store, derived_class="memory_item", derived_id=store.memory_id, persist=False, owner_id="other-owner")
    elif name == "injected_persistence_failure":
        store.source_path.write_text("changed source text", encoding="utf-8")
        store.fail_next_derived_text_create = True
        replay = await _call(store, derived_class="derived_text", derived_id=store.derived_text_id, persist=True)
    elif name == "repeated_request_idempotency":
        store.source_path.write_text("changed source text", encoding="utf-8")
        first = await _call(store, derived_class="derived_text", derived_id=store.derived_text_id, persist=True, request_id="repeat-request")
        second = await _call(store, derived_class="derived_text", derived_id=store.derived_text_id, persist=True, request_id="repeat-request")
        replay = {**second, "first_result": first["result"], "replacement_count": len(store.created_derived_text)}
    else:
        raise ValueError(f"unimplemented scenario: {name}")
    return {
        "name": name,
        "result": replay.get("result"),
        "failure_reason": replay.get("failure_reason"),
        "replacement_id": replay.get("replacement_id"),
        "idempotent_replay": bool(replay.get("idempotent_replay")),
        "replacement_count": replay.get("replacement_count"),
    }


async def run_corpus(path: Path = DEFAULT_CORPUS) -> dict[str, Any]:
    payload = load_corpus(path)
    executed = []
    by_name = {scenario["name"]: scenario for scenario in payload["scenarios"]}
    with TemporaryDirectory(prefix="derivation-replay-") as tmp:
        root = Path(tmp)
        for name in sorted(REQUIRED_SCENARIOS):
            scenario = by_name[name]
            out = await _run_named(name, root)
            expected = scenario["expected"]
            if out["result"] != expected["result"]:
                raise AssertionError(f"{name} result {out['result']} != expected {expected['result']}")
            if expected.get("failure_reason") and out.get("failure_reason") != expected["failure_reason"]:
                raise AssertionError(f"{name} failure_reason {out.get('failure_reason')} != expected {expected['failure_reason']}")
            if expected.get("duplicate_replacements") is False and out.get("replacement_count") != 1:
                raise AssertionError(f"{name} replacement_count {out.get('replacement_count')} != 1")
            executed.append(out)
    return {
        "schema_version": payload["schema_version"],
        "manifest_scenario_count": len(payload["scenarios"]),
        "static_manifest_validation": "passed",
        "executed_scenario_count": len(executed),
        "executed_scenarios": executed,
    }


def main() -> int:
    payload = asyncio.run(run_corpus())
    print(json.dumps(payload, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
