"""
embeddings — text-to-vector via a local Ollama server.

Lazily imports httpx so the base myopic install never requires it; only
myopic[semantic] pulls it in.
"""

from __future__ import annotations

from myopic.config import auto_pull, embed_model, ollama_url

EMBED_BATCH = 32


def embed_texts(texts: list[str]) -> list[list[float]]:
    """Embed a list of texts via Ollama's /api/embed endpoint, batched.

    If the model isn't pulled (Ollama returns 404) and MYOPIC_AUTO_PULL is set,
    the model is pulled once and the batch retried. Otherwise raises RuntimeError
    with an actionable message. The semantic-extra install guidance is surfaced at
    the TOOL layer — this internal helper raises RuntimeError, not tool errors.
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
    pulled = False  # auto-pull at most once per call

    def _embed(batch: list[str]) -> list[list[float]]:
        resp = httpx.post(f"{url}/api/embed", json={"model": model, "input": batch}, timeout=120)
        resp.raise_for_status()
        return resp.json()["embeddings"]

    for i in range(0, len(texts), EMBED_BATCH):
        batch = texts[i : i + EMBED_BATCH]
        try:
            vectors.extend(_embed(batch))
        except httpx.HTTPStatusError as exc:
            # A 404 means the model isn't pulled. Auto-pull once if opted in.
            if exc.response.status_code == 404 and auto_pull() and not pulled:
                from myopic.ollama import pull_model
                try:
                    pull_model(url, model)
                except Exception as pexc:
                    raise RuntimeError(
                        f"Embedding model {model} is not pulled and MYOPIC_AUTO_PULL "
                        f"failed to fetch it: {pexc}. Run `myopic doctor`."
                    ) from pexc
                pulled = True
                vectors.extend(_embed(batch))  # retry the same batch once
            else:
                raise RuntimeError(
                    f"Ollama at {url} returned an error embedding text (model={model}): {exc}. "
                    f"Run `myopic doctor` to check Ollama and pull the model "
                    f"(or set MYOPIC_AUTO_PULL=1)."
                ) from exc
        except httpx.HTTPError as exc:
            raise RuntimeError(
                f"Could not reach Ollama at {url} to embed text (model={model}): {exc}. "
                f"Run `myopic doctor` to check Ollama and set MYOPIC_OLLAMA_URL."
            ) from exc

    return vectors
