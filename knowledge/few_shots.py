"""按题型的 few-shot 样板库。

每个样板是一段「题面 + range.json + gen + validator」的完整范例。
默认以「压缩结构要点」形式喂给 Planner：只借通用骨架
（registerGen / opt / type 分支 / index 分层 / skipBlanks+readEof / ensuref / generator.h API；
validator 不验空白格式），
禁止借范例输入字段形状；输入格式以本题标程为准。
Coder 不直接吃完整 few-shot，按 gen_plan.md + API 硬约束实现。

样板要求：正确、能跑、范围/结构校验到位（不验空白格式），且与目标题「同构但不同」。

detect_problem_type(): 关键词兜底（range.json 未写/写错 problem_type 时使用）。
resolve_problem_type_from_range(): 从 range.json 取题型，无效则关键词兜底。
get_few_shot(): 统一入口，按显式指定或已解析题型返回完整样板字符串（可能为空）。
get_few_shot_rag(..., compact=True): Planner 用压缩要点；compact=False 返回完整源码。
题型判定融入 Range Agent 写 range.json（字段 problem_type），不再单独调大模型判型。
"""

from __future__ import annotations

import re

# C++ testlib 版样板从 few_shots_cpp 导入（当前默认用 C++ testlib）
from knowledge.few_shots_cpp import (
    CPP_ARRAY_EXAMPLE,
    CPP_TREE_EXAMPLE,
    CPP_GRAPH_EXAMPLE,
    CPP_STRING_EXAMPLE,
    CPP_NUMBER_THEORY_EXAMPLE,
    CPP_MULTI_TEST_EXAMPLE,
    CPP_GEOMETRY_EXAMPLE,
    CPP_DP_EXAMPLE,
    CPP_MATRIX_EXAMPLE,
    CPP_RANGE_QUERY_EXAMPLE,
    CPP_WEIGHTED_TREE_EXAMPLE,
    CPP_WEIGHTED_GRAPH_EXAMPLE,
    CPP_INTERACTIVE_EXAMPLE,
)

# 阶段一 RAG 召回入口（可选，失败时自动回退到关键词模板）
from knowledge.few_shots_rag import (
    retrieve_few_shots,
    format_rag_few_shots,
    format_rag_few_shots_compact,
    compress_few_shot_content,
)


# ---- 题型关键词（用于自动判型）----
# 顺序无关，按命中关键词数量打分；都没有时默认 array（最通用）。
# 关键词尽量选各题型独有的，避免「边/节点」这种树图共用的泛词主导。
_TYPE_KEYWORDS = {
    "tree": ["树", "tree", "直径", "父节点", "lca", "无根", "有根", "二叉树",
             "dfs 树", "树形 dp", "子树", "depth", "forest"],
    "graph": ["图", "graph", "连通块", "最短路", "最短路径", "环", "mst",
              "二分图", "网络流", "邻接", "割点", "桥", "dijkstra", "bfs 图"],
    "string": ["字符串", "string", "子串", "子序列", "模式", "回文",
               "kmp", "trie", "后缀", "hash 串", "匹配"],
    "number_theory": ["gcd", "lcm", "素数", "质数", "同余", "数论", "约数",
                      "整除", "欧拉", "费马", "逆元", "模意义"],
    "array": ["数组", "序列", "求和", "区间", "排序", "前缀和",
              "最大子段", "逆序对", "差分", "双指针"],
    "geometry": ["几何", "凸包", "多边形", "坐标", "平面", "点集",
                 "convex", "polygon", "geometry", "交点", "面积", "最近点对"],
    "multi_test": ["多测", "多组", "测试组数", "t 组", "T 组", "sum n",
                   "multi test", "multiple test"],
    "dp": ["动态规划", "dp", "背包", "knapsack", "最长公共", "lis", "lcs", "状态压缩"],
    "matrix": ["矩阵", "matrix", "二维数组", "网格", "grid", "行列"],
    "range_query": ["区间", "线段树", "树状数组", "rmq", "前缀和询问", "range query", "查询次数"],
    "weighted_tree": ["边权", "带权树", "点权", "树上路径权"],
    "weighted_graph": ["带权图", "边权图", "最短路", "dij", "spfa", "floyd"],
    "interactive": ["交互", "interactive", "询问", "query", "交互库"],
}

