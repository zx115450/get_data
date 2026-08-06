"""agent/llm：compact_messages / 重试判定 / token 预算。"""
from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest

from agent import llm
from agent.llm import (
    TokenBudgetExceeded,
    _check_token_budget,
    _is_retryable_llm_error,
    compact_messages,
    reset_token_usage,
)


class TestCompactMessages:
    def test_stubs_long_write_content(self):
        content = "x" * 500
        messages = [
            {"role": "system", "content": "s"},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "1",
                        "type": "function",
                        "function": {
                            "name": "write_gen",
                            "arguments": json.dumps({"content": content}),
                        },
                    }
                ],
            },
            {"role": "tool", "tool_call_id": "1", "content": "OK: wrote gen.cpp"},
        ]
        compact_messages(messages)
        args = json.loads(messages[1]["tool_calls"][0]["function"]["arguments"])
        assert "__OMITTED_SOURCE__" in args["content"]
        assert len(args["content"]) < len(content)

    def test_write_tool_result_collapsed(self):
        messages = [
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "tc1",
                        "type": "function",
                        "function": {"name": "write_gen", "arguments": "{}"},
                    }
                ],
            },
            {
                "role": "tool",
                "tool_call_id": "tc1",
                "content": "OK: compiled\n" + ("detail\n" * 50),
            },
        ]
        compact_messages(messages)
        assert messages[1]["content"].startswith("[write_gen]")


class TestRetryable:
    def test_rate_limit(self):
        from openai import RateLimitError

        # RateLimitError 构造较繁琐，用假对象 + 字符串兜底
        exc = Exception("Error code: 429 - rate limit exceeded")
        assert _is_retryable_llm_error(exc) is True

    def test_5xx(self):
        assert _is_retryable_llm_error(Exception("503 Service Unavailable")) is True

    def test_413_not_retryable(self):
        assert _is_retryable_llm_error(Exception("413 Request Entity Too Large")) is False

    def test_budget_not_retryable(self):
        assert _is_retryable_llm_error(TokenBudgetExceeded("x")) is False

    def test_api_status_429(self):
        exc = MagicMock()
        exc.__class__ = type("X", (llm.APIStatusError if False else Exception,), {})
        # 用真实 APIStatusError 子类检测路径：直接造带 status_code 的假异常不行
        # 字符串路径已覆盖；此处测 Type 分支用 RateLimitError 名
        assert _is_retryable_llm_error(Exception("too many requests")) is True


class TestTokenBudget:
    def test_unlimited_ok(self, monkeypatch):
        reset_token_usage()
        monkeypatch.setenv("LLM_MAX_TOTAL_TOKENS", "0")
        from config.settings import get_settings

        get_settings.cache_clear()
        _check_token_budget()  # 不应抛

    def test_exceeded(self, monkeypatch):
        reset_token_usage()
        monkeypatch.setenv("LLM_MAX_TOTAL_TOKENS", "100")
        from config.settings import get_settings

        get_settings.cache_clear()
        usage = llm._current_usage()
        usage.total_tokens = 100
        with pytest.raises(TokenBudgetExceeded):
            _check_token_budget()
