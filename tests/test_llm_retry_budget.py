"""LLM 重试与 token 预算。"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from agent import llm
from config.settings import get_settings


@pytest.fixture
def _reset_usage():
    llm.reset_token_usage()
    yield
    llm.reset_token_usage()
    get_settings.cache_clear()


def test_token_budget_blocks_chat(_reset_usage, monkeypatch):
    monkeypatch.setenv("LLM_MAX_TOTAL_TOKENS", "100")
    get_settings.cache_clear()

    usage = llm._current_usage()
    usage.total_tokens = 100
    usage.prompt_tokens = 80
    usage.completion_tokens = 20

    with pytest.raises(llm.TokenBudgetExceeded):
        llm.chat([{"role": "user", "content": "hi"}], [])


def test_retry_on_429_then_success(_reset_usage, monkeypatch):
    monkeypatch.setenv("LLM_RETRY_MAX", "2")
    monkeypatch.setenv("LLM_RETRY_MIN_WAIT", "0.01")
    monkeypatch.setenv("LLM_RETRY_MAX_WAIT", "0.02")
    get_settings.cache_clear()

    calls = {"n": 0}

    def flaky():
        calls["n"] += 1
        if calls["n"] < 3:
            raise llm.RateLimitError(
                "rate limited",
                response=MagicMock(status_code=429, headers={}),
                body=None,
            )
        resp = MagicMock()
        resp.usage = None
        msg = MagicMock()
        msg.content = "ok"
        msg.tool_calls = None
        resp.choices = [MagicMock(message=msg)]
        return resp

    with patch.object(llm, "_chat_client") as client_fn:
        client = MagicMock()
        client.chat.completions.create.side_effect = lambda **kw: flaky()
        client_fn.return_value = client
        with patch.object(llm, "time") as fake_time:
            fake_time.sleep = MagicMock()
            actions = llm.chat([{"role": "user", "content": "x"}], [])
    assert actions == []
    assert calls["n"] == 3


def test_413_not_retried(_reset_usage, monkeypatch):
    monkeypatch.setenv("LLM_RETRY_MAX", "3")
    get_settings.cache_clear()

    def boom():
        raise RuntimeError("413 Request Entity Too Large")

    with pytest.raises(RuntimeError, match="413"):
        llm._create_with_retry(boom, label="chat")


def test_is_retryable_5xx():
    err = llm.APIStatusError(
        "server error",
        response=MagicMock(status_code=503, headers={}),
        body=None,
    )
    assert llm._is_retryable_llm_error(err) is True
    err413 = RuntimeError("413 Request Entity Too Large")
    assert llm._is_retryable_llm_error(err413) is False
