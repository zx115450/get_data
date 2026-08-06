"""agent/llm.py：parse_tool_arguments / TokenUsage / _truncate。"""
from __future__ import annotations

import json

import pytest

from agent.llm import (
    TokenUsage,
    _truncate,
    parse_tool_arguments,
)


class TestParseToolArguments:
    def test_valid_json(self):
        assert parse_tool_arguments('{"content":"hello","path":"a.cpp"}') == {
            "content": "hello",
            "path": "a.cpp",
        }

    def test_empty(self):
        assert parse_tool_arguments("") == {}
        assert parse_tool_arguments("   ") == {}

    def test_json_string_as_content(self):
        assert parse_tool_arguments('"just a string"') == {"content": "just a string"}

    @pytest.mark.parametrize(
        "raw,expect_substr",
        [
            # 截断在 content 中间
            ('{"content":"#include <bits/stdc++.h>\\nint main(){', "#include"),
            ('{"scheme_id":"s1","content":"registerGen(argc,argv,1);\\n', "registerGen"),
        ],
    )
    def test_truncated_json_recovers_content(self, raw, expect_substr):
        args = parse_tool_arguments(raw)
        assert args.get("_args_recovered") is True
        assert expect_substr in (args.get("content") or "")

    def test_truncated_recovers_scheme_id(self):
        raw = '{"scheme_id":"edge_chain","content":"#include <bits/stdc++.h>\\nint x='
        args = parse_tool_arguments(raw)
        assert args.get("scheme_id") == "edge_chain"
        assert args.get("_args_recovered") is True

    def test_raw_cpp_without_json(self):
        raw = "#include <bits/stdc++.h>\nint main(){registerGen(argc,argv,1);}"
        args = parse_tool_arguments(raw)
        assert args.get("_args_recovered") is True
        assert "registerGen" in args["content"]

    def test_unrecoverable_returns_preview(self):
        args = parse_tool_arguments("{not json at all!!!")
        assert args.get("_args_parse_failed") is True
        assert "_raw_args_preview" in args


class TestTokenUsage:
    def test_add_chat_and_as_dict(self):
        u = TokenUsage()

        class Fake:
            prompt_tokens = 100
            completion_tokens = 50
            total_tokens = 150

        u.add_chat(Fake())
        u.add_chat(Fake())
        d = u.as_dict()
        assert d["prompt_tokens"] == 200
        assert d["completion_tokens"] == 100
        assert d["total_tokens"] == 300
        assert d["chat_calls"] == 2

    def test_add_chat_none_safe(self):
        u = TokenUsage()
        u.add_chat(None)
        assert u.chat_calls == 0

    def test_add_embedding(self):
        u = TokenUsage()

        class Fake:
            prompt_tokens = 32
            total_tokens = 32

        u.add_embedding(Fake())
        assert u.embedding_tokens == 32
        assert u.embedding_calls == 1

    def test_format_brief(self):
        u = TokenUsage(prompt_tokens=1, completion_tokens=2, total_tokens=3, chat_calls=1)
        s = u.format_brief()
        assert "prompt=1" in s
        assert "calls=1" in s


class TestTruncate:
    def test_short_unchanged(self):
        assert _truncate("abc", 10) == "abc"

    def test_none_to_empty(self):
        assert _truncate(None, 10) == ""  # type: ignore[arg-type]

    def test_long_has_marker(self):
        s = "x" * 200
        out = _truncate(s, 50)
        assert "truncated" in out
        assert len(out) < 200

    def test_error_keeps_more_tail(self):
        body = "ok head\n" + ("m" * 100) + "\nERROR: boom at end"
        out = _truncate(body, 60)
        assert "ERROR" in out or "boom" in out
