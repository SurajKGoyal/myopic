"""
embeddings — text-to-vector via a local Ollama server.

Lazily imports httpx so the base myopic install never requires it; only
myopic[semantic] pulls it in.
"""

from __future__ import annotations

from myopic.config import embed_model, ollama_url

EMBED_BATCH = 32


def embed_texts(texts: list[str]) -> list[list[float]]:
    """Embed a list of texts via Ollama's /api/embed endpoint, batched.

    Raises RuntimeError with an actionable message (mentioning MYOPIC_OLLAMA_URL
    and that the model must be pulled) if Ollama is unreachable or errors.
    The semantic extra install guidance is surfaced at the TOOL layer — this
    function raises RuntimeError since it is an internal helper, not an MCP
    tool boundary.
    """
    try:
        import httpx
    except ImportError as exc:
        raise RuntimeError(
            "semantic search needs the optional extra — install with: pip install myopic[semantic]"
        ) from exc

    url = ollama_url()
    model = embed_model()
    vectors: list[list[float]] = []

    for i in range(0, len(texts), EMBED_BATCH):
        batch = texts[i : i + EMBED_BATCH]
        try:
            resp = httpx.post(
                f"{url}/api/embed",
                json={"model": model, "input": batch},
                timeout=120,
            )
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            raise RuntimeError(
                f"Could not reach Ollama at {url} to embed text (model={model}): {exc}. "
                f"Check MYOPIC_OLLAMA_URL and that the model is pulled "
                f"(`ollama pull {model}`)."
            ) from exc

        data = resp.json()
        vectors.extend(data["embeddings"])

    return vectors