# 标程源码中用于辅助判型的关键词。比题面关键词更侧重算法/数据结构痕迹。
_STD_CODE_KEYWORDS = {
    "tree": [
        "tree", "dfs", "lca", "diameter", "subtree", "parent", "children",
        "ancestor", "depth", "height", "rooted", "binary tree", "fenwick",
    ],
    "graph": [
        "graph", "adj", "adjacency", "bfs", "dfs", "dijkstra", "floyd",
        "kruskal", "prim", "mst", "topological", "union", "dsu", "scc",
        "tarjan", "bridge", "cut vertex", "dag", "bipartite", "flow",
    ],
    "string": [
        "string", "substr", "substring", "kmp", "trie", "suffix", "prefix",
        "palindrome", "hash", "rolling hash", "z-function", "manacher",
    ],
    "number_theory": [
        "gcd", "lcm", "prime", "sieve", "mod", "inverse", "phi", "factor",
        "divisor", "exgcd", "powmod", "fast pow", "combinatorics", "nCr",
    ],
    "array": [
        "array", "sort", "prefix", "segment tree", "fenwick", "binary search",
        "two pointers", "sliding window", "dp", "max subarray", "inversion",
    ],
    "geometry": [
        "convex", "hull", "polygon", "geometry", "cross", "dot", "point",
        "segment", "area", "closest", "graham", "andrew", "rotating calipers",
    ],
    "multi_test": [
        "t--", "while(t--)", "while (t--)", "for(int t", "for (int t",
        "read(t)", "cin >> t", "scanf(\"%d\", &t)", "sum n", "sumn",
    ],
    "dp": ["dp", "knapsack", "lis", "lcs", "memo", "dfs(i", "f[i]"],
    "matrix": ["matrix", "grid", "a[i][j]", "vector<vector"],
    "range_query": ["segment tree", "fenwick", "query", "l r", "range"],
    "weighted_tree": ["edge weight", "w[u]", "tree weight"],
    "weighted_graph": ["dijkstra", "spfa", "floyd", "edge.w", "weight"],
    "interactive": ["interactive", "query", "ask", "cout.flush", "fflush"],
}


def _score_by_keywords(text: str, keyword_dict: dict) -> dict:
    """按 keyword_dict 给各类别打分。"""
    text = (text or "").lower()
    scores = {typ: 0 for typ in keyword_dict}
    for typ, kws in keyword_dict.items():
        for kw in kws:
            scores[typ] += text.count(kw.lower())
    return scores


def detect_problem_type(
    problem_statement: str,
    data_range_desc: str = "",
    std_code: str = "",
) -> str:
    """关键词计分回退：综合题面/范围/标程打分，全无命中默认 array。"""
    stmt_scores = _score_by_keywords(problem_statement + " " + data_range_desc, _TYPE_KEYWORDS)
    code_scores = _score_by_keywords(std_code, _STD_CODE_KEYWORDS)

    # 合并：题面权重 1，标程权重 1。可在后续按效果调整。
    scores = {typ: stmt_scores.get(typ, 0) + code_scores.get(typ, 0)
              for typ in set(stmt_scores) | set(code_scores)}
    best = max(scores, key=scores.get)
    return best if scores[best] > 0 else "array"


# 固定模板（来自 few_shots_cpp）
FEW_SHOTS = {
    "array": CPP_ARRAY_EXAMPLE,
    "tree": CPP_TREE_EXAMPLE,
    "graph": CPP_GRAPH_EXAMPLE,
    "string": CPP_STRING_EXAMPLE,
    "number_theory": CPP_NUMBER_THEORY_EXAMPLE,
    "geometry": CPP_GEOMETRY_EXAMPLE,
    "multi_test": CPP_MULTI_TEST_EXAMPLE,
    "dp": CPP_DP_EXAMPLE,
    "matrix": CPP_MATRIX_EXAMPLE,
    "range_query": CPP_RANGE_QUERY_EXAMPLE,
    "weighted_tree": CPP_WEIGHTED_TREE_EXAMPLE,
    "weighted_graph": CPP_WEIGHTED_GRAPH_EXAMPLE,
    "interactive": CPP_INTERACTIVE_EXAMPLE,
}

