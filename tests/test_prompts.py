"""agent/prompts：按题型/阶段拼装。"""
from __future__ import annotations

from agent import prompts


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

    def test_default_full_prompt_cached(self):
        assert isinstance(prompts.default_full_prompt, str)
        assert len(prompts.default_full_prompt) > 50
