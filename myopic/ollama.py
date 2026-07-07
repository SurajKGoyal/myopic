"""
Thin Ollama HTTP helpers, shared by `myopic doctor` (interactive) and the
auto-pull path in embeddings (headless). httpx is imported lazily so the base
install never requires it.

myopic never manages the Ollama process — it only talks to a server the user
already runs. These helpers just wrap the three calls we need: list models,
check presence, and pull.
"""

from __future__ import annotations

from typing import Callable


def list_models(url: str) -> list[str]:
    """Names of models pulled on the Ollama server. Raises if unreachable."""
    import httpx

    resp = httpx.get(f"{url}/api/tags", timeout=5)
    resp.raise_for_status()
    return [m.get("name", "") for m in resp.json().get("models", [])]


def model_present(models: list[str], model: str) -> bool:
    """True if `model` is among `models`, tolerant of an implicit :latest tag."""
    base = model.split(":")[0]
    return any(n == model or n.split(":")[0] == base for n in models)


def pull_model(
    url: str,
    model: str,
    on_progress: Callable[[str, int | None, int | None], None] | None = None,
) -> None:
    """Pull a model via Ollama's HTTP API.

    Streams progress; if `on_progress` is given it's called with
    (status, completed, total) per update. Raises RuntimeError on a pull error.
    """
    import json

    import httpx

    with httpx.stream("POST", f"{url}/api/pull", json={"model": model}, timeout=None) as r:
        r.raise_for_status()
        for line in r.iter_lines():
            if not line:
                continue
            msg = json.loads(line)
            if msg.get("error"):
                raise RuntimeError(msg["error"])
            if on_progress:
                on_progress(msg.get("status", ""), msg.get("completed"), msg.get("total"))