KNOWN_PROBLEM_TYPES = tuple(FEW_SHOTS.keys())

# 拼进 Range Agent 的 task/system：要求在同一次 write_range 里写出 problem_type
PROBLEM_TYPE_RANGE_HINT = (
    "【problem_type — 必填】在 range.json 中写入字段 problem_type，"
    "取值必须是下列之一（只写标识符，不要写中文）：\n"
    + ", ".join(KNOWN_PROBLEM_TYPES)
    + "\n\n分类原则（按优先级）：\n"
    "1. 输入主体是树（n-1 条边、有根/无根树、子树、树上路径、树链剖分、LCA 等）"
    "→ tree；以边权/点权为主 → weighted_tree。\n"
    "2. 输入主体是一般图（连通性、最短路、DAG、二分图、网络流等）"
    "→ graph 或 weighted_graph。\n"
    "3. 以 gcd/素数/同余/欧拉函数/组合数等数论对象为主 → number_theory。"
    "不要仅因答案需要取模（mod/%）就判为 number_theory。\n"
    "4. 字符串/模式匹配 → string；几何点集/凸包 → geometry；"
    "多测 T+sum → multi_test；区间数据结构查询 → range_query；"
    "DP/背包 → dp；二维网格 → matrix；交互题 → interactive。\n"
    "5. 其余序列/数组题 → array。\n"
    "先定 problem_type，再按该题型设计 edge_cases（见下方各题型建议）。\n"
)


def normalize_problem_type(raw: str) -> str:
    """把自由文本规范成已知题型标识；无法识别返回空串。"""
    if not raw:
        return ""
    t = str(raw).strip().lower().replace(" ", "_").replace("-", "_")
    t = t.strip("`\"'.,;:()[]")
    if t in FEW_SHOTS:
        return t
    for line in str(raw).splitlines():
        cand = line.strip().lower().replace(" ", "_").replace("-", "_")
        cand = cand.strip("`\"'.,;:()[]")
        # 允许 "题型: tree" / "type=tree"
        m = re.search(
            r"(?:problem_type|type|题型)\s*[:=：]\s*([a-z_]+)",
            cand,
        )
        if m and m.group(1) in FEW_SHOTS:
            return m.group(1)
        if cand in FEW_SHOTS:
            return cand
    text = str(raw).lower()
    for typ in KNOWN_PROBLEM_TYPES:
        if re.search(rf"\b{re.escape(typ)}\b", text):
            return typ
    return ""


def resolve_problem_type_from_range(
    range_json: dict | None,
    problem_statement: str = "",
    data_range_desc: str = "",
    std_code: str = "",
) -> str:
    """从 range.json 的 problem_type 取值；无效/缺失时关键词兜底，再默认 array。"""
    if isinstance(range_json, dict):
        typ = normalize_problem_type(str(range_json.get("problem_type") or ""))
        if typ:
            return typ
    return (
        detect_problem_type(problem_statement, data_range_desc, std_code) or "array"
    )


def get_few_shot(
    problem_type: str,
    problem_statement: str = "",
    data_range_desc: str = "",
    std_code: str = "",
) -> str:
    """统一取样板：显式指定优先，否则关键词回退判型；找不到返回空串。"""
    typ = normalize_problem_type(problem_type) or detect_problem_type(
        problem_statement, data_range_desc, std_code
    )
    return FEW_SHOTS.get(typ, "")


def detected_type(
    problem_type: str,
    problem_statement: str = "",
    data_range_desc: str = "",
    std_code: str = "",
    range_json: dict | None = None,
) -> str:
    """返回最终生效的题型名。

    优先级：显式 problem_type > range.json.problem_type > 关键词回退。
    """
    typ = normalize_problem_type(problem_type)
    if typ:
        return typ
    return resolve_problem_type_from_range(
        range_json, problem_statement, data_range_desc, std_code
    )


