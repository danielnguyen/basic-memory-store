from __future__ import annotations

from pathlib import Path
from typing import Iterable


def is_allowed_path(path: Path, *, allowed_extensions: set[str], exclude_globs: list[str]) -> bool:
    if not path.is_file():
        return False
    if path.suffix.lower() not in allowed_extensions:
        return False
    posix = path.as_posix()
    for pattern in exclude_globs:
        pattern = pattern.strip()
        if pattern and path.match(pattern):
            return False
        if pattern.endswith("/*") and posix.startswith(pattern[:-1]):
            return False
    return True


def chunk_text(text: str, *, chunk_size: int, chunk_overlap: int) -> list[dict[str, int | str]]:
    if not text:
        return []

    chunks: list[dict[str, int | str]] = []
    start = 0
    index = 0
    step = max(1, chunk_size - chunk_overlap)
    text_len = len(text)
    while start < text_len:
        end = min(text_len, start + chunk_size)
        snippet = text[start:end].strip()
        if snippet:
            chunks.append(
                {
                    "chunk_index": index,
                    "text": snippet,
                    "char_start": start,
                    "char_end": end,
                }
            )
            index += 1
        if end >= text_len:
            break
        start += step
    return chunks


def iter_ingestable_paths(paths: Iterable[str], *, allowed_extensions: set[str], exclude_globs: list[str]) -> list[Path]:
    out: list[Path] = []
    for raw in paths:
        path = Path(raw).expanduser().resolve()
        if path.is_file():
            if is_allowed_path(path, allowed_extensions=allowed_extensions, exclude_globs=exclude_globs):
                out.append(path)
            continue
        if not path.is_dir():
            continue
        for child in sorted(p for p in path.rglob("*") if p.is_file()):
            if is_allowed_path(child, allowed_extensions=allowed_extensions, exclude_globs=exclude_globs):
                out.append(child)
    return out
