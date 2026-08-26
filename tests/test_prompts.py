"""agent/prompts：按题型/阶段拼装。"""
from __future__ import annotations

from agent import prompts
from agent.prompts.types import TYPE_STRING


class TestPromptAssembly:
    def test_range_prompt(self):
        p = prompts.build_range_prompt()
        assert "range.json" in p
        assert "write_range" in p or "finish" in p
        assert "每步最多一次" in p
        assert "可反复" in p or "下一轮" in p

    def test_coder_include_namespace_pair(self):
        p = prompts.build_coder_prompt("array")
        assert '#include "testlib.h" + using namespace std' in p
        assert '#include "generator.h" + using namespace generator::all' in p
        assert "必须成对" in p
        assert "禁止混" in p
        r = prompts.build_coder_rewrite_prompt("array")
        assert "必须成对" in r
        assert "generator::all" in r

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

    def test_planner_bans_fake_rnd_next_api(self):
        """任意题型：Planner 不得在 plan 写三参数 rnd.next 伪 API。"""
        p = prompts.build_planner_prompt()
        assert "伪 API" in p or "自造重载" in p
        assert "rnd.next(lo, hi, k)" in p or "rnd.next(lo,hi,k)" in p
        assert "禁止自造第三参数" in p or "合法签名" in p
        assert "三参数数值伪 API" in p

    def test_coder_system_api_beats_plan_pseudo(self):
        """Coder：系统 API 优先于 plan 伪代码（通用）。"""
        p = prompts.build_coder_prompt("array")
        assert "系统 API 优先" in p
        assert "rnd.next(lo,hi,k)" in p or "rnd.next(lo, hi, k)" in p

    def test_coder_dual_artifact_discipline(self):
        p = prompts.build_coder_prompt("array")
        assert "双产物" in p
        assert "缺少 validator" in p or "write_validate" in p
        assert "禁止为「再优化」重写" in p or "已编译成功的 gen" in p

    def test_decimals_gate_allows_two_arg_scaling(self):
        from agent.prompts.core import BASE_GEN_RULES_CORE

        assert "禁止误解" in BASE_GEN_RULES_CORE
        assert "不等于禁止两参数" in BASE_GEN_RULES_CORE
        assert "rnd.next(0, 100, 1)" in BASE_GEN_RULES_CORE or "rnd.next(lo, hi, k)" in BASE_GEN_RULES_CORE

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

    def test_coder_opt_template_wraps_main(self):
        """固定样板须显式 int main，避免全局 registerGen。"""
        p = prompts.build_coder_prompt("array")
        assert "int main(int argc, char* argv[])" in p
        assert "return 0;" in p
        # 样板注释里应提示勿把 Agent 工具写进源码
        assert "finish" in p and "Agent" in p

    def test_rnd_next_api_card_in_coder_and_fixer(self):
        from agent.prompts.core import RND_NEXT_API_CARD

        coder = prompts.build_coder_prompt("array")
        fixer = prompts.build_gen_fixer_prompt("array")
        assert "rnd.next(lo, hi, decimals)" in coder or "禁止自造第三参数" in coder
        assert RND_NEXT_API_CARD[:20] in coder
        assert "禁止自造第三参数" in fixer or "rnd.next(0, 100, 1)" in fixer
        assert "整数缩放" in coder or "tenths" in coder

    def test_gen_fixer_prompt_missing_exe_precondition(self):
        p = prompts.build_gen_fixer_prompt("array")
        assert "gen 未编译" in p or "write_gen + write_validate" in p
        assert "局部" in p  # 禁止局部边角修补


class TestCoderTaskSkeleton:
    def test_non_tree_coder_task_has_universal_main_skeleton(self):
        from runners.agent import _build_coder_task

        task = _build_coder_task(
            stmt_plain="题面",
            range_plain="1<=n<=100",
            output_plain="一行浮点数",
            std_for_prompt="int main(){}",
            range_json={"count": 27, "constraints": {"n": {"type": "int", "min": 1, "max": 100}}, "edge_cases": ["edge_n1"]},
            eff_type="array",
            resume_failure_block="",
        )
        assert "【最短可编骨架 · 必遵守】" in task
        assert "int main(int argc, char* argv[])" in task
        assert "registerGen(argc, argv, 1)" in task
        assert "return 0;" in task
        assert "禁止在源码写 finish" in task or "finish 是 Agent 工具" in task
        assert "【最短可编骨架 · 树/图 · 必遵守】" not in task
        assert "系统 API" in task
        assert "rnd.next(lo,hi,k)" in task or "合法签名" in task

    def test_tree_coder_task_keeps_graph_skeleton(self):
        from runners.agent import _build_coder_task

        task = _build_coder_task(
            stmt_plain="树",
            range_plain="n",
            output_plain="",
            std_for_prompt="",
            range_json={"count": 15, "constraints": {}, "edge_cases": ["chain"]},
            eff_type="tree",
            resume_failure_block="",
        )
        assert "【最短可编骨架 · 树/图 · 必遵守】" in task
        assert '#include "generator.h"' in task
        assert "int main(int argc, char* argv[])" in task


class TestGenFixerMissingExe:
    def test_missing_exes_forces_full_rewrite_instructions(self, tmp_path):
        from runners.prompts import build_gen_fixer_task, missing_gen_validator_exes

        assert missing_gen_validator_exes(tmp_path)  # 空目录两者都缺
        task = build_gen_fixer_task(
            "题面",
            "范围",
            {"count": 9, "edge_cases": ["edge_n1"]},
            "ERROR: gen 未编译，请先 write_gen",
            attempt=1,
            max_attempts=4,
            job_dir=tmp_path,
        )
        assert "【前置条件 · 硬】" in task
        assert "write_gen(完整源码)" in task or "立刻 write_gen" in task
        assert "修边角" in task or "局部补丁" in task
        assert "int main(int argc, char* argv[])" in task

    def test_with_exes_keeps_patch_workflow(self, tmp_path):
        from runners.prompts import build_gen_fixer_task, _artifact_exe_name

        (tmp_path / _artifact_exe_name("gen")).write_bytes(b"x")
        (tmp_path / _artifact_exe_name("validator")).write_bytes(b"x")
        task = build_gen_fixer_task(
            "题面",
            "范围",
            {"count": 9, "edge_cases": ["edge_n1"]},
            "FAIL: validate type=edge_n1 ...",
            attempt=1,
            max_attempts=4,
            job_dir=tmp_path,
        )
        assert "【前置条件 · 硬】" not in task
        assert "根据失败日志修复" in task or "自检失败摘要" in task
        assert 'read_file("gen.cpp")' in task
