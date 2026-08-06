"""GUI constants and edge-case helpers."""
import os

from dotenv import load_dotenv

load_dotenv()

_SERVER_HOST = os.getenv("SERVER_HOST", "127.0.0.1").strip() or "127.0.0.1"
_SERVER_PORT = int(os.getenv("SERVER_PORT") or "8000")
BASE = f"http://{_SERVER_HOST}:{_SERVER_PORT}"
PROBLEM_TYPES = [
    "自动", "array", "tree", "graph", "string", "number_theory", "geometry",
    "multi_test", "dp", "matrix", "range_query", "weighted_tree", "weighted_graph",
    "interactive",
]
LANGS = ["python", "cpp"]
# 界面中文 → API 英文 id（空字符串表示不使用内置 checker）
BUILTIN_CHECKER_LABEL_TO_ID = {
    "无": "",
    "按行比较": "lcmp",
    "按词比较": "wcmp",
    "浮点比较·1e-4": "rcmp4",
    "浮点比较·1e-6": "rcmp6",
    "浮点比较·1e-9": "rcmp9",
    "Yes/No 比较": "yesno",
}
BUILTIN_CHECKER_ID_TO_LABEL = {v: k for k, v in BUILTIN_CHECKER_LABEL_TO_ID.items() if v}
BUILTIN_CHECKER_ID_TO_LABEL[""] = "无"
BUILTIN_CHECKER_OPTIONS = list(BUILTIN_CHECKER_LABEL_TO_ID.keys())


def _builtin_checker_label_from_any(value: str) -> str:
    """把工作区/API 里的英文 id 或中文标签规范成界面中文。"""
    v = (value or "").strip()
    if not v or v == "无":
        return "无"
    if v in BUILTIN_CHECKER_LABEL_TO_ID:
        return v
    return BUILTIN_CHECKER_ID_TO_LABEL.get(v.lower(), "无")


def _builtin_checker_id_from_label(label: str) -> str:
    """界面中文 → 提交用的英文 id；「无」为空串。"""
    return BUILTIN_CHECKER_LABEL_TO_ID.get(
        _builtin_checker_label_from_any(label), ""
    )

# 边界类型：界面展示中文；提交仍用英文 id。方案过多时默认保留约 5 个。
EDGE_CASE_UI_LIMIT = 5
EDGE_CASE_LABELS = {
    "edge_T1": "多测 T=1",
    "edge_Tmax": "多测 T 最大",
    "edge_t1": "多测 T=1",
    "edge_tmax": "多测 T 最大",
    "edge_n1": "n 最小（通常=1）",
    "edge_n2": "n=2",
    "edge_nmax": "n 最大",
    "edge_n_min": "n 最小",
    "edge_n_max": "n 最大",
    "edge_m0": "边数/规模为 0",
    "edge_m1": "边数/规模为 1",
    "edge_m_min": "边数最小",
    "edge_m_max": "边数最大",
    "edge_mmax": "边数最大",
    "edge_all_eulerian_cycle": "欧拉回路（全偶度连通）",
    "edge_all_eulerian_path": "欧拉通路（恰 2 奇度）",
    "edge_connected": "连通图",
    "edge_disconnected": "不连通图",
    "edge_two_odd_degree": "恰 2 个奇度点",
    "edge_four_odd_degree": "恰 4 个奇度点",
    "edge_single_node_loop": "单点自环",
    "edge_color_reuse": "少量颜色大量复用",
    "edge_long_words": "最长颜色名",
    "edge_same_color_both_ends": "两端同色木棍",
    "disconnected": "不连通",
    "random_sparse": "稀疏随机图",
    "connected": "连通",
    "connected_tree": "树（连通 n-1 边）",
    "chain": "链状",
    "star": "菊花/星形",
    "all_equal": "全相等",
    "descending": "严格递减",
    "all_negative": "全负",
    "all_max_value": "全取最大值",
    "two_values": "仅两种取值",
    "big_T_small_n": "大 T + 小 n",
    "small_T_big_n": "小 T + 大 n",
    "single_max_case": "单组最大规模",
    "sum_full": "sum 顶满",
    "edge_W1": "容量/权值最小",
    "edge_Wmax": "容量/权值最大",
    "all_heavy": "全重物",
    "all_light": "全轻物",
    "edge_11": "1×1",
    "row": "单行",
    "col": "单列",
    "all_zero": "全零",
    "all_max": "全最大",
    "q1": "查询数=1",
    "qmax": "查询数最大",
    "point_queries": "点查询",
    "full_range": "整段查询",
    "all_same": "全相同字符",
    "pattern_at_start": "模式在开头",
    "pattern_at_end": "模式在结尾",
    "no_match": "无匹配",
    "long_run": "长连续段",
    "two_chars": "仅两种字符",
    "all_prime": "全素数",
    "coprime_pair": "互质对",
    "all_even": "全偶数",
    "include_one": "含 1",
    "convex_hull": "凸包",
    "simple_polygon": "简单多边形",
    "random_points": "随机点集",
    "collinear": "共线",
    "same_x": "同 x 坐标",
    "negative_cycle_reachable": "可达负环",
    "negative_cycle_unreachable": "不可达负环",
    "no_negative_cycle": "无负环",
    "dag_acyclic": "DAG 无环",
    "bipartite": "二分图",
}


