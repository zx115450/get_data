"""agent/tools 纯逻辑参数校验（不编译）。"""
from __future__ import annotations

from agent.tools import dispatch


def test_write_gen_missing_content():
    out = dispatch("write_gen", {})
    assert out.startswith("ERROR")
    assert "content" in out.lower() or "参数" in out


def test_write_gen_parse_failed_flag():
    out = dispatch(
        "write_gen",
        {"_args_parse_failed": True, "_raw_args_preview": "{bad"},
    )
    assert out.startswith("ERROR")


def test_unknown_tool():
    out = dispatch("not_a_real_tool", {})
    assert "ERROR" in out or "未知" in out or "unknown" in out.lower()
