"""agent/prompts：按题型/阶段拼装。"""
from __future__ import annotations

from agent import prompts
from agent.prompts.types import TYPE_STRING


class TestPromptAssembly:
    def test_range_prompt(self):
        p = prompts.build_range_prompt()
        assert "range.json" in p
        assert "write_range" in p or "finish" in p

    def test_coder_prompt_includes_type(self):
        p = prompts.build_coder_prompt("tree")
        assert len(p) > 100
        # 树题型模块应出现
        assert "tree" in p.lower() or "树" in p or "gen" in p.lower()

    def test_planner_prompt(self):
        p = prompts.build_planner_prompt()
        assert "plan" in p.lower() or "计划" in p or "SCALE" in p or "分层" in p

    def test_full_prompt_has_tools(self):
        p = prompts.build_full_prompt()
        assert "write_gen" in p
        assert "finish" in p

    def test_planner_special_constraints_gate(self):
        p = prompts.build_planner_prompt()
        assert "special_constraints" in p
        assert "优先于状态密度" in p or "不得删掉任一必含模式" in p
        assert "违反 special_constraints" in p or "漏掉" in p
        assert "定长拼装" in p
        assert "各段长度之和" in p or "可核对等式" in p

    def test_coder_fixed_length_assembly(self):
        from agent.prompts.core import BASE_GEN_RULES_CORE

        p = prompts.build_coder_prompt("string")
        assert "定长拼装" in p or "定长拼装" in BASE_GEN_RULES_CORE
        assert "L -" in p or "L-" in BASE_GEN_RULES_CORE or "L - (int)" in BASE_GEN_RULES_CORE
        assert "多模式植入" in p or "先 rnd p1" in p

    def test_type_string_bans_sequential_pool(self):
        assert "先采再滤" in TYPE_STRING
        assert "一次枚举" in TYPE_STRING
        assert "pool" in TYPE_STRING
        assert "n must be positive" in TYPE_STRING or "崩溃" in TYPE_STRING
