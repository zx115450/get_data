"""Prompt assembly (build_* APIs)."""
from __future__ import annotations

from .core import (
    BASE_GEN_RULES,
    BASE_GEN_RULES_CORE,
    BASE_VAL_RULES,
    CLI_CONTRACT,
    COMMON_CORE,
    MULTI_TEST,
    PERF,
    RANGE_CONTRACT,
    RANGE_ONLY_CORE,
    RULES,
    SCALE,
    TOOLS_FULL,
    TOOLS_RANGE,
    WORKFLOW,
    WRITE_CONTENT_GATE,
)
from .stages import (
    CHECKER_CODER_PROMPT,
    CHECKER_PLANNER_PROMPT,
    CODER_PROMPT,
    CODER_REWRITE_PROMPT,
    FIXER_CORE,
    FIXER_RULES,
    FIXER_TOOLS,
    FIXER_WORKFLOW,
    GEN_FIXER_CORE,
    GEN_FIXER_RULES,
    GEN_FIXER_TOOLS,
    GEN_FIXER_WORKFLOW,
    PLANNER_PROMPT,
    REVIEWER_CORE,
    REVIEWER_RULES,
    REVIEWER_TOOLS,
    REVIEWER_WORKFLOW,
    SPECIAL_CODER_PROMPT,
    SPECIAL_FIXER_PROMPT,
    _GEN_API_GATE,
    _GEN_API_GATE_BY_TYPE,
)
from .types import _TYPE_MODULES

def _needs_perf(problem_type: str, range_json: dict | None) -> bool:
    """是否注入性能硬约束模块。

    默认对树图、矩阵、大范围题启用；数组题若范围小可关闭。
    """
    if problem_type in ("tree", "weighted_tree", "graph", "weighted_graph", "geometry", "matrix", "range_query", "dp"):
        return True
    if range_json is None:
        return False
    cons = range_json.get("constraints") or {}
    for v in cons.values():
        if isinstance(v, (list, tuple)) and len(v) == 2 and v[1] >= 100000:
            return True
    return False


def _needs_multi(range_json: dict | None, problem_statement: str = "", std_code: str = "") -> bool:
    """是否注入多测 + sum 约束模块。

    检测：题型 multi_test，或 constraints 含 T/t + sum_*，或题面/标程有 T + sum 描述。
    """
    if range_json is None:
        return False
    if range_json.get("problem_type") == "multi_test":
        return True
    cons = range_json.get("constraints") or {}
    cons_keys = {str(k).lower() for k in cons}
    has_t = "t" in cons_keys
    has_sum_cons = any("sum" in k for k in cons_keys)
    if has_t and has_sum_cons:
        return True
    text = ((problem_statement or "") + "\n" + (std_code or "")).lower()
    has_sum = any(k in text for k in ("sum", "total", "σ", "sigma"))
    return has_t and has_sum


def build_full_prompt(
    problem_type: str = "",
    *,
    range_json: dict | None = None,
    problem_statement: str = "",
    std_code: str = "",
) -> str:
    """组装 gen/validator 阶段的完整 System Prompt。

    Args:
        problem_type: 题型，如 tree/graph/array/geometry 等。
        range_json: 已规划好的 range.json（可能为 None）。
        problem_statement: 题面文本，用于多测检测。
        std_code: 标程源码，用于多测检测。
    """
    parts = [
        COMMON_CORE,
        WRITE_CONTENT_GATE,
        TOOLS_FULL,
        WORKFLOW,
        CLI_CONTRACT,
        SCALE,
    ]

    if _needs_multi(range_json, problem_statement, std_code):
        parts.append(MULTI_TEST)

    if _needs_perf(problem_type, range_json):
        parts.append(PERF)

    parts.append(BASE_GEN_RULES)
    parts.append(BASE_VAL_RULES)

    type_module = _TYPE_MODULES.get(problem_type, "")
    if type_module:
        parts.append(type_module)

    parts.append(RULES)
    return "\n\n".join(parts)


def build_range_prompt() -> str:
    """返回 range 规划阶段的短 System Prompt（保持与 range_agent 对齐）。"""
    from knowledge.few_shots import PROBLEM_TYPE_RANGE_HINT

    return "\n\n".join([
        RANGE_ONLY_CORE,
        TOOLS_RANGE,
        RANGE_CONTRACT,
        PROBLEM_TYPE_RANGE_HINT.strip(),
        "edge_cases 要覆盖最小/最大/典型边界；"
        "仅当 constraints 含 T/t 时才写 edge_Tmax（不要写 edge_T1）；无多测禁止写。",
        "只允许 write_range 与 finish；不要读文件。"
        "无已有 range 或判定需重写时：write_range 成功后立刻 finish，不要重复 write_range。"
        "判定可复用时：禁止 write_range，直接 finish。"
        "看到 ERROR 要修正后再 write_range。",
        RULES,
    ])