# ---- 数组 / 序列题（求和类）----
ARRAY_EXAMPLE = """【参考范例：一道数组题的标准写法】
题面：给定 n 和 n 个整数 a1..an，输出它们的和。
输入格式：第 1 行 n；第 2 行 n 个整数空格分隔。
输出格式：一个整数。
数据范围：n∈[1,1e5]，ai∈[-1e9,1e9]；本范例 count=20（实际由覆盖需求自定，≥15），覆盖 n=1、n=max、全相等、降序。

range.json:
{
  "count": 20,
  "constraints": {"n": [1, 100000], "ai": [-1000000000, 1000000000]},
  "edge_cases": ["edge_n1", "edge_nmax", "all_equal", "descending"]
}

gen.py:
import argparse, random, sys

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--seed", type=int, required=True)
    ap.add_argument("--type", default="random")
    a = ap.parse_args()
    rng = random.Random(a.seed)
    N_MIN, N_MAX = 1, 100000
    A_MIN, A_MAX = -1000000000, 1000000000
    if a.type == "edge_n1":
        n = 1
    elif a.type == "edge_nmax":
        n = N_MAX
    else:
        n = rng.randint(N_MIN, min(100, N_MAX))
    if a.type == "all_equal":
        v = rng.randint(A_MIN, A_MAX); vals = [v] * n
    elif a.type == "descending":
        vals = sorted([rng.randint(A_MIN, A_MAX) for _ in range(n)], reverse=True)
    else:
        vals = [rng.randint(A_MIN, A_MAX) for _ in range(n)]
    print(n)
    print(" ".join(map(str, vals)))

if __name__ == "__main__":
    main()

validate.py:
import sys

def main():
    data = sys.stdin.read().strip().split()
    if not data:
        print("empty input", file=sys.stderr); sys.exit(1)
    try:
        n = int(data[0])
    except ValueError:
        print("n not int", file=sys.stderr); sys.exit(1)
    if not (1 <= n <= 100000):
        print(f"n out of range: {n}", file=sys.stderr); sys.exit(1)
    if len(data) != 1 + n:
        print(f"expected {n} numbers, got {len(data)-1}", file=sys.stderr); sys.exit(1)
    for x in data[1:]:
        try:
            v = int(x)
        except ValueError:
            print(f"not int: {x}", file=sys.stderr); sys.exit(1)
        if not (-1000000000 <= v <= 1000000000):
            print(f"ai out of range: {v}", file=sys.stderr); sys.exit(1)
    sys.exit(0)

if __name__ == "__main__":
    main()
"""

