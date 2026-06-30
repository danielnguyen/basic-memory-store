from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from services.chunking import chunk_text, iter_ingestable_paths


TEXT_ARTIFACT_DERIVATION_VERSION = "text-artifact-chunk-v1"
LOCAL_FILE_DERIVATION_VERSION = "file-chunk-v1"
SUPPORTED_TEXT_ARTIFACT_MIME = {"text/plain", "text/markdown", "application/json"}


async def derive_text_chunks_for_artifact(
    *,
    pg: Any,
    qdrant: Any,
    settings: Any,
    artifact: dict[str, Any],
    text: str,
    derivation_version: str,
    file_path: str | None = None,
    repo_name: str | None = None,
    ingestion_id: UUID | None = None,
) -> dict[str, int]:
    artifact_id = UUID(artifact["artifact_id"])
    chunks = chunk_text(
        text,
        chunk_size=settings.ingest_chunk_size_chars,
        chunk_overlap=settings.ingest_chunk_overlap_chars,
    )
    expected_indexes = {int(chunk["chunk_index"]) for chunk in chunks}
    existing = await pg.get_derived_text_for_artifact_version(
        artifact_id=artifact_id,
        derivation_version=derivation_version,
    )
    active_indexed = [
        row
        for row in existing
        if _is_completed_chunk(row, expected_indexes=expected_indexes)
    ]
    active_indexes = {
        int(row["derivation_params"]["chunk_index"])
        for row in active_indexed
    }
    if active_indexes == expected_indexes and len(active_indexed) == len(chunks):
        return {"chunks_created": 0, "chunks_indexed": 0, "chunks_existing": len(active_indexed)}

    if existing:
        await _retire_incomplete_derivation_rows(
            pg=pg,
            qdrant=qdrant,
            owner_id=artifact["owner_id"],
            rows=existing,
            expected_indexes=expected_indexes,
        )

    attempt_id = str(uuid4())
    pending: list[dict[str, Any]] = []
    chunks_created = 0
    chunks_indexed = 0
    for chunk in chunks:
        chunk_index = int(chunk["chunk_index"])
        qdrant_point_id = _qdrant_point_id(
            artifact_id=artifact_id,
            derivation_version=derivation_version,
            chunk_index=chunk_index,
        )
        derived = await pg.create_derived_text(
            artifact_id=artifact_id,
            kind="chunk",
            text=str(chunk["text"]),
            language=None,
            derivation_params={
                "derivation_type": "chunk",
                "derivation_version": derivation_version,
                "chunking_algorithm": "fixed-overlap-text",
                "chunking_algorithm_version": "fixed-overlap-text-v1",
                "chunk_size": settings.ingest_chunk_size_chars,
                "chunk_overlap": settings.ingest_chunk_overlap_chars,
                "status": "building",
                "indexing_status": "pending",
                "expected_chunk_count": len(chunks),
                "attempt_id": attempt_id,
                "qdrant_point_id": str(qdrant_point_id),
                "source_refs": [
                    {
                        "ref_type": "artifact",
                        "ref_id": artifact["artifact_id"],
                        "support_kind": "direct",
                    }
                ],
                "chunk_index": chunk_index,
                "char_start": chunk["char_start"],
                "char_end": chunk["char_end"],
                "file_path": file_path or artifact.get("file_path") or artifact.get("filename") or "",
                "repo_name": repo_name or artifact.get("repo_name"),
                "ingestion_id": str(ingestion_id) if ingestion_id else artifact.get("ingestion_id"),
            },
        )
        chunks_created += 1
        await qdrant.upsert_derived_text_vector(
            derived_text_id=UUID(derived["derived_text_id"]),
            artifact_id=artifact_id,
            owner_id=artifact["owner_id"],
            content=derived["text"],
            client_id=artifact.get("client_id"),
            conversation_id=artifact.get("conversation_id"),
            qdrant_point_id=qdrant_point_id,
            derivation_status="building",
            derivation_attempt_id=attempt_id,
            derivation_version=derivation_version,
            file_path=file_path or artifact.get("file_path") or artifact.get("filename") or "",
            repo_name=repo_name or artifact.get("repo_name"),
            chunk_index=chunk_index,
        )
        await pg.create_embedding_ref(
            ref_type="derived_text",
            ref_id=UUID(derived["derived_text_id"]),
            model=settings.embed_model,
            qdrant_point_id=str(qdrant_point_id),
        )
        indexed = await pg.update_derived_text_params(
            derived_text_id=UUID(derived["derived_text_id"]),
            owner_id=artifact["owner_id"],
            derivation_params={
                **derived["derivation_params"],
                "status": "building",
                "indexing_status": "indexed",
                "qdrant_point_id": str(qdrant_point_id),
                "embedding_model": settings.embed_model,
            },
        )
        if indexed is None:
            raise RuntimeError("derived text indexing state update failed")
        pending.append(indexed)
        chunks_indexed += 1

    for row in pending:
        params = row["derivation_params"]
        await qdrant.upsert_derived_text_vector(
            derived_text_id=UUID(row["derived_text_id"]),
            artifact_id=artifact_id,
            owner_id=artifact["owner_id"],
            content=row["text"],
            client_id=artifact.get("client_id"),
            conversation_id=artifact.get("conversation_id"),
            qdrant_point_id=params["qdrant_point_id"],
            derivation_status="active",
            derivation_attempt_id=attempt_id,
            derivation_version=derivation_version,
            file_path=file_path or artifact.get("file_path") or artifact.get("filename") or "",
            repo_name=repo_name or artifact.get("repo_name"),
            chunk_index=int(params["chunk_index"]),
        )

    activated = await pg.activate_derived_text_attempt(
        artifact_id=artifact_id,
        owner_id=artifact["owner_id"],
        derivation_version=derivation_version,
        attempt_id=attempt_id,
        expected_chunk_count=len(chunks),
    )
    if len(activated) != len(chunks):
        raise RuntimeError("derived text activation failed")

    return {"chunks_created": chunks_created, "chunks_indexed": chunks_indexed, "chunks_existing": 0}


