from __future__ import annotations

from typing import Any


def build_context_block(retrieved: list[dict[str, Any]], max_chars: int) -> str:
    if not retrieved:
        return ""

    lines = ["Relevant past context (verbatim excerpts):"]
    for item in retrieved:
        ts = item.get("created_at", "")
        role = item.get("role", "")
        cid = item.get("conversation_id", "")
        content = item.get("content", "")
        lines.append(f"- [{ts}] (convo={cid}) {role}: {content}")

    text = "\n".join(lines)
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 200] + "\n…(truncated)\n"


def build_artifact_context_block(artifact_refs: list[dict[str, Any]], max_chars: int) -> str:
    if not artifact_refs:
        return ""

    lines = ["Relevant ingested file excerpts:"]
    for item in artifact_refs:
        repo = item.get("repo_name")
        file_path = item.get("file_path", "")
        label = f"{repo}/{file_path}" if repo else file_path
        snippet = item.get("snippet", "")
        lines.append(f"- [{label}] {snippet}")

    text = "\n".join(lines)
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 200] + "\n…(truncated)\n"


def assemble_messages(system_preamble: str, context_block: str, recent_messages: list[dict[str, Any]], user_messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    msgs: list[dict[str, Any]] = [{"role": "system", "content": system_preamble}]
    if context_block:
        msgs.append({"role": "system", "content": context_block})

    # Add recent conversation window (already role/content)
    msgs.extend(recent_messages)

    # Add new user-provided messages (this request)
    msgs.extend(user_messages)
    return msgs