# ---- 树题（求直径类）----
TREE_EXAMPLE = """【参考范例：一道树题的标准写法】
题面：给定一棵 n 个节点的无权无根树，求树的直径（最长简单路径的边数）。
输入格式：第 1 行 n；接下来 n-1 行每行两个整数 u v 表示一条边（节点编号 1..n）。
输出格式：一个整数。
数据范围：n∈[2,1e5]；本范例 count=20（实际由覆盖需求自定，≥15），覆盖链、菊花、随机树、平衡二叉树、n=2。

range.json:
{
  "count": 20,
  "constraints": {"n": [2, 100000]},
  "edge_cases": ["chain", "star", "random_tree", "balanced_binary", "edge_n2"]
}

gen.py:
import argparse, random, sys

def build(n, typ, rng):
    if n == 1:
        return []
    if typ == "chain":
        return [(i, i + 1) for i in range(1, n)]
    if typ == "star":
        return [(1, i) for i in range(2, n + 1)]
    if typ == "balanced_binary":
        return [(i // 2, i) for i in range(2, n + 1)]
    # random_tree: 节点 i 连到 1..i-1 中的随机一个，保证连通无环
    return [(rng.randint(1, i - 1), i) for i in range(2, n + 1)]

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--seed", type=int, required=True)
    ap.add_argument("--type", default="random_tree")
    a = ap.parse_args()
    rng = random.Random(a.seed)
    N_MIN, N_MAX = 2, 100000
    if a.type == "edge_n2":
        n = 2
    elif a.type == "chain" or a.type == "star" or a.type == "balanced_binary":
        n = rng.randint(N_MIN, min(1000, N_MAX))
    else:
        n = rng.randint(N_MIN, min(1000, N_MAX))
    edges = build(n, a.type, rng)
    print(n)
    for u, v in edges:
        print(u, v)

if __name__ == "__main__":
    main()

validate.py:
import sys

def main():
    data = sys.stdin.read().strip().split()
    if not data:
        print("empty input", file=sys.stderr); sys.exit(1)
    idx = 0
    try:
        n = int(data[idx]); idx += 1
    except ValueError:
        print("n not int", file=sys.stderr); sys.exit(1)
    if not (2 <= n <= 100000):
        print(f"n out of range: {n}", file=sys.stderr); sys.exit(1)
    if len(data) - idx != 2 * (n - 1):
        print(f"expected {n-1} edges, got {(len(data)-idx)//2}", file=sys.stderr); sys.exit(1)
    edges = []
    seen = set()
    for _ in range(n - 1):
        u = int(data[idx]); v = int(data[idx + 1]); idx += 2
        if not (1 <= u <= n) or not (1 <= v <= n):
            print(f"node out of range: {u} {v}", file=sys.stderr); sys.exit(1)
        if u == v:
            print(f"self loop: {u}", file=sys.stderr); sys.exit(1)
        key = (min(u, v), max(u, v))
        if key in seen:
            print(f"duplicate edge: {u} {v}", file=sys.stderr); sys.exit(1)
        seen.add(key)
        edges.append((u, v))
    # 并查集查连通 + 无环
    parent = list(range(n + 1))
    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]; x = parent[x]
        return x
    for u, v in edges:
        ru, rv = find(u), find(v)
        if ru == rv:
            print(f"cycle at edge {u} {v}", file=sys.stderr); sys.exit(1)
        parent[ru] = rv
    root = find(1)
    for i in range(2, n + 1):
        if find(i) != root:
            print("not connected", file=sys.stderr); sys.exit(1)
    sys.exit(0)

if __name__ == "__main__":
    main()
"""

# ---- 图题（连通性类）----
GRAPH_EXAMPLE = """【参考范例：一道图题的标准写法】
题面：给定 n 个点 m 条边的无向图（无自环无重边），判断是否连通。
输入格式：第 1 行 n m；接下来 m 行每行 u v。
输出格式：YES 或 NO。
数据范围：n∈[1,1000]，m∈[0,n*(n-1)/2]；本范例 count=15（实际由覆盖需求自定，≥15），覆盖连通树、不连通、完全图、链、菊花、随机稀疏。

range.json:
{
  "count": 15,
  "constraints": {"n": [1, 1000], "m": [0, 499500]},
  "edge_cases": ["connected_tree", "disconnected", "complete", "path", "star", "random_sparse"]
}

gen.py:
import argparse, random, sys

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--seed", type=int, required=True)
    ap.add_argument("--type", default="random_sparse")
    a = ap.parse_args()
    rng = random.Random(a.seed)
    N_MIN, N_MAX = 1, 1000
    n = rng.randint(max(2, N_MIN), min(50, N_MAX))
    edges = []
    if a.type == "connected_tree":
        for i in range(2, n + 1):
            edges.append((rng.randint(1, i - 1), i))
    elif a.type == "disconnected":
        mid = n // 2 or 1
        for i in range(2, mid + 1):
            edges.append((rng.randint(1, i - 1), i))
        for i in range(mid + 2, n + 1):
            edges.append((rng.randint(mid + 1, i - 1) if i > mid + 1 else mid + 1, i))
    elif a.type == "complete":
        for u in range(1, n + 1):
            for v in range(u + 1, n + 1):
                edges.append((u, v))
    elif a.type == "path":
        for i in range(1, n):
            edges.append((i, i + 1))
    elif a.type == "star":
        for i in range(2, n + 1):
            edges.append((1, i))
    else:  # random_sparse
        pool = [(u, v) for u in range(1, n + 1) for v in range(u + 1, n + 1)]
        rng.shuffle(pool)
        m = rng.randint(0, min(len(pool), n))
        edges = pool[:m]
    print(n, len(edges))
    for u, v in edges:
        print(u, v)

if __name__ == "__main__":
    main()

validate.py:
import sys

def main():
    data = sys.stdin.read().strip().split()
    if not data:
        print("empty input", file=sys.stderr); sys.exit(1)
    idx = 0
    n = int(data[idx]); m = int(data[idx + 1]); idx += 2
    if not (1 <= n <= 1000):
        print(f"n out of range: {n}", file=sys.stderr); sys.exit(1)
    if not (0 <= m <= 499500):
        print(f"m out of range: {m}", file=sys.stderr); sys.exit(1)
    if len(data) - idx != 2 * m:
        print(f"expected {m} edges, got {(len(data)-idx)//2}", file=sys.stderr); sys.exit(1)
    seen = set()
    for _ in range(m):
        u = int(data[idx]); v = int(data[idx + 1]); idx += 2
        if not (1 <= u <= n) or not (1 <= v <= n):
            print(f"node out of range: {u} {v}", file=sys.stderr); sys.exit(1)
        if u == v:
            print(f"self loop: {u}", file=sys.stderr); sys.exit(1)
        key = (min(u, v), max(u, v))
        if key in seen:
            print(f"duplicate edge: {u} {v}", file=sys.stderr); sys.exit(1)
        seen.add(key)
    sys.exit(0)

if __name__ == "__main__":
    main()
"""