def is_supported_text_artifact_mime(mime: str | None) -> bool:
    return (mime or "").split(";")[0].strip().lower() in SUPPORTED_TEXT_ARTIFACT_MIME


def _is_completed_chunk(row: dict[str, Any], *, expected_indexes: set[int]) -> bool:
    params = row.get("derivation_params") if isinstance(row, dict) else None
    if not isinstance(params, dict):
        return False
    if params.get("status") != "active" or params.get("indexing_status") != "indexed":
        return False
    try:
        chunk_index = int(params.get("chunk_index"))
    except (TypeError, ValueError):
        return False
    return chunk_index in expected_indexes and bool(params.get("qdrant_point_id"))


def _qdrant_point_id(*, artifact_id: UUID, derivation_version: str, chunk_index: int) -> UUID:
    return uuid5(NAMESPACE_URL, f"basic-memory-store:{artifact_id}:{derivation_version}:{chunk_index}")


async def _retire_incomplete_derivation_rows(
    *,
    pg: Any,
    qdrant: Any,
    owner_id: str,
    rows: list[dict[str, Any]],
    expected_indexes: set[int],
) -> None:
    for row in rows:
        params = row.get("derivation_params") if isinstance(row, dict) else {}
        params = params if isinstance(params, dict) else {}
        if _is_completed_chunk(row, expected_indexes=expected_indexes):
            status = "superseded"
            indexing_status = params.get("indexing_status")
        else:
            status = "failed"
            indexing_status = "incomplete"
        point_id = params.get("qdrant_point_id") or row.get("derived_text_id")
        if point_id:
            await qdrant.mark_derived_text_vector_inactive(
                qdrant_point_id=point_id,
                derivation_status=status,
            )
        updated = await pg.update_derived_text_params(
            derived_text_id=UUID(row["derived_text_id"]),
            owner_id=owner_id,
            derivation_params={
                **params,
                "status": status,
                "indexing_status": indexing_status,
                "retry_replaced": True,
            },
        )
        if updated is None:
            raise RuntimeError("derived text retirement failed")


async def ingest_files(
    *,
    pg,
    qdrant,
    settings,
    owner_id: str,
    client_id: str | None,
    source_surface: str | None,
    repo_name: str | None,
    paths: list[str],
) -> dict[str, str | int | None]:
    allowed_extensions = {item.strip().lower() for item in settings.ingest_allowed_extensions.split(",") if item.strip()}
    exclude_globs = [item.strip() for item in settings.ingest_exclude_globs_default.split(",") if item.strip()]
    ingestion_id = uuid4()

    discovered = iter_ingestable_paths(
        paths,
        allowed_extensions=allowed_extensions,
        exclude_globs=exclude_globs,
    )
    if len(discovered) > settings.ingest_max_files_per_request:
        discovered = discovered[: settings.ingest_max_files_per_request]

    root_candidates = [Path(item).expanduser().resolve() for item in paths]
    files_ingested = 0
    chunks_created = 0
    artifacts_created = 0

    for path in discovered:
        data = path.read_text(encoding="utf-8", errors="ignore")
        size = path.stat().st_size
        if size > settings.ingest_max_file_bytes or not data.strip():
            continue

        file_path = _derive_file_path(path, root_candidates)
        artifact_id = uuid4()
        artifact = await pg.create_artifact(
            artifact_id=artifact_id,
            owner_id=owner_id,
            filename=path.name,
            mime="text/plain",
            size=size,
            object_uri=f"file://{path}",
            client_id=client_id,
            conversation_id=None,
            source_surface=source_surface,
            source_kind="local_file" if not repo_name else "repo_file",
            repo_name=repo_name,
            repo_ref=None,
            file_path=file_path,
            ingestion_id=ingestion_id,
            sha256=hashlib.sha256(data.encode("utf-8")).hexdigest(),
            status="completed",
        )
        artifacts_created += 1

        result = await derive_text_chunks_for_artifact(
            pg=pg,
            qdrant=qdrant,
            settings=settings,
            artifact=artifact,
            text=data,
            derivation_version=LOCAL_FILE_DERIVATION_VERSION,
            file_path=file_path,
            repo_name=repo_name,
            ingestion_id=ingestion_id,
        )
        chunks_created += result["chunks_created"]

        files_ingested += 1

    return {
        "ingestion_id": str(ingestion_id),
        "owner_id": owner_id,
        "repo_name": repo_name,
        "files_seen": len(discovered),
        "files_ingested": files_ingested,
        "chunks_created": chunks_created,
        "artifacts_created": artifacts_created,
        "status": "completed",
    }


def _derive_file_path(path: Path, roots: list[Path]) -> str:
    for root in roots:
        if root.is_dir():
            try:
                return path.relative_to(root).as_posix()
            except ValueError:
                continue
    return path.name
