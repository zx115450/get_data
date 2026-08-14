"""parse_builtin_checker_from_plan：容忍反引号/跨行。"""
from __future__ import annotations

from runners.prompts import parse_builtin_checker_from_plan


class TestParseBuiltinCheckerFromPlan:
    def test_plain_colon(self):
        assert parse_builtin_checker_from_plan(
            "应使用内置 checker: rcmp6"
        ) == "rcmp6"

    def test_backticks_like_job_plan(self):
        # 复现 20260813_170709：反引号导致旧正则漏匹配
        plan = """## 1. 判定类型
唯一答案比对（每组输出两个浮点数，答案唯一）。

## 2. 模板选型
应使用内置 checker: `rcmp6`（EPS=1e-6）。
理由：题目要求误差不超过 1e-6，输出为两行浮点数，答案唯一，无需自定义 SPJ。

## 6. 实现思路 · 核心
1. 使用 `rcmp6` 内置 checker，无需编写自定义代码。
"""
        assert parse_builtin_checker_from_plan(plan) == "rcmp6"

    def test_cross_line_selection_fallback(self):
        plan = "## 2. 模板选型\n应使用内置 checker: `wcmp`\n无需自定义。"
        assert parse_builtin_checker_from_plan(plan) == "wcmp"

    def test_chinese_colon_and_quotes(self):
        assert parse_builtin_checker_from_plan(
            "应使用内置 checker：'rcmp4'"
        ) == "rcmp4"

    def test_builtin_equals(self):
        assert parse_builtin_checker_from_plan(
            "builtin checker: yesno\n无需自定义"
        ) == "yesno"

    def test_custom_spj_returns_none(self):
        plan = """## 2. 模板选型
construct_verify

## 6. 实现思路
验证构造方案合法性，不要用内置 token 比对。
"""
        assert parse_builtin_checker_from_plan(plan) is None

    def test_empty(self):
        assert parse_builtin_checker_from_plan("") is None
        assert parse_builtin_checker_from_plan(None) is None  # type: ignore[arg-type]
