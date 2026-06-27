from __future__ import annotations

import json
from pathlib import Path
from typing import Any


DEFAULT_CORPUS = Path(__file__).resolve().parents[1] / "replay" / "derivation_scenarios.v1.json"
REQUIRED_SCENARIOS = {
    "derived_text_identical_rebuild",
    "derived_text_source_changed",
    "derived_text_source_missing",
    "proactive_replay_no_delivery_side_effect",
    "proactive_trigger_missing",
    "memory_item_identical_rebuild",
    "memory_item_changed_rebuild_with_supersession",
    "episode_identical_rebuild",
    "unsupported_episode_without_recipe",
    "cross_owner_rejection",
    "injected_persistence_failure",
    "repeated_request_idempotency",
}
DERIVED_CLASSES = {"derived_text", "proactive_suggestion", "memory_item", "episode"}
RESULTS = {"identical", "replaced", "unsupported", "failed", "not_found"}


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
        exposed = set(expected.get("diagnostic_fields") or [])
        leaked = banned & exposed
        if leaked:
            raise ValueError(f"scenario {scenario.get('name')} exposes banned diagnostic fields: {', '.join(sorted(leaked))}")
    return payload


def main() -> int:
    payload = load_corpus()
    print(json.dumps({
        "schema_version": payload["schema_version"],
        "scenario_count": len(payload["scenarios"]),
        "required_scenarios_present": True,
        "privacy_banned_fields_checked": True,
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