# ---- 字符串题（模式匹配类）----
STRING_EXAMPLE = """【参考范例：一道字符串题的标准写法】
题面：给定长度为 n 的小写字母字符串 s 和模式串 p，输出 p 在 s 中作为子串出现的次数。
输入格式：第 1 行 n；第 2 行 s；第 3 行 p。
输出格式：一个整数。
数据范围：n∈[1,1000]，p 长度∈[1,n]；本范例 count=15（实际由覆盖需求自定，≥15），覆盖全相同、模式在首/尾、无匹配、长连续段。

range.json:
{
  "count": 15,
  "constraints": {"n": [1, 1000]},
  "edge_cases": ["all_same", "pattern_at_start", "pattern_at_end", "no_match", "long_run"]
}

gen.py:
import argparse, random, string, sys

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--seed", type=int, required=True)
    ap.add_argument("--type", default="random")
    a = ap.parse_args()
    rng = random.Random(a.seed)
    letters = string.ascii_lowercase
    N_MIN, N_MAX = 1, 1000
    n = rng.randint(N_MIN, min(50, N_MAX))
    if a.type == "all_same":
        c = rng.choice(letters); s = c * n
    elif a.type == "long_run":
        c = rng.choice(letters); s = c * n
    else:
        s = "".join(rng.choice(letters) for _ in range(n))
    p_len = rng.randint(1, max(1, n))
    if a.type == "pattern_at_start":
        p = s[:p_len]
    elif a.type == "pattern_at_end":
        p = s[n - p_len:]
    elif a.type == "no_match":
        p = "".join(rng.choice(letters) for _ in range(p_len))
        while p in s:
            p = "".join(rng.choice(letters) for _ in range(p_len))
    else:
        p = s[:p_len] if n > 0 else "a"
    print(n)
    print(s)
    print(p)

if __name__ == "__main__":
    main()

validate.py:
import sys

def main():
    lines = sys.stdin.read().split("\\n")
    if len(lines) < 3:
        print("need 3 lines", file=sys.stderr); sys.exit(1)
    try:
        n = int(lines[0].strip())
    except ValueError:
        print("n not int", file=sys.stderr); sys.exit(1)
    if not (1 <= n <= 1000):
        print(f"n out of range: {n}", file=sys.stderr); sys.exit(1)
    s = lines[1].rstrip("\\n")
    p = lines[2].rstrip("\\n")
    if len(s) != n:
        print(f"|s|={len(s)} != n={n}", file=sys.stderr); sys.exit(1)
    if not (1 <= len(p) <= n):
        print(f"p len out of range: {len(p)}", file=sys.stderr); sys.exit(1)
    for ch in s + p:
        if not ("a" <= ch <= "z"):
            print(f"non-lowercase: {ch!r}", file=sys.stderr); sys.exit(1)
    sys.exit(0)

if __name__ == "__main__":
    main()
"""

