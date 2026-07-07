"""
Tests for `myopic doctor`. The pure model-matching logic is always tested; the
CLI integration (which needs the semantic extra importable) is gated on lancedb.
"""

from __future__ import annotations

import importlib.util

import pytest
from click.testing import CliRunner

from myopic import cli as cli_mod

_LANCEDB = importlib.util.find_spec("lancedb") is not None

_MODEL = "unclemusclez/jina-embeddings-v2-base-code"


class TestModelPresent:
    def test_exact(self):
        assert cli_mod._model_present([_MODEL], _MODEL) is True

    def test_implicit_latest_tag(self):
        # Ollama reports the pulled model with a :latest tag appended.
        assert cli_mod._model_present([f"{_MODEL}:latest"], _MODEL) is True

    def test_absent(self):
        assert cli_mod._model_present(["nomic-embed-text:latest", "llama3:8b"], _MODEL) is False

    def test_empty(self):
        assert cli_mod._model_present([], _MODEL) is False


@pytest.mark.skipif(not _LANCEDB, reason="doctor's Ollama checks need the semantic extra")
class TestDoctorCommand:
    def _configured(self, monkeypatch, tmp_path):
        # A valid GitLab config so the platform section passes.
        monkeypatch.setenv("MYOPIC_HOME", str(tmp_path))
        monkeypatch.setenv("GITLAB_URL", "https://gitlab.com")
        monkeypatch.setenv("GITLAB_TOKEN", "tok")
        from myopic.config import invalidate_config_cache
        invalidate_config_cache()

    def test_model_present(self, monkeypatch, tmp_path):
        self._configured(monkeypatch, tmp_path)
        monkeypatch.setattr(cli_mod, "_ollama_models", lambda url: [f"{_MODEL}:latest"])
        result = CliRunner().invoke(cli_mod.cli, ["doctor"])
        assert result.exit_code == 0
        assert "embedding model pulled" in result.output
        assert "All good" in result.output

    def test_ollama_unreachable_is_soft(self, monkeypatch, tmp_path):
        self._configured(monkeypatch, tmp_path)

        def _boom(url):
            raise RuntimeError("Connection refused")

        monkeypatch.setattr(cli_mod, "_ollama_models", _boom)
        result = CliRunner().invoke(cli_mod.cli, ["doctor"])
        # Ollama down is optional -> not a hard failure.
        assert result.exit_code == 0
        assert "not reachable" in result.output

    def test_model_missing_no_pull(self, monkeypatch, tmp_path):
        self._configured(monkeypatch, tmp_path)
        monkeypatch.setattr(cli_mod, "_ollama_models", lambda url: ["llama3:8b"])
        pulled = {"called": False}
        monkeypatch.setattr(cli_mod, "_pull_model",
                            lambda url, model: pulled.__setitem__("called", True))
        result = CliRunner().invoke(cli_mod.cli, ["doctor", "--no-pull"])
        assert result.exit_code == 0
        assert "not pulled" in result.output
        assert pulled["called"] is False

    def test_model_missing_pull_invoked(self, monkeypatch, tmp_path):
        self._configured(monkeypatch, tmp_path)
        monkeypatch.setattr(cli_mod, "_ollama_models", lambda url: [])
        calls = []
        monkeypatch.setattr(cli_mod, "_pull_model", lambda url, model: calls.append(model))
        result = CliRunner().invoke(cli_mod.cli, ["doctor", "--pull"])
        assert result.exit_code == 0
        assert calls == [_MODEL]

    def test_no_platform_is_hard_fail(self, monkeypatch, tmp_path):
        monkeypatch.setenv("MYOPIC_HOME", str(tmp_path))
        for var in ("GITLAB_URL", "GITLAB_TOKEN", "MYOPIC_GITLAB_URL", "MYOPIC_GITLAB_TOKEN",
                    "GITHUB_TOKEN", "MYOPIC_GITHUB_TOKEN"):
            monkeypatch.delenv(var, raising=False)
        from myopic.config import invalidate_config_cache
        invalidate_config_cache()
        monkeypatch.setattr(cli_mod, "_ollama_models", lambda url: [f"{_MODEL}:latest"])
        result = CliRunner().invoke(cli_mod.cli, ["doctor"])
        assert result.exit_code == 1
        assert "No platform configured" in result.output
