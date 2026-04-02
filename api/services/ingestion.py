from __future__ import annotations

import hashlib
from pathlib import Path
from uuid import UUID, uuid4

from services.chunking import chunk_text, iter_ingestable_paths


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

        chunks = chunk_text(
            data,
            chunk_size=settings.ingest_chunk_size_chars,
            chunk_overlap=settings.ingest_chunk_overlap_chars,
        )
        for chunk in chunks:
            derived = await pg.create_derived_text(
                artifact_id=UUID(artifact["artifact_id"]),
                kind="chunk",
                text=str(chunk["text"]),
                language=None,
                derivation_params={
                    "chunk_index": chunk["chunk_index"],
                    "char_start": chunk["char_start"],
                    "char_end": chunk["char_end"],
                    "file_path": file_path,
                    "repo_name": repo_name,
                    "ingestion_id": str(ingestion_id),
                },
            )
            await qdrant.upsert_derived_text_vector(
                derived_text_id=UUID(derived["derived_text_id"]),
                artifact_id=UUID(artifact["artifact_id"]),
                owner_id=owner_id,
                content=derived["text"],
                client_id=client_id,
                file_path=file_path,
                repo_name=repo_name,
                chunk_index=int(chunk["chunk_index"]),
            )
            await pg.create_embedding_ref(
                ref_type="derived_text",
                ref_id=UUID(derived["derived_text_id"]),
                model=settings.embed_model,
                qdrant_point_id=derived["derived_text_id"],
            )
            chunks_created += 1

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
