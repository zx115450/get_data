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

    def test_planner_axis_hard_gate(self):
        from agent.prompts.core import SCALE, PERF, BASE_GEN_RULES_CORE

        p = prompts.build_planner_prompt()
        assert "(index/3)%3" in p or "(i / 3) % 3" in p
        assert "规模轴必须" in SCALE
        assert "先核对" in PERF or "先修解轴" in PERF
        assert "解轴 · 硬" in BASE_GEN_RULES_CORE
        assert "规模轴写成" in p

    def test_unique_capped_by_domain(self):
        from agent.prompts.core import SCALE, BASE_GEN_RULES_CORE, PERF

        p = prompts.build_planner_prompt()
        assert "域基数" in SCALE or "域大小" in SCALE
        assert "uni" in BASE_GEN_RULES_CORE.lower() or "唯一状态数" in BASE_GEN_RULES_CORE
        assert "域基数" in BASE_GEN_RULES_CORE or "域大小" in BASE_GEN_RULES_CORE
        assert "域基数" in PERF or "hi-lo" in PERF
        assert "域基数" in p or "min(目标" in p

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

    def test_string_prompt_no_bleed_tree_manual(self):
        p = prompts.build_coder_prompt("string")
        # 字符串题型不应注入树专属的 FlowerChain 等详细 API
        assert "unweight::FlowerChain" not in p
        assert "set_flower_size" not in p

    def test_tree_prompt_has_tree_module(self):
        p = prompts.build_coder_prompt("tree")
        assert "unweight::Tree" in p
        assert "Flower" in p

    def test_new_type_modules_injected(self):
        markers = {
            "number_theory": "素数",
            "dp": "背包",
            "range_query": "区间查询",
            "multi_test": "多测",
            "interactive": "交互",
        }
        for typ, marker in markers.items():
            p = prompts.build_coder_prompt(typ)
            assert len(p) > 100
            assert marker in p, f"{typ} prompt missing marker {marker}"

    def test_type_modules_cover_new_types(self):
        from agent.prompts.types import _TYPE_MODULES
        new_types = {"number_theory", "dp", "range_query", "multi_test", "interactive", "permutation"}
        assert new_types.issubset(set(_TYPE_MODULES.keys()))
        for typ in new_types:
            assert _TYPE_MODULES[typ], f"{typ} module is empty"

    def test_coder_prompt_multi_type(self):
        p = prompts.build_coder_prompt(["tree", "multi_test"])
        assert "unweight::Tree" in p, "应注入树模块"
        assert "多测" in p, "应注入多测模块"
        # 去重：重复类型只追加一次
        p2 = prompts.build_coder_prompt(["tree", "tree"])
        assert p2 == prompts.build_coder_prompt("tree")

    def test_full_prompt_multi_type(self):
        p = prompts.build_full_prompt(["graph", "dp"])
        assert "unweight::Graph" in p or "DAG" in p, "应注入图模块"
        assert "背包" in p, "应注入 DP 模块"

    def test_normalize_problem_types(self):
        from knowledge.few_shots import normalize_problem_types
        assert normalize_problem_types("tree, multi_test") == ["tree", "multi_test"]
        assert normalize_problem_types(["tree", "multi_test"]) == ["tree", "multi_test"]
        assert normalize_problem_types("tree") == ["tree"]
        assert normalize_problem_types("unknown, tree") == ["tree"]
