"""题面特殊结构约束的扫描与提醒。

range 规划阶段和 gen/validator 编写阶段都会用到：
- range_agent 用它给 LLM 一份「题面里命中了哪些结构约束」的提示，让 LLM 把约束写进 range.json 的 special_constraints 字段；
- runner 用它给写 gen 的 Agent 一份针对性提醒，确保 gen 真正保证该性质。

两边共用同一份关键词表，避免重复维护。
提示中优先推荐 ACM-generator（generator.h）API，减少 LLM 手写慢/错的构造。
"""

# 题面里出现这些关键词时，说明题目对图/序列的结构有「特殊约束」。
# 这种约束往往是标程算法的隐含假设（比如「哈密顿路径计数」标程可能假设图存在唯一拓扑序），
# 生成器必须真正保证该性质，否则数据虽然格式合法但语义错误，标程答案无意义。
# 每条命中后追加的提醒会拼到 task 里，让 Agent 在写 gen 时显式处理。
_STRUCT_CONSTRAINT_HINTS = [
    {
        "keywords": ("哈密顿", "hamilton", "hamiltonian"),
        "title": "哈密顿路径/回路",
        "hint": (
            "题面涉及哈密顿路径/回路。生成器不能只造普通图：必须保证生成的图"
            "「真的存在哈密顿路径/回路」，否则标程算出的答案与题意不符。"
            "推荐做法：先固定一条总序链（可用 unweight::Chain 作骨架）保证存在性，"
            "再在链上随机加前向边（i<j）扩充边数；validator 用 ensuref 显式校验"
            "「图存在哈密顿路径」或至少校验「存在唯一汇点 / 拓扑序覆盖所有点」。"
            "edge_cases 至少含 edge_hamiltonian_chain（纯链）和 edge_hamiltonian_extra（链+额外边）。"
        ),
    },
    {
        "keywords": ("欧拉", "euler", "eulerian"),
        "title": "欧拉路径/回路",
        "hint": (
            "题面涉及欧拉路径/回路。生成器必须保证图满足欧拉条件"
            "（无向图：0 或 2 个奇度点；有向图：入度出度匹配或恰一对源汇）。"
            "推荐做法：先构造一条欧拉回路/路径作为骨架，再在保持度数平衡的前提下加边。"
            "validator 用 ensuref 校验度数条件。"
            "edge_cases 至少含 edge_euler_circuit 和 edge_euler_path。"
        ),
    },
    {
        "keywords": ("平面图", "planar"),
        "title": "平面图",
        "hint": (
            "题面要求图是平面图。生成器不能用任意随机图：必须保证可平面嵌入"
            "（如外平面图、链+局部边、K4 子结构受控）。"
            "推荐：unweight::Chain / Tree / GridGraph 作为骨架，避免 K5/K3,3。"
            "validator 用 ensuref 校验边数 <= 3n-6（n>=3）作为必要条件，"
            "或干脆只生成已知平面结构（链、树、外平面图、网格图）。"
        ),
    },
    {
        "keywords": ("竞赛图", "tournament"),
        "title": "竞赛图",
        "hint": (
            "题面要求图是竞赛图（完全有向图，每对点恰好一条有向边）。"
            "生成器必须输出恰好 n*(n-1)/2 条边且每对 (u,v) 恰有一条。"
            "推荐做法：枚举所有 (u,v) u<v，随机决定方向。validator 用 ensuref 校验边数与方向唯一性。"
        ),
    },
    {
        "keywords": ("二分图", "bipartite"),
        "title": "二分图",
        "hint": (
            "题面要求图是二分图。优先用 unweight::BipartiteGraph(n, m)；"
            "或手写：先随机划分左右部，再只在左右部之间连边。"
            "validator 用 ensuref + BFS 染色校验无奇环。"
            "edge_cases 至少含 edge_bipartite_balanced 和 edge_bipartite_unbalanced。"
        ),
    },
    {
        "keywords": ("弦图", "chordal"),
        "title": "弦图",
        "hint": (
            "题面要求图是弦图（每个长度>=4 的环都有弦）。生成器推荐用「完美消除序」构造："
            "按序加点，每个新点连到之前某区间内的所有点。validator 校验存在完美消除序。"
        ),
    },
    {
        "keywords": ("连通图", "connected", "保证连通"),
        "title": "连通图",
        "hint": (
            "题面要求图连通。推荐：先 unweight::Tree 作生成树骨架，再加额外边；"
            "或 unweight::Graph 并确保连通参数。validator 用 ensuref + 并查集/BFS 校验连通分量数=1。"
            "edge_cases 至少含 edge_connected_tree 和 edge_connected_dense。"
        ),
    },
    {
        "keywords": ("DAG", "有向无环", "directed acyclic", "拓扑"),
        "title": "DAG / 有向无环图",
        "hint": (
            "题面要求图是 DAG。优先用 unweight::DAG(n, m)；"
            "或手写：固定顶点排列作拓扑序，只在前向（i<j）连边。"
            "validator 用 ensuref + Kahn 拓扑校验无环。"
            "edge_cases 至少含 edge_dag_chain（纯链）和 edge_dag_extra（链+前向边）。"
        ),
    },
    {
        "keywords": (
            "为根", "指向孩子", "父亲指向", "恰有一个父亲", "恰好有一个父亲",
            "没有边指向", "parent", "rooted tree", "u→v", "u → v",
        ),
        "title": "有根有向树",
        "hint": (
            "题面要求有根有向树（根 R、边由父亲指向孩子）。生成器禁止无根默认："
            "unweight::Tree t(n); t.set_is_rooted(true); t.set_root(R); t.set_output_root(false); "
            "t.gen(); 再 cout << t 或按 e.u()→e.v() 打印（边须父→子）。"
            "Chain/Flower 需要方向时同样 set_is_rooted(true)+set_root(R)。"
            "禁止依赖无根 Tree 的随机端点交换（未 set_is_rooted 就 cout << t 易打出指向根的边）。"
            "手写边永远打印「父亲 孩子」。"
            "validator：ensuref(v!=R)；非根 indeg==1；并查集连通（边数=n-1、无自环）。"
        ),
    },
    {
        "keywords": ("树", "tree"),
        "title": "树",
        "hint": (
            "题面要求图是树（n-1 条边、连通、无环）。优先用 generator.h："
            "unweight::Tree / Chain（链）/ Flower（菊花）/ FlowerChain / MaxSonTree。"
            "仅当题面为无向树（无根、无父→子方向）时，才可裸写 t.gen(); cout << t；"
            "若题面写根 / 父亲 / 有向边 / u→v，禁止无根默认，必须按「有根有向树」提醒："
            "set_is_rooted(true); set_root(R); set_output_root(false); 再 gen()。"
            "自定义顺序用 for (auto &e : t.edges())。"
            "get_edges() / t.shuffle() 不存在，写错会编译失败。"
            "validator：边数=n-1、无自环、无重边、连通、无环；有根时再验无边指向根 + 非根恰一父亲。"
        ),
    },
    {
        "keywords": ("凸包", "convex hull", "convex"),
        "title": "凸包 / 点集几何",
        "hint": (
            "题面涉及凸包或平面点集。优先用 ConvexHull<int> / SimplePolygon<int> / RandomPoints<int>，"
            "set_xy_limit 后 gen()，cout << obj 输出。validator 校验点数与坐标范围，必要时校验凸性。"
        ),
    },
    {
        "keywords": ("基环树", "伪树", "pseudotree", "cactus", "仙人掌"),
        "title": "基环树 / 仙人掌",
        "hint": (
            "题面涉及基环树或仙人掌。优先用 unweight::PseudoTree / PseudoInTree / PseudoOutTree / Cactus。"
            "validator 按题意校验环数/块结构。"
        ),
    },
]


