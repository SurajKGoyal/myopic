"""
Tests for MYOPIC_AUTO_PULL: embed_texts pulls a missing model once (on a 404)
when opted in, and otherwise raises an actionable error. httpx is available in
the base install (via the mcp SDK), so no gating needed.
"""

from __future__ import annotations

import httpx
import pytest

import myopic.embeddings as emb
import myopic.ollama as ollama_mod
from myopic.config import auto_pull


class _Resp:
    def __init__(self, status: int, payload=None):
        self.status_code = status
        self._payload = payload

    def raise_for_status(self):
        if self.status_code >= 400:
            req = httpx.Request("POST", "http://x/api/embed")
            raise httpx.HTTPStatusError(
                "err", request=req, response=httpx.Response(self.status_code, request=req)
            )

    def json(self):
        return self._payload


class TestAutoPullConfig:
    def test_truthy_values(self, monkeypatch):
        for v in ("1", "true", "YES", "on"):
            monkeypatch.setenv("MYOPIC_AUTO_PULL", v)
            assert auto_pull() is True

    def test_falsey(self, monkeypatch):
        monkeypatch.setenv("MYOPIC_AUTO_PULL", "0")
        assert auto_pull() is False
        monkeypatch.delenv("MYOPIC_AUTO_PULL", raising=False)
        assert auto_pull() is False


class TestEmbedAutoPull:
    def test_pulls_once_then_retries(self, monkeypatch):
        monkeypatch.setenv("MYOPIC_AUTO_PULL", "1")
        calls = {"post": 0, "pull": 0}

        def fake_post(url, json, timeout):
            calls["post"] += 1
            if calls["post"] == 1:
                return _Resp(404)               # model not pulled
            return _Resp(200, {"embeddings": [[0.1, 0.2]]})

        monkeypatch.setattr(httpx, "post", fake_post)
        monkeypatch.setattr(ollama_mod, "pull_model",
                            lambda url, model: calls.__setitem__("pull", calls["pull"] + 1))

        out = emb.embed_texts(["hello"])
        assert out == [[0.1, 0.2]]
        assert calls["pull"] == 1 and calls["post"] == 2

    def test_404_without_optin_raises(self, monkeypatch):
        monkeypatch.delenv("MYOPIC_AUTO_PULL", raising=False)
        monkeypatch.setattr(httpx, "post", lambda url, json, timeout: _Resp(404))
        pulled = {"n": 0}
        monkeypatch.setattr(ollama_mod, "pull_model",
                            lambda url, model: pulled.__setitem__("n", pulled["n"] + 1))
        with pytest.raises(RuntimeError, match="doctor"):
            emb.embed_texts(["hello"])
        assert pulled["n"] == 0   # never pulled without opt-in

    def test_pull_failure_surfaces(self, monkeypatch):
        monkeypatch.setenv("MYOPIC_AUTO_PULL", "1")
        monkeypatch.setattr(httpx, "post", lambda url, json, timeout: _Resp(404))

        def _boom(url, model):
            raise RuntimeError("registry down")

        monkeypatch.setattr(ollama_mod, "pull_model", _boom)
        with pytest.raises(RuntimeError, match="failed to fetch"):
            emb.embed_texts(["hello"])
