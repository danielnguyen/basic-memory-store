from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional, Sequence

import httpx


@dataclass
class LiteLLMClient:
    """
    Minimal async client for LiteLLM's OpenAI-compatible endpoints.
    Designed to be swappable and boring.

    Assumes embeddings endpoint:
      POST {base_url}/v1/embeddings
    with OpenAI-style payload:
      {"model": "...", "input": ["text1", "text2", ...]}
    """

    base_url: str
    api_key: Optional[str] = None
    timeout_s: float = 30.0

    def _headers(self, request_id: str | None = None) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        if request_id:
            headers["X-Request-ID"] = request_id
        return headers

    def _url(self, path: str) -> str:
        return self.base_url.rstrip("/") + path

    async def embeddings(self, model: str, texts: list[str], request_id: str | None = None) -> list[list[float]]:
        """
        OpenAI-compatible embeddings call via LiteLLM.
        Returns embeddings in the same order as input.
        """
        if not texts:
            return []

        url = self._url("/v1/embeddings")
        payload: dict[str, Any] = {"model": model, "input": texts}

        async with httpx.AsyncClient(timeout=self.timeout_s) as client:
            r = await client.post(url, headers=self._headers(request_id), json=payload)
            try:
                r.raise_for_status()
            except httpx.HTTPStatusError as e:
                # Include provider error body for easier debugging
                raise RuntimeError(f"LiteLLM embeddings failed: {e.response.status_code} {e.response.text}") from e
            data = r.json()

        items = data.get("data")
        if not isinstance(items, list):
            raise RuntimeError(f"Unexpected embeddings response shape: {data!r}")

        # Expected OpenAI format:
        # {"data":[{"embedding":[...], "index":0, ...}, ...], ...}
        items = sorted(items, key=lambda x: x.get("index", 0))
        vectors: list[list[float]] = []
        for it in items:
            emb = it.get("embedding")
            if not isinstance(emb, list):
                raise RuntimeError(f"Missing/invalid embedding in response item: {it!r}")
            vectors.append(emb)

        # Optional sanity: ensure same count as input
        if len(vectors) != len(texts):
            raise RuntimeError(
                f"Embeddings count mismatch: got {len(vectors)}, expected {len(texts)}"
            )

        return vectors

    async def embed_texts(self, model: str, texts: Sequence[str]) -> list[list[float]]:
        """
        Protocol-compatible alias for storage.qdrant.Embedder:
          async embed_texts(model, texts) -> list[list[float]]
        """
        return await self.embeddings(model, list(texts))
    
    async def chat(
        self,
        model: str,
        messages: Sequence[dict[str, Any]],
        temperature: float | None = None,
        max_tokens: Optional[int] = None,
        request_id: str | None = None,
    ) -> str:
        """
        OpenAI-compatible chat completions call via LiteLLM.

        Assumes:
          POST {base_url}/v1/chat/completions
        Payload:
          {"model": "...", "messages": [...], "temperature": 1, "max_tokens": ...}

        Returns:
          assistant message content (string)
        """
        url = self._url("/v1/chat/completions")
        payload: dict[str, Any] = {
            "model": model,
            "messages": list(messages)
        }
        if temperature is not None:
            payload["temperature"] = temperature
        if max_tokens is not None:
            payload["max_tokens"] = max_tokens

        async with httpx.AsyncClient(timeout=self.timeout_s) as client:
            r = await client.post(url, headers=self._headers(request_id), json=payload)
            try:
                r.raise_for_status()
            except httpx.HTTPStatusError as e:
                # Include provider error body for easier debugging
                raise RuntimeError(f"LiteLLM chat failed: {e.response.status_code} {e.response.text}") from e
            data = r.json()

        # OpenAI-compatible response:
        # {"choices":[{"message":{"role":"assistant","content":"..."}, ...}], ...}
        try:
            return data["choices"][0]["message"]["content"]
        except Exception as e:
            raise RuntimeError(f"Unexpected chat response shape: {data!r}") from e


class LiteLLMEmbedder:
    """
    Adapter to satisfy storage.qdrant.Embedder protocol:
      async embed_texts(model, texts) -> list[list[float]]
    """

    def __init__(self, litellm: LiteLLMClient) -> None:
        self.litellm = litellm

    async def embed_texts(self, model: str, texts: Sequence[str], request_id: str | None = None) -> list[list[float]]:
        return await self.embeddings(model, list(texts), request_id=request_id)
