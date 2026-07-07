"""Tests for the myopic setup wizard helpers. No network — pure file operations."""

from __future__ import annotations

import os
import stat

import pytest

from myopic._wizard import upsert_env_var, write_config_toml
from myopic.config import config_path, env_path


@pytest.fixture()
def isolated_home(tmp_path, monkeypatch):
    """Redirect the myopic config dir to a tmp path via MYOPIC_HOME."""
    monkeypatch.setenv("MYOPIC_HOME", str(tmp_path))
    return tmp_path


class TestWriteConfigToml:
    def test_writes_url_and_token_reference(self, isolated_home):
        write_config_toml("https://gitlab.mycompany.com/")
        text = config_path().read_text()
        assert "gitlab.mycompany.com" in text
        assert "${GITLAB_TOKEN}" in text

    def test_trailing_slash_stripped(self, isolated_home):
        write_config_toml("https://gitlab.com/")
        assert 'url = "https://gitlab.com"' in config_path().read_text()

    def test_secret_never_in_toml(self, isolated_home):
        # The wizard writes the value only to .env, never the TOML.
        write_config_toml("https://gitlab.com")
        assert "glpat" not in config_path().read_text().lower()


class TestUpsertEnvVar:
    def test_writes_secret(self, isolated_home):
        upsert_env_var("GITLAB_TOKEN", "glpat-abc")
        assert "GITLAB_TOKEN=glpat-abc" in env_path().read_text()

    @pytest.mark.skipif(os.name != "posix", reason="chmod semantics are POSIX-only")
    def test_env_is_chmod_600(self, isolated_home):
        upsert_env_var("GITLAB_TOKEN", "glpat-abc")
        perms = stat.S_IMODE(os.stat(env_path()).st_mode)
        assert perms == 0o600

    def test_rotation_replaces_not_appends(self, isolated_home):
        upsert_env_var("GITLAB_TOKEN", "old")
        upsert_env_var("GITLAB_TOKEN", "new")
        text = env_path().read_text()
        assert text.count("GITLAB_TOKEN=") == 1
        assert "new" in text and "old" not in text

    def test_preserves_other_lines(self, isolated_home):
        env_path().parent.mkdir(parents=True, exist_ok=True)
        env_path().write_text("OTHER=keepme\nGITLAB_TOKEN=old\n")
        upsert_env_var("GITLAB_TOKEN", "new")
        text = env_path().read_text()
        assert "OTHER=keepme" in text
        assert "GITLAB_TOKEN=new" in text