def build_planner_prompt() -> str:
    """返回 Planner 阶段（单次纯文本）的 System Prompt。

    始终附带 SCALE + MULTI_TEST + PERF：小中大全组合、gen 5s 硬时限、以及 std 有效状态预算 K。
    """
    return "\n\n".join([PLANNER_PROMPT, SCALE, MULTI_TEST, PERF])

def _with_type_modules(base_parts: list[str], problem_type: str = "") -> str:
    """在通用规则后追加题型门禁片段 + 题型模块（tree/graph 等）。"""
    parts = list(base_parts)
    pt = problem_type or ""
    gate_extra = _GEN_API_GATE_BY_TYPE.get(pt, "")
    if gate_extra:
        parts.append(gate_extra)
    type_module = _TYPE_MODULES.get(pt, "")
    if type_module:
        parts.append(type_module)
    return "\n\n".join(parts)


def build_coder_prompt(problem_type: str = "") -> str:
    """返回 Coder 阶段（硬自检）的 System Prompt。

    不注入 BASE_GEN_API_MANUAL：CODER_PROMPT 已含 _GEN_API_GATE，另附 TYPE_*。
    """
    return _with_type_modules(
        [WRITE_CONTENT_GATE, CODER_PROMPT, PERF, BASE_GEN_RULES_CORE, BASE_VAL_RULES],
        problem_type,
    )


def build_coder_rewrite_prompt(problem_type: str = "") -> str:
    """返回 Coder Rewrite（骨架重写）阶段的 System Prompt。"""
    return _with_type_modules(
        [WRITE_CONTENT_GATE, CODER_REWRITE_PROMPT, PERF, BASE_GEN_RULES_CORE, BASE_VAL_RULES],
        problem_type,
    )

def build_checker_planner_prompt() -> str:
    """返回 Checker Planner 阶段（纯文本）的 System Prompt。"""
    return CHECKER_PLANNER_PROMPT


def build_checker_coder_prompt() -> str:
    """返回 Checker Coder 阶段（带工具）的 System Prompt。"""
    return CHECKER_CODER_PROMPT


def build_checker_prompt() -> str:
    """兼容旧名：等同 Checker Coder System Prompt。"""
    return build_checker_coder_prompt()

def build_reviewer_prompt() -> str:
    """返回 Reviewer Agent 的 System Prompt。"""
    return "\n\n".join([
        REVIEWER_CORE,
        REVIEWER_TOOLS,
        REVIEWER_WORKFLOW,
        REVIEWER_RULES,
    ])

def build_fixer_prompt() -> str:
    """返回 Fixer Agent 的 System Prompt。"""
    return "\n\n".join([
        FIXER_CORE,
        FIXER_TOOLS,
        FIXER_WORKFLOW,
        FIXER_RULES,
    ])

def build_gen_fixer_prompt(problem_type: str = "") -> str:
    """返回阶段 2（Gen Agent 自检失败后）Fixer Agent 的 System Prompt。

    已含 _GEN_API_GATE + TYPE_*，只附 BASE_GEN_RULES_CORE，避免与手册重复。
    """
    return _with_type_modules(
        [
            WRITE_CONTENT_GATE,
            GEN_FIXER_CORE,
            _GEN_API_GATE,
            GEN_FIXER_TOOLS,
            GEN_FIXER_WORKFLOW,
            GEN_FIXER_RULES,
            BASE_GEN_RULES_CORE,
        ],
        problem_type,
    )


def build_special_coder_prompt(problem_type: str = "") -> str:
    """返回 SpecialCoder 阶段的 System Prompt。"""
    return _with_type_modules(
        [SPECIAL_CODER_PROMPT, BASE_GEN_RULES],
        problem_type,
    )


def build_special_fixer_prompt(problem_type: str = "") -> str:
    """返回 Special Fixer 阶段的 System Prompt。"""
    return _with_type_modules(
        [SPECIAL_FIXER_PROMPT, _GEN_API_GATE, BASE_GEN_RULES_CORE],
        problem_type,
    )


# 兼容旧入口：默认的完整 prompt（≈原 SYSTEM_PROMPT）
# 注意：现在默认仍包含 write_checker/use_builtin_checker，仅用于不拆 checker 的旧调用。
default_full_prompt = build_full_prompt()
