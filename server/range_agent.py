"""单独生成 range.json（第一步），供 GUI 预览后再决定是否开跑全流程。"""
from __future__ import annotations

import json
import tempfile
import uuid
from pathlib import Path

from agent import tools
from agent.core import run as agent_run
from agent.tools import _schema
from pipeline.gen_data import (
    MIN_REGULAR_COUNT,
    _clamp_regular_count,
    infer_regular_count,
    normalize_range_json,
    validate_range_json,
)
from knowledge.struct_hints import scan_structural_hints, scan_structural_titles
from utils.markup import to_plain_for_llm

RANGE_ONLY_PROMPT = """你是出题数据规划助手。任务：根据题面与数据范围描述，只产出一份 range.json。

可用工具：
- write_range(content): 写入 range.json（合法 JSON 字符串）
- finish(summary): 写完并确认合法后调用

range.json 必须含：
- problem_type: 题型标识符（与题面/标程匹配的英文枚举，见 task 里的可选项与分类原则）
- count: 正整数；count ≥ max(15, 3^k)，k=小中大轴数（三维即 ≥27；不要写「建议≥30」）
- constraints: 对象，变量名 -> [min, max]（整数）
- edge_cases: 字符串数组（边界类型名，禁止含 "random"；总额 4～6）
  【命名】约束极值统一 edge_ 前缀：edge_nmin / edge_nmax / edge_k_min / edge_m_min / edge_Tmax；
  禁止裸写 k_min / nmax / Tmax（易导致 gen 写成 edge_k_min 与 range 不一致）。
  结构边界可用语义名：chain / disconnected / path / star（可不带 edge_）。
  最终名字会原样作为 gen --type；Coder 必须逐字符一致，禁止自行加/删 edge_。
- special_constraints: 字符串数组，列出题面里所有「特殊结构约束」（如 DAG、连通、二分图、哈密顿、欧拉、平面图、竞赛图、树等）。
  没有特殊约束时写空数组 []。每条用简短中文描述，如 "图是 DAG"、"图必须存在哈密顿路径"、"图连通"。
可选（建议填写；未写时系统默认 time_limit_ms=5000、memory_limit_mb=1024）：
- time_limit_ms: 正整数（毫秒），标程时限；默认 5000
- memory_limit_mb: 正整数（MB）；默认 1024

【提取 special_constraints 的方法 — 极重要】
1. 仔细读题面，找出所有「保证」「约定」「满足...」「是 X 图」「存在...」等结构性质描述。
2. 把每条性质提炼成一句简短中文，写进 special_constraints。
3. 【与 edge 名额】尽量在 edge_cases（总额仍 4～6）里为关键结构约束各留一个边界名
   （如 "DAG" -> edge_dag；"哈密顿" -> edge_hamiltonian；约束极值用 edge_k_min 而非 k_min）；
   约束过多时合并同类或只保留最关键 2～3 条，禁止为「一条约束一个 edge」而超过 6。
4. special_constraints 不只是抄题面关键词：要判断它对生成器意味着什么。
   例如「求哈密顿路径数量」隐含「图必须存在哈密顿路径」，生成器要保证这一点，
   否则标程答案无意义——这种隐含约束也要写进 special_constraints。

规则：
1. 只调用 write_range，不要写 gen/validator，不要编造测例正文。
2. edge_cases 要覆盖最小/最大规模 + 关键结构边界，总数 4～6（不要超过 6）。
   【多测】仅当 constraints 含 T（或 t）时才写 edge_Tmax（可配 big_T_small_n）；
   【不要写 edge_T1】T=1 已被 edge_nmax / small_T_big_n / 攻 n 覆盖，无额外测点。
   无多测（EOF 读入 / 单组）禁止写 edge_Tmax / edge_T1。
3. edge_cases 优先占位：edge_n1/edge_nmax，其余名额给 special_constraints 的核心结构（可合并同类）。
   约束变量的最小/最大边界名必须带 edge_ 前缀（edge_k_min，禁止 k_min）。
4. write_range 成功后立刻 finish，不要重复 write_range。
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
        # 无脑勿加 edge_Tmax：仅当 constraints 含 T 时再加；不要写 edge_T1
        "edge_n1", "edge_nmax", "edge_m_min", "edge_m_max",
        "disconnected", "random_sparse",
        # 以下按题意选用，勿无脑全抄：connected_tree / path / star / complete /
        # bipartite / dag_acyclic / negative_cycle_reachable /
        # edge_Tmax / big_T_small_n（仅多测；不要 edge_T1）
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
        "edge_Tmax", "edge_n_min", "edge_nmax",
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
    "graph": (
        "图题：建议按需挑选下列边界，不要全抄。"
        "仅当 constraints 含 T 时才写 edge_Tmax（可配 big_T_small_n）；不要写 edge_T1。"
        "有多测时输入格式跟标程（先 T 再各组）。"
        "edge_n1：无自环则 m=0（空边列表合法时可空输出），允许自环可用 (1,1)。"
        "complete 须控制 n 使边数≤m 上界。"
        "再按题意补结构边界（负环/DAG/连通/二分图等）"
    ),
    "string": "字符串题：建议 edge_cases 覆盖以下边界（按需挑选，不要全抄）",
    "number_theory": "数论题：建议 edge_cases 覆盖以下边界（按需挑选，不要全抄）",
    "geometry": "几何题：建议 edge_cases 覆盖以下边界（按需挑选，不要全抄）。优先用 ConvexHull/SimplePolygon/RandomPoints",
    "multi_test": (
        "多测题：建议 edge_cases 覆盖以下边界（按需挑选，不要全抄）。"
        "优先 edge_Tmax / big_T_small_n / edge_nmax；不要写 edge_T1（与 edge_nmax/攻n 重复）。"
    ),
    "dp": "DP 题：建议覆盖规模边界与退化背包/转移情形",
    "matrix": "矩阵题：建议覆盖 1×1、满规模、单行/单列",
    "range_query": "区间查询题：建议覆盖 n/q 极值与点询/整段询",
    "weighted_tree": "带权树：优先 edge_weight::Tree/Chain/Flower",
    "weighted_graph": "带权图：树骨架 + 稀疏随机边，注意边权范围",
    "interactive": "交互/询问序列（离线）：本工具只生成询问文件，不能替代真正 interactor",
}


def _build_type_hint_block(problem_type: str | list[str]) -> str:
    """根据题型拼一段 edge_cases 建议文本，拼到 task 里。未知题型返回空串。

    支持多题型：会合并每个已知类型的建议。
    """
    from knowledge.few_shots import normalize_problem_types

    types = normalize_problem_types(problem_type)
    if not types:
        return ""
    blocks = []
    for typ in types:
        if typ not in _TYPE_EDGE_HINTS:
            continue
        header = _TYPE_HINT_HEADER.get(typ, "")
        examples = _TYPE_EDGE_HINTS[typ]
        joined = ", ".join(examples)
        blocks.append(
            f"【题型 edge_cases 建议（{typ}）】\n"
            f"{header}：\n{joined}\n"
        )
    if not blocks:
        return ""
    return (
        "\n\n"
        + "\n".join(blocks)
        + "这些只是建议清单（few-shot 同理仅供参考），最终 edge_cases 必须与题面/标程一致；"
        "若题面有特殊结构约束（如「图是 DAG」「图连通」「存在哈密顿路径」「可达负环」），"
        "必须额外加对应边界（如 dag_acyclic / connected / has_hamiltonian / negative_cycle_reachable）"
        "并在 gen 里真正保证该性质。\n"
    )


def _build_all_type_hints_block() -> str:
    """题型未预判时：给出各题型 edge_cases 建议，供写 range 时一并选用。"""
    lines = ["\n\n【各题型 edge_cases 建议（先选 problem_type，再按该行挑选，勿全抄）】"]
    for typ, examples in _TYPE_EDGE_HINTS.items():
        header = _TYPE_HINT_HEADER.get(typ, typ)
        lines.append(f"- {typ}: {header} → {', '.join(examples)}")
    lines.append(
        "最终 edge_cases 必须与题面/标程一致；有特殊结构约束时额外加对应边界。"
        "无多测 T（constraints 无 T/t）时禁止写 edge_Tmax/edge_T1；"
        "有多测时不要写 edge_T1（T=1 已被 edge_nmax/攻n 覆盖）。\n"
    )
    return "\n".join(lines)


def _build_special_samples_block(
    special_samples_desc: str,
    special_samples_count: int,
    auto_discover_special: bool = False,
) -> str:
    """若启用特殊样例（用户提示或自动挖掘），返回拼进 range task 的说明文本。"""
    desc = (special_samples_desc or "").strip()
    if desc:
        return (
            f"\n\n【特殊样例提示】\n"
            f"用户提示：{desc}\n"
            f"每方案样例数：{special_samples_count}（系统稍后单独调用大模型挖 1 条方案并自选 mutate/build，此处 count 先按常规即可）\n"
            "要求：\n"
            "1. edge_cases 不要写 special_samples；\n"
            "2. 不要在本阶段规划特殊构造方案或 gen_special；"
            "系统会在写出 range 后单独调用大模型（题面+标程）理解特殊样例，并产出 1 条方案（mode 由模型选，用户可手改）。\n"
            "3. count 由你自定（常规样例数，不得小于 15）；特殊组由后续选中方案叠加，不要自行加减。\n"
        )
    if auto_discover_special:
        return (
            f"\n\n【自动挖掘特殊方案】已开启（用户未填特殊提示）。\n"
            f"每方案样例数：{special_samples_count}\n"
            "要求：\n"
            "1. edge_cases 不要写 special_samples；\n"
            "2. 不要在本阶段规划特殊构造方案；"
            "系统将单独调用大模型根据标程/题面理解后产出 1 条方案（mutate/build 由模型选择）。\n"
            "3. count 由你自定（常规样例数，不得小于 15）。\n"
        )
    return ""

RANGE_TOOL_SCHEMAS = [
    _schema(
        "write_range",
        "写入 range.json。content 为完整 JSON：problem_type、count、constraints、edge_cases（不要含 random）。",
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
    special_samples_desc: str = "",
    special_samples_count: int = 1,
    auto_discover_special: bool = False,
) -> dict:
    """调 LLM 只生成 range.json，返回清洗并校验后的 dict（不含 std_cmd）。

    题型由 Range Agent 写入 range.json.problem_type（不单独调大模型判型；忽略 GUI 传入题型）。
    auto_discover_special：用户未填特殊提示时，仍根据标程/题面自动挖特殊方案。
    """
    from knowledge.few_shots import (
        PROBLEM_TYPE_RANGE_HINT,
        normalize_problem_type,
        resolve_problem_types_from_range,
    )

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
        std_hint = (
            f"\n\n【标程片段 lang={lang}，供推断题型与是否有多测 T】\n```\n{code}\n```\n"
        )

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

    special_block = _build_special_samples_block(
        special_samples_desc, special_samples_count, auto_discover_special,
    )

    task = (
        f"请只产出 range.json（含 problem_type）。\n\n"
        f"【题面】\n{stmt}\n\n"
        f"【数据范围描述】\n{rng}\n"
        f"{std_hint}"
        f"\n{PROBLEM_TYPE_RANGE_HINT}"
        f"{_build_all_type_hints_block()}"
        f"{pre_titles_block}"
        f"{struct_hint_block}"
        f"{special_block}"
        f"\ncount 由你根据覆盖需求自定（常规样例数），不得小于 {MIN_REGULAR_COUNT}；"
        f"统一规则 count ≥ max(15, 3^k)（k=小中大轴数；三维即 ≥27，不要写建议≥30）；"
        f"用户未另行指定时不要无故写成小于 {MIN_REGULAR_COUNT}。"
        f"constraints 覆盖题面中的规模变量（如 n、T、m）。"
        f"edge_cases 总数 4～6：优先 edge_n1/edge_nmax，其余给 special_constraints 关键结构（可合并，勿超 6）。"
        f"约束极值名须带 edge_ 前缀（edge_k_min，禁止 k_min）；结构名可无前缀。"
        f"写完 write_range 后 finish。"
        f"务必填写 special_constraints 字段（即使为空数组也要写）。\n"
        f"务必填写 problem_type：一个或多个与题面一致的英文标识符（如 tree / tree,multi_test / [tree,multi_test]）。\n"
    )
    summary = agent_run(
        task,
        max_steps=4,
        verbose=False,
        system_prompt=RANGE_ONLY_PROMPT,
        tool_schemas=RANGE_TOOL_SCHEMAS,
        tool_limits={"write_range": 1},
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
    typ = resolve_problem_types_from_range(data, stmt, rng, std_code)
    data["problem_type"] = typ
    data["auto_discover_special"] = bool(auto_discover_special)
    print(f"[range_agent] problem_type from range.json: {typ}", flush=True)

    # 特殊方案：有用户提示或开启自动挖掘时挖候选；count = 常规 + 选中×每方案样例数
    hint = (special_samples_desc or "").strip()
    want_special = bool(hint) or bool(auto_discover_special)
    if want_special:
        from server.special_discover import (
            apply_schemes_to_range,
            discover_special_schemes,
        )
        # 常规样例数信任 LLM（≥下限）；若残留旧特殊计数先剥掉再叠加
        regular = infer_regular_count(data)
        type_for_special = normalize_problem_type(typ)
        print(
            f"[range_agent] discover special schemes "
            f"(hint_len={len(hint)}, auto={bool(auto_discover_special)}, type={type_for_special}, regular={regular})",
            flush=True,
        )
        schemes = discover_special_schemes(
            stmt,
            rng,
            std_code=std_code,
            user_hint=hint,
            problem_type=type_for_special,
            samples_per_scheme=max(1, int(special_samples_count or 1)),
            auto_discover=bool(auto_discover_special),
        )
        if not schemes and hint:
            from server.special_discover import fallback_user_scheme
            schemes = fallback_user_scheme(hint, max(1, int(special_samples_count or 1)))
        data = apply_schemes_to_range(
            data,
            schemes,
            user_hint=hint,
            regular_count=regular,
        )
        print(f"[range_agent] special schemes = {len(schemes)}", flush=True)
    else:
        data["count"] = _clamp_regular_count(data.get("count"))
        data.pop("special_samples_desc", None)
        if not data.get("special_schemes"):
            data.pop("special_samples_count", None)
            data.pop("special_schemes", None)
        print(
            f"[range_agent] skip special discover "
            f"(no special_samples_desc and auto_discover_special=false); "
            f"count={data['count']} (AI-chosen, min={MIN_REGULAR_COUNT})",
            flush=True,
        )

    errs = validate_range_json(data)
    if errs:
        raise RuntimeError("range.json 不合法:\n" + "\n".join(f"  - {e}" for e in errs))
    return data