def scan_structural_hints(text: str) -> str:
    """扫题面文本，命中特殊结构约束时返回拼好的提醒块；未命中返回空串。

    用于在 runner 拼 task 时追加针对性提醒，避免 Agent 写出「格式合法但语义错误」的数据。
    """
    if not text:
        return ""
    low = text.lower()
    hits = []
    for spec in _STRUCT_CONSTRAINT_HINTS:
        for kw in spec["keywords"]:
            if kw.lower() in low:
                hits.append(spec)
                break
    if not hits:
        return ""
    lines = ["\n\n【题面特殊结构约束 — 生成器必须显式保证】"]
    seen_titles = set()
    for spec in hits:
        if spec["title"] in seen_titles:
            continue
        seen_titles.add(spec["title"])
        lines.append(f"\n● {spec['title']}\n{spec['hint']}")
    lines.append(
        "\n以上结构约束必须在 gen.cpp 的对应 --type 分支里真正实现，"
        "优先 #include \"generator.h\" 使用对应 API，"
        "并在 validator.cpp 用 ensuref 显式校验；不要只写 random 分支指望碰运气。"
    )
    return "".join(lines)


def scan_structural_titles(text: str) -> list[str]:
    """扫题面文本，返回命中的结构约束标题列表（去重保序）。

    供 range_agent 把命中的约束名写进 range.json 的 special_constraints 字段，
    让后续 gen/validator 编写阶段能看到一份明确的约束清单。
    """
    if not text:
        return []
    low = text.lower()
    titles = []
    seen = set()
    for spec in _STRUCT_CONSTRAINT_HINTS:
        for kw in spec["keywords"]:
            if kw.lower() in low:
                if spec["title"] not in seen:
                    seen.add(spec["title"])
                    titles.append(spec["title"])
                break
    return titles
