"""config.settings：启动校验。"""
from __future__ import annotations

import pytest

from config.settings import get_settings, validate_runtime


class TestSettings:
    def test_defaults(self, monkeypatch):
        monkeypatch.setenv("LLM_API_KEY", "sk-test")
        get_settings.cache_clear()
        s = get_settings()
        assert s.llm_model
        assert s.server_port == 8000
        assert s.job_retention_days == 30

    def test_validate_requires_key(self, monkeypatch):
        monkeypatch.setenv("LLM_API_KEY", "")
        get_settings.cache_clear()
        with pytest.raises(ValueError, match="LLM_API_KEY"):
            validate_runtime(require_llm_key=True)

    def test_validate_ok(self, monkeypatch):
        monkeypatch.setenv("LLM_API_KEY", "sk-x")
        monkeypatch.setenv("LLM_MODEL", "gpt-test")
        get_settings.cache_clear()
        s = validate_runtime(require_llm_key=True)
        assert s.llm_model == "gpt-test"

    def test_bad_port(self, monkeypatch):
        monkeypatch.setenv("LLM_API_KEY", "sk-x")
        monkeypatch.setenv("SERVER_PORT", "99999")
        get_settings.cache_clear()
        with pytest.raises(Exception):
            get_settings()
