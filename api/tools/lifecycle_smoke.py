from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
from uuid import UUID, uuid4

from services.memory_items import normalize_source_refs, source_ref_hash
from services.memory_lifecycle import effective_freshness_state
from services.derivation_lifecycle import replay_derived
from storage.postgres import PostgresStore
from tools import schema_migrations


async def run_smoke(*, dsn: str, db_dir: Path) -> dict[str, object]:
    with schema_migrations.psycopg.connect(dsn, row_factory=schema_migrations.dict_row) as conn:
        upgrade = schema_migrations.run_upgrade(conn, db_dir)
        status = schema_migrations.run_status(conn, db_dir)
        check = schema_migrations.run_check(conn, db_dir)

    owner_id = "lifecycle-smoke-owner"
    first_refs = normalize_source_refs(
        [{"ref_type": "message", "ref_id": "lifecycle-smoke-source-1", "support_kind": "direct"}]
    )
    second_refs = normalize_source_refs(
        [{"ref_type": "message", "ref_id": "lifecycle-smoke-source-2", "support_kind": "direct"}]
    )

    store = PostgresStore(dsn)
    await store.open()
    try:
        original = await store.promote_memory_item(
            owner_id=owner_id,
            memory_type="preference",
            summary="Neutral lifecycle smoke memory.",
            source_refs_json=first_refs,
            source_ref_hash=source_ref_hash(first_refs),
            scores_json={},
            promotion_state="promoted",
            confidence=0.8,
            explanation_json={"rationale": "disposable smoke"},
            generation_trace_id="lifecycle-smoke-create-1",
            expires_at=None,
            request_id="lifecycle-smoke-create-1",
            reinforce=False,
            supersedes_memory_id=None,
        )
        replacement = await store.promote_memory_item(
            owner_id=owner_id,
            memory_type="preference",
            summary="Neutral corrected lifecycle smoke memory.",
            source_refs_json=second_refs,
            source_ref_hash=source_ref_hash(second_refs),
            scores_json={},
            promotion_state="promoted",
            confidence=0.9,
            explanation_json={"rationale": "disposable smoke correction"},
            generation_trace_id="lifecycle-smoke-create-2",
            expires_at=None,
            request_id="lifecycle-smoke-create-2",
            reinforce=False,
            supersedes_memory_id=None,
        )
        original_id = UUID(original["memory"]["memory_id"])
        replacement_id = UUID(replacement["memory"]["memory_id"])
        await store.transition_memory_item(
            memory_id=original_id,
            owner_id=owner_id,
            new_status="parked",
            reason_code="smoke_parked",
            reason_metadata={"source": "smoke"},
            request_id="lifecycle-smoke-parked",
            related_memory_id=None,
        )
        await store.transition_memory_item(
            memory_id=original_id,
            owner_id=owner_id,
            new_status="stale",
            reason_code="smoke_stale",
            reason_metadata={"source": "smoke"},
            request_id="lifecycle-smoke-stale",
            related_memory_id=None,
        )
        await store.transition_memory_item(
            memory_id=replacement_id,
            owner_id=owner_id,
            new_status="corrected",
            reason_code="smoke_correction",
            reason_metadata={"source": "smoke"},
            request_id="lifecycle-smoke-corrected",
            related_memory_id=original_id,
        )
        artifact_id = uuid4()
        source_path = Path("/tmp") / f"wave2c-lifecycle-smoke-{artifact_id}.txt"
        source_path.write_text("Wave 2C lifecycle smoke source.", encoding="utf-8")
        artifact = await store.create_artifact(
            artifact_id=artifact_id,
            owner_id=owner_id,
            filename=source_path.name,
            mime="text/plain",
            size=source_path.stat().st_size,
            object_uri=f"file://{source_path}",
            status="completed",
        )
        derived = await store.create_derived_text(
            artifact_id=UUID(artifact["artifact_id"]),
            kind="chunk",
            text="Wave 2C lifecycle smoke source.",
            language=None,
            derivation_params={
                "derivation_type": "chunk",
                "derivation_version": "file-chunk-v1",
                "chunking_algorithm": "fixed-overlap-text",
                "chunking_algorithm_version": "fixed-overlap-text-v1",
                "chunk_size": 400,
                "chunk_overlap": 0,
                "status": "active",
                "source_refs": [{"ref_type": "artifact", "ref_id": artifact["artifact_id"], "support_kind": "direct"}],
                "chunk_index": 0,
                "char_start": 0,
                "char_end": len("Wave 2C lifecycle smoke source."),
            },
        )
        wave2c_replay = await replay_derived(
            store,
            derived_class="derived_text",
            derived_id=UUID(derived["derived_text_id"]),
            owner_id=owner_id,
            request_id="lifecycle-smoke-wave2c-replay",
            requested_derivation_version=None,
            persist_replacement=True,
            expected_current_derivation_version="file-chunk-v1",
        )
    finally:
        await store.close()

    reopened = PostgresStore(dsn)
    await reopened.open()
    try:
        original_debug = await reopened.get_memory_debug(original_id, owner_id)
        replacement_debug = await reopened.get_memory_debug(replacement_id, owner_id)
        wave2c_debug = await reopened.get_derived_text_for_owner(UUID(derived["derived_text_id"]), owner_id)
    finally:
        await reopened.close()

    assert original_debug is not None
    assert replacement_debug is not None
    assert original_debug["memory"]["status"] == "superseded"
    assert original_debug["memory"]["superseded_by_memory_id"] == str(replacement_id)
    assert effective_freshness_state(original_debug["memory"]) == "superseded"
    assert replacement_debug["memory"]["status"] == "corrected"
    assert replacement_debug["memory"]["supersedes_memory_id"] == str(original_id)
    assert effective_freshness_state(replacement_debug["memory"]) == "corrected"
    assert wave2c_replay is not None
    assert wave2c_replay["replay"]["result"] == "identical"
    assert wave2c_debug is not None
    assert wave2c_debug["derivation_params"]["lifecycle"]["terminal_result"] == "identical"
    lifecycle_events = [
        event for event in original_debug["events"] if event["event_type"] == "state_changed"
    ]
    assert [event["reason_json"]["new_status"] for event in lifecycle_events] == [
        "parked",
        "stale",
        "superseded",
    ]
    return {
        "status": "ok",
        "original_memory_id": str(original_id),
        "replacement_memory_id": str(replacement_id),
        "original_status": original_debug["memory"]["status"],
        "replacement_status": replacement_debug["memory"]["status"],
        "original_event_count": len(original_debug["events"]),
        "replacement_event_count": len(replacement_debug["events"]),
        "reopen_verified": True,
        "wave2c_replay_result": wave2c_replay["replay"]["result"],
        "wave2c_recipe_chunk_size": wave2c_debug["derivation_params"]["chunk_size"],
        "migration_state": status["state"],
        "migration_check_state": check["state"],
        "applied_migrations": upgrade["applied_migrations"],
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dsn", required=True)
    parser.add_argument("--db-dir", default="/app/db")
    args = parser.parse_args()
    payload = asyncio.run(run_smoke(dsn=args.dsn, db_dir=Path(args.db_dir)))
    print(json.dumps(payload, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