# ---- 数论题（GCD 类）----
NUMBER_THEORY_EXAMPLE = """【参考范例：一道数论题的标准写法】
题面：给定 n 个正整数，输出它们的最大公约数。
输入格式：第 1 行 n；第 2 行 n 个正整数空格分隔。
输出格式：一个整数。
数据范围：n∈[1,1e5]，ai∈[1,1e9]；本范例 count=15（实际由覆盖需求自定，≥15），覆盖 n=1、全相等、全素数、含两两互素、全偶。

range.json:
{
  "count": 15,
  "constraints": {"n": [1, 100000], "ai": [1, 1000000000]},
  "edge_cases": ["edge_n1", "all_equal", "all_prime", "coprime_pair", "all_even"]
}

gen.py:
import argparse, random, sys

def is_prime(x):
    if x < 2: return False
    i = 2
    while i * i <= x:
        if x % i == 0: return False
        i += 1
    return True

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--seed", type=int, required=True)
    ap.add_argument("--type", default="random")
    a = ap.parse_args()
    rng = random.Random(a.seed)
    N_MIN, N_MAX = 1, 100000
    A_MIN, A_MAX = 1, 1000000000
    if a.type == "edge_n1":
        n = 1
    else:
        n = rng.randint(N_MIN, min(100, N_MAX))
    if a.type == "all_equal":
        v = rng.randint(A_MIN, A_MAX); vals = [v] * n
    elif a.type == "all_prime":
        primes = [x for x in range(2, 200) if is_prime(x)]
        vals = [rng.choice(primes) for _ in range(n)]
    elif a.type == "coprime_pair":
        # 选互不相同的素数保证两两互素
        primes = [x for x in range(2, 500) if is_prime(x)]
        rng.shuffle(primes)
        vals = primes[:n]
    elif a.type == "all_even":
        vals = [rng.randint(A_MIN // 2, A_MAX // 2) * 2 for _ in range(n)]
    else:
        vals = [rng.randint(A_MIN, A_MAX) for _ in range(n)]
    print(n)
    print(" ".join(map(str, vals)))

if __name__ == "__main__":
    main()

validate.py:
import sys

def main():
    data = sys.stdin.read().strip().split()
    if not data:
        print("empty input", file=sys.stderr); sys.exit(1)
    try:
        n = int(data[0])
    except ValueError:
        print("n not int", file=sys.stderr); sys.exit(1)
    if not (1 <= n <= 100000):
        print(f"n out of range: {n}", file=sys.stderr); sys.exit(1)
    if len(data) != 1 + n:
        print(f"expected {n} numbers, got {len(data)-1}", file=sys.stderr); sys.exit(1)
    for x in data[1:]:
        try:
            v = int(x)
        except ValueError:
            print(f"not int: {x}", file=sys.stderr); sys.exit(1)
        if not (1 <= v <= 1000000000):
            print(f"ai out of range: {v}", file=sys.stderr); sys.exit(1)
    sys.exit(0)

if __name__ == "__main__":
    main()
"""


def _maybe_compact(block: str, compact: bool) -> str:
    if not block or not compact:
        return block
    # 关键词回退是完整单模板；压成结构要点并加标题
    body = compress_few_shot_content(block)
    if not body:
        return ""
    return f"【结构要点：题型模板】\n{body}"


