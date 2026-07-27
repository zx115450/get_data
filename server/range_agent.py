"""单独生成 range.json（第一步），供 GUI 预览后再决定是否开跑全流程。"""
from __future__ import annotations

import json
import tempfile
import uuid
from pathlib import Path

from agent import tools
from agent.core import run as agent_run
from agent.tools import _schema
from pipeline.gen_data import normalize_range_json, validate_range_json
from server.struct_hints import scan_structural_hints, scan_structural_titles
from utils.markup import to_plain_for_llm

RANGE_ONLY_PROMPT = """你是出题数据规划助手。任务：根据题面与数据范围描述，只产出一份 range.json。

可用工具：
- write_range(content): 写入 range.json（合法 JSON 字符串）
- finish(summary): 写完并确认合法后调用

range.json 必须含：
- count: 正整数，默认 15
- constraints: 对象，变量名 -> [min, max]（整数）
- edge_cases: 字符串数组（边界类型名，禁止含 "random"）
- special_constraints: 字符串数组，列出题面里所有「特殊结构约束」（如 DAG、连通、二分图、哈密顿、欧拉、平面图、竞赛图、树等）。
  没有特殊约束时写空数组 []。每条用简短中文描述，如 "图是 DAG"、"图必须存在哈密顿路径"、"图连通"。
可选：
- time_limit_ms: 正整数（毫秒），标程时限
- memory_limit_mb: 正整数（MB），会限制 gen/validator/std 进程内存；题面有内存限制时务必填写

【提取 special_constraints 的方法 — 极重要】
1. 仔细读题面，找出所有「保证」「约定」「满足...」「是 X 图」「存在...」等结构性质描述。
2. 把每条性质提炼成一句简短中文，写进 special_constraints。
3. 对每条 special_constraints，必须在 edge_cases 里加一个对应的边界类型，
   命名要能体现该约束（如 "DAG" -> edge_dag_chain / edge_dag_extra；
   "哈密顿" -> edge_hamiltonian_chain / edge_hamiltonian_extra；
   "连通" -> edge_connected_tree / edge_connected_dense）。
4. special_constraints 不只是抄题面关键词：要判断它对生成器意味着什么。
   例如「求哈密顿路径数量」隐含「图必须存在哈密顿路径」，生成器要保证这一点，
   否则标程答案无意义——这种隐含约束也要写进 special_constraints。

规则：
1. 只调用 write_range，不要写 gen/validator，不要编造测例正文。
2. edge_cases 要覆盖最小/最大/典型边界；多测 T 时建议含 edge_T1、edge_Tmax 等。
3. edge_cases 必须覆盖 special_constraints 里每一条约束对应的边界。
4. write_range 成功后立刻 finish。
5. 看到 ERROR 要修正后再 write_range。
"""


# 按题型的 edge_cases 建议。range 规划阶段就给模型一份「该题型通常要覆盖哪些边界」的清单，
# 避免模型只写 random + edge_n1/edge_nmax 这种最浅的边界。
_TYPE_EDGE_HINTS = {
    "array": [
        "edge_n1", "edge_nmax", "all_equal", "descending", "all_negative",
        "all_max_value", "two_values",
    ],
    "tree": [
        "edge_n2", "edge_nmax", "chain", "star", "balanced_binary", "random_tree",
        "flower_chain", "caterpillar", "broom",
    ],
    "graph": [
        "edge_n1", "edge_nmax", "edge_m_min", "edge_m_max",
        "connected_tree", "disconnected", "complete", "path", "star",
        "random_sparse", "random_dense", "bipartite", "dag_acyclic",
    ],
    "string": [
        "edge_n1", "edge_nmax", "all_same", "pattern_at_start", "pattern_at_end",
        "no_match", "long_run", "two_chars",
    ],
    "number_theory": [
        "edge_n1", "edge_nmax", "all_equal", "all_prime", "coprime_pair",
        "all_even", "include_one", "all_max_value",
    ],
    "geometry": [
        "edge_n3", "edge_nmax", "convex_hull", "simple_polygon",
        "random_points", "collinear", "same_x",
    ],
    "multi_test": [
        "edge_T1", "edge_Tmax", "edge_n_min", "edge_nmax",
        "big_T_small_n", "small_T_big_n", "single_max_case",
    ],
    "dp": [
        "edge_n1", "edge_nmax", "edge_W1", "edge_Wmax", "all_heavy", "all_light",
    ],
    "matrix": [
        "edge_11", "edge_nmax", "row", "col", "all_zero", "all_max",
    ],
    "range_query": [
        "edge_n1", "edge_nmax", "q1", "qmax", "point_queries", "full_range",
    ],
    "weighted_tree": [
        "edge_n2", "edge_nmax", "chain", "star", "random_tree", "heavy_weights",
    ],
    "weighted_graph": [
        "connected_tree", "path", "star", "random_sparse", "edge_n1", "dense",
    ],
    "interactive": [
        "edge_n1", "edge_nmax", "q1", "qmax", "repeat_ask",
    ],
}