def _edge_case_zh(edge_id: str) -> str:
    """英文边界 id → 中文说明；未知则按关键词猜一句。"""
    e = (edge_id or "").strip()
    if not e:
        return ""
    if e in EDGE_CASE_LABELS:
        return EDGE_CASE_LABELS[e]
    low = e.lower()
    rules = (
        ("eulerian_cycle", "欧拉回路"),
        ("eulerian_path", "欧拉通路"),
        ("disconnected", "不连通"),
        ("connected", "连通"),
        ("negative_cycle", "负环相关"),
        ("hamilton", "哈密顿"),
        ("bipartite", "二分图"),
        ("dag", "有向无环"),
        ("sparse", "稀疏"),
        ("dense", "稠密"),
        ("complete", "完全图"),
        ("chain", "链"),
        ("star", "星/菊花"),
        ("tmax", "T 最大"),
        ("t1", "T=1"),
        ("mmax", "边数最大"),
        ("m0", "边数/规模为 0"),
        ("m1", "边数/规模为 1"),
        ("nmax", "n 最大"),
        ("n1", "n=1"),
        ("n2", "n=2"),
        ("long", "长串/大规模"),
        ("odd", "奇度相关"),
        ("loop", "自环"),
        ("reuse", "复用"),
    )
    for key, zh in rules:
        if key in low:
            return zh
    return e.replace("_", " ")


def _trim_edge_cases(edges: list, limit: int = EDGE_CASE_UI_LIMIT) -> list[str]:
    """保留约 limit 个边界：优先最小/最大规模与结构边界，保序。"""
    cleaned: list[str] = []
    seen: set[str] = set()
    for e in edges or []:
        if not isinstance(e, str):
            continue
        name = e.strip()
        if not name or name in seen:
            continue
        seen.add(name)
        cleaned.append(name)
    if len(cleaned) <= limit:
        return cleaned

    def _score(name: str) -> int:
        low = name.lower()
        s = 0
        if any(k in low for k in ("m0", "n1", "n2", "empty", "min", "_1")):
            s += 100
        if any(k in low for k in ("mmax", "nmax", "tmax", "max")):
            s += 90
        if any(k in low for k in ("euler", "cycle", "path", "dag", "hamilton")):
            s += 75
        if any(k in low for k in ("disconnect", "connect", "bipartite")):
            s += 65
        if any(k in low for k in ("odd", "loop", "sparse", "dense")):
            s += 55
        if any(k in low for k in ("t1", "tmax", "sum")):
            s += 40
        return s

    ranked = sorted(enumerate(cleaned), key=lambda iv: (-_score(iv[1]), iv[0]))
    keep_idx = {i for i, _ in ranked[:limit]}
    return [e for i, e in enumerate(cleaned) if i in keep_idx]