def _ensure_multi_test_template(block: str, compact: bool) -> str:
    """题型为 multi_test 时，保证压缩/完整块中含官方多测三桶模板。"""
    pinned = FEW_SHOTS.get("multi_test") or ""
    if not pinned:
        return block
    pin_body = _maybe_compact(pinned, compact) if compact else pinned
    if not pin_body:
        return block
    # 已含三桶信号则不重复贴
    if block and (
        "三桶" in block
        or ("攻T" in block and "攻n" in block)
        or ("攻 T" in block and "攻 n" in block)
        or ("bucket" in block and "force_small_n" in block)
    ):
        return block
    header = "【结构要点：题型模板 multi_test（强制注入）】\n" if compact else ""
    if not block:
        return pin_body if compact else pinned
    return f"{block}\n\n{header}{pin_body}" if compact else f"{block}\n\n{pinned}"


def get_few_shot_rag(
    problem_type: str = "",
    problem_statement: str = "",
    data_range_desc: str = "",
    std_code: str = "",
    top_k: int = 2,
    use_fallback: bool = True,
    compact: bool = False,
) -> tuple[str, str]:
    """RAG 召回 few-shot 模板，并返回 (模板字符串, 召回信息摘要)。

    compact=True：只返回通用骨架要点（registerGen/opt/分支/skipBlanks+readEof 等，给 Planner）；
                  不含范例输入字段形状；validator 要点强调不验空白、禁止裸 readEof。
    compact=False：返回完整范例源码（兼容旧用途 / 调试）。

    未配置 EMBEDDING_API_KEY 时：直接用关键词/题型模板，不发起 embedding 调用。

    若 RAG 调用失败或返回空：
      - use_fallback=True 时，回退到原有关键词模板匹配
      - use_fallback=False 时，返回空字符串

    题型为 multi_test 时：始终保证官方多测三桶模板在结果中（RAG 未召回同类时强制追加）。
    """
    from agent.llm import embedding_configured

    typ = detected_type(problem_type, problem_statement, data_range_desc, std_code)
    want_multi = typ == "multi_test"

    if not embedding_configured():
        fallback = get_few_shot(problem_type, problem_statement, data_range_desc, std_code)
        # 显式/检测为 multi_test 但 keyword 未命中时，仍强制用官方模板
        if want_multi and not fallback:
            fallback = FEW_SHOTS.get("multi_test", "")
        block = _maybe_compact(fallback, compact)
        if want_multi:
            block = _ensure_multi_test_template(block, compact)
        return block, f"未配置 Embedding，使用题型模板: {typ or 'multi_test'}"

    try:
        examples = retrieve_few_shots(problem_statement, data_range_desc, std_code, top_k=top_k)
    except Exception as e:
        if not use_fallback:
            return "", f"RAG 召回失败: {e}"
        fallback = get_few_shot(problem_type, problem_statement, data_range_desc, std_code)
        if want_multi and not fallback:
            fallback = FEW_SHOTS.get("multi_test", "")
        block = _maybe_compact(fallback, compact)
        if want_multi:
            block = _ensure_multi_test_template(block, compact)
        return (
            block,
            f"RAG 失败，已回退 keyword 模板: {type(e).__name__}: {e}",
        )

    if not examples:
        if not use_fallback:
            return "", "RAG 未召回任何模板"
        fallback = get_few_shot(problem_type, problem_statement, data_range_desc, std_code)
        if want_multi and not fallback:
            fallback = FEW_SHOTS.get("multi_test", "")
        block = _maybe_compact(fallback, compact)
        if want_multi:
            block = _ensure_multi_test_template(block, compact)
        return block, "RAG 未召回模板，已回退 keyword 匹配"

    rag_block = (
        format_rag_few_shots_compact(examples)
        if compact
        else format_rag_few_shots(examples)
    )
    summary = f"RAG 召回 {len(examples)} 个模板: " + ", ".join(
        f"{ex['key']}({ex['score']})" for ex in examples
    )
    if want_multi:
        before = rag_block or ""
        rag_block = _ensure_multi_test_template(rag_block, compact)
        if (rag_block or "") != before and "强制注入" in (rag_block or ""):
            summary += " + 强制注入 multi_test 三桶模板"
    return rag_block, summary