_TYPE_HINT_HEADER = {
    "array": "数组/序列题：建议 edge_cases 覆盖以下边界（按需挑选，不要全抄）",
    "tree": "树题：建议 edge_cases 覆盖以下边界（按需挑选，不要全抄）。生成优先用 generator.h 的 Tree/Chain/Flower",
    "graph": "图题：建议 edge_cases 覆盖以下边界（按需挑选，不要全抄）。注意图题还要根据题意补结构边界（如 DAG 题加 dag_acyclic，连通题加 connected/disconnected）；优先用 Graph/BipartiteGraph/DAG 等 API",
    "string": "字符串题：建议 edge_cases 覆盖以下边界（按需挑选，不要全抄）",
    "number_theory": "数论题：建议 edge_cases 覆盖以下边界（按需挑选，不要全抄）",
    "geometry": "几何题：建议 edge_cases 覆盖以下边界（按需挑选，不要全抄）。优先用 ConvexHull/SimplePolygon/RandomPoints",
    "multi_test": "多测题：建议 edge_cases 覆盖以下边界（按需挑选，不要全抄）",
    "dp": "DP 题：建议覆盖规模边界与退化背包/转移情形",
    "matrix": "矩阵题：建议覆盖 1×1、满规模、单行/单列",
    "range_query": "区间查询题：建议覆盖 n/q 极值与点询/整段询",
    "weighted_tree": "带权树：优先 edge_weight::Tree/Chain/Flower",
    "weighted_graph": "带权图：树骨架 + 稀疏随机边，注意边权范围",
    "interactive": "交互/询问序列（离线）：本工具只生成询问文件，不能替代真正 interactor",
}


def _build_type_hint_block(problem_type: str) -> str:
    """根据题型拼一段 edge_cases 建议文本，拼到 task 里。未知题型返回空串。"""
    if not problem_type or problem_type not in _TYPE_EDGE_HINTS:
        return ""
    header = _TYPE_HINT_HEADER.get(problem_type, "")
    examples = _TYPE_EDGE_HINTS[problem_type]
    joined = ", ".join(examples)
    return (
        f"\n\n【题型 edge_cases 建议（{problem_type}）】\n"
        f"{header}：\n{joined}\n"
        f"这些只是建议清单，最终 edge_cases 必须与题面/数据范围一致；"
        f"若题面有特殊结构约束（如「图是 DAG」「图连通」「存在哈密顿路径」），"
        f"必须额外加一个对应边界（如 dag_acyclic / connected / has_hamiltonian）"
        f"并在 gen 里真正保证该性质。\n"
    )

RANGE_TOOL_SCHEMAS = [
    _schema(
        "write_range",
        "写入 range.json。content 为完整 JSON：count、constraints、edge_cases（不要含 random）。",
        {"content": {"type": "string", "description": "range.json 完整 JSON 字符串"}},
        ["content"],
    ),
    _schema(
        "finish",
        "range.json 已写好后调用结束。",
        {"summary": {"type": "string", "description": "简述"}},
        [],
    ),
]


def propose_range_json(
    problem_statement: str,
    data_range_desc: str,
    problem_type: str = "",
    std_code: str = "",
    lang: str = "cpp",
) -> dict:
    """调 LLM 只生成 range.json，返回清洗并校验后的 dict（不含 std_cmd）。"""
    stmt = to_plain_for_llm(problem_statement)
    rng = to_plain_for_llm(data_range_desc)
    work = Path(tempfile.gettempdir()) / f"acm_range_{uuid.uuid4().hex[:10]}"
    work.mkdir(parents=True, exist_ok=True)
    tools.set_context(str(work), std_cmd="")

    std_hint = ""
    if std_code and std_code.strip():
        code = std_code.strip()
        if len(code) > 4000:
            code = code[:2000] + "\n/* ... */\n" + code[-1500:]
        std_hint = f"\n\n【标程片段 lang={lang}，仅供推断是否有多测 T】\n```\n{code}\n```\n"

    # 扫题面关键词：命中特殊结构约束时，给 LLM 两份帮助：
    # 1) 一段针对性提醒文本（scan_structural_hints），告诉 LLM 该约束对生成器意味着什么；
    # 2) 一份预扫描出的约束标题清单（scan_structural_titles），让 LLM 把它们写进 special_constraints。
    struct_hint_block = scan_structural_hints(stmt) or scan_structural_hints(problem_statement or "")
    pre_titles = scan_structural_titles(stmt) or scan_structural_titles(problem_statement or "")
    pre_titles_block = ""
    if pre_titles:
        pre_titles_block = (
            "\n\n【预扫描到的特殊结构约束（请据此填写 special_constraints）】\n"
            + "\n".join(f"- {t}" for t in pre_titles)
            + "\n请确认这些约束确实出现在题面里（不要凭空加），"
            "并补充题面里其它未被预扫描到的隐含约束。每条都要在 edge_cases 里加对应边界。\n"
        )

    task = (
        f"请只产出 range.json。\n\n"
        f"【题面】\n{stmt}\n\n"
        f"【数据范围描述】\n{rng}\n"
        f"{std_hint}"
        f"{_build_type_hint_block(problem_type)}"
        f"{pre_titles_block}"
        f"{struct_hint_block}"
        f"\ncount 默认 15。constraints 覆盖题面中的规模变量（如 n、T、m）。"
        f"edge_cases 用简短英文标识符。写完 write_range 后 finish。"
        f"务必填写 special_constraints 字段（即使为空数组也要写）。\n"
    )
    summary = agent_run(
        task,
        max_steps=8,
        verbose=False,
        system_prompt=RANGE_ONLY_PROMPT,
        tool_schemas=RANGE_TOOL_SCHEMAS,
    )
    path = work / "range.json"
    if not path.exists():
        raise RuntimeError(f"未能生成 range.json（Agent: {summary}）")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise RuntimeError(f"range.json 不是合法 JSON: {e}") from e

    data = normalize_range_json(dict(data))
    data.pop("std_cmd", None)
    errs = validate_range_json(data)
    if errs:
        raise RuntimeError("range.json 不合法:\n" + "\n".join(f"  - {e}" for e in errs))
    return data
