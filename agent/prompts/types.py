"""Problem-type prompt modules."""
from __future__ import annotations

TYPE_TREE = """【树图题型模块 — tree / weighted_tree】
树题优先用 ACM-generator（#include "generator.h"；using namespace generator::all）：
  【硬门禁】类名必须带权重前缀。all 不会引入全局 Chain/Flower/Tree。
  正确：unweight::Tree / unweight::Chain / unweight::Flower / unweight::FlowerChain …
  错误：Chain ch(n); Flower fl(n);  → was not declared in this scope
  权重前缀：unweight:: / edge_weight::T / node_weight::T / both_weight::NodeT,EdgeT。
  按边界选型与关键方法：
    unweight::Tree t(n)：一般随机树；use_random_father() 或 use_pruefer()；set_is_rooted / set_root
    unweight::Chain ch(n) / unweight::Flower fl(n)：链 / 菊花（星）
    unweight::FlowerChain fc(n)：set_flower_size(k) 或 set_flower_chain_size(fs, cs)
    unweight::HeightTree ht(n)：强制有根；set_height(h)；禁用 set_is_rooted
    unweight::MaxDegreeTree md(n)：set_max_degree(d)
    MaxSonTree / DegreeTree / SonTree：同样必须 unweight:: 或对应权重前缀
  流程：构造 →（可选 set_begin_node(1)）→ gen() → cout 或遍历 edges()
  【默认输出可用时 · 仅无向无根树】
      unweight::Tree t(n); t.gen(); cout << t << "\\n";
  【有根/有向 · 硬门禁】题面或 special_constraints 要求根 R、边父亲→孩子时：
      unweight::Tree t(n);
      t.set_is_rooted(true); t.set_root(R); t.set_output_root(false);
      t.gen(); cout << t;  // 或 edges() 按 e.u()→e.v() 打印
    禁止：未 set_is_rooted 就 cout << t（会随机 swap，易出指向根的边）。
    Chain/Flower 同理需要方向时也要有根。
    手写边：永远打印父亲 孩子，禁止孩子 父亲。
  【对齐标程 · 多字段边】禁止盲 cout << t：
      unweight::Tree t(n); t.set_begin_node(1); t.gen();
      for (auto &e : t.edges()) printf("%d %d %d %d\\n", e.u(), e.v(), a, b);
  【类型】Chain/Flower 与 Tree 并列，不可互相当 Tree&；
  共享边输出用 [&](auto &t){ t.gen(); for (auto &e : t.edges()) …; }，或分支内联。
  【单边权】
      edge_weight::Tree<int> t(n);
      t.set_edges_weight_function([](){ return rnd.next(1, 1000000000); });
      t.gen(); cout << t;
  【多权字段 a,b】用 unweight:: + 自己采样打印，不要虚构 weight::。
  开关：set_output_node_count(false) / set_output_root(false) / set_begin_node(0或1)。
  【规模头】默认「n → 边」仅当标程如此；标程无规模头时必须关输出或手写边。
  【严禁】裸 Chain/Flower/Tree；weight:: / set_weight_limit / get_edges() / t.shuffle() / _edges / rnd.next(...,1e9)。
  仍须 registerGen + --seed/--type/--index/--count；禁止 fill_inputs/hack/init_gen。

树图默认按「无自环、无重边」处理；题面允许则另说。
树 validator：边数=n-1、无自环、无重边、连通、无环；带权再验权值范围；
  有根时再验「无边指向根 + 非根恰一父亲」。
"""

TYPE_GRAPH = """【图题型模块 — graph / weighted_graph】
优先 ACM-generator（能用 Graph/Tree/Chain/… 就用）；仅库表达不了的约束（如度数上限）再手写边。
输入格式以标程为准。using namespace generator::all;
  【硬门禁】类名必须带前缀：unweight::Graph / unweight::DAG / …；禁止裸 Graph。
  结构选型与方法：
    unweight::Graph g(n,m)：通用图；set_direction / set_multiply_edge / set_self_loop / set_connect
    unweight::BipartiteGraph(n,m[,left])：set_left / set_left_right / rand_left / set_different_part；
      首行格式 use_format_node|left_right|node_left|node_right（对齐标程）；禁 set_direction/self_loop
    unweight::DAG(n,m)：有向无环；禁 set_direction/self_loop
    unweight::CycleGraph / WheelGraph / GridGraph(n,m)：网格用 set_row / set_row_column / rand_row
    unweight::PseudoTree / PseudoInTree / PseudoOutTree：基环树族
    unweight::Cactus(n,m)：无向连通无重边无自环
    unweight::Forest(n,m)：add_tree_size / set_trees_size({…})
    unweight::StartReachableGraph：单源可达
  边数：min_edge_count() / max_edge_count() / rand_edge_count(lo,hi) /
    set_edge_count(min(m_limit, max_edge_count()))；严禁 O(n^2) 建边池
  【默认输出】
      unweight::Graph g(n, m); g.gen(); cout << g << "\\n";  // n m →（点权）→ m 行边
      set_output_node_count / set_output_edge_count 可关首行字段
  【规模头】默认「n m → 边」仅当标程如此；标程无规模头时禁止套用默认头，须关输出或手写边。
  【多字段边】for (auto &e : g.edges()) 自行打印；【单边权】edge_weight::Graph<int> + set_edges_weight_function + e.w()
  【点编号硬约束】题面点号通常为 1..n：禁止输出 0。
    Graph/Tree 默认 begin=1，e.u()/e.v() 可直接打印。
    手写边：内部全程 1-based（rnd.next(1,n)；路径边用 i↔i+1）；勿 for(i=0;i<n)。
    随机重标号：rnd.perm(n) 得到 0..n-1，映射必须 +1（或 rand_p(n,1)）；label[u]/label[v] 输出。
  【严禁】裸 Graph/DAG/Chain；weight:: / set_weight_limit / get_edges() / shuffle / _edges / rnd.next(...,1e9)。
  仍须 registerGen + seed/type/index/count；禁止 fill_inputs/hack/init_gen。

【多测】标程先读 T：首行必须是 T；仅此时可写 edge_Tmax（可选 big_T_small_n）；禁 edge_T1。
【n=1】禁自环 → m=0（可空）；允许自环 → (1,1,w)。采样须有尝试上限。
【完全图】控制 n 使边数 ≤ m 上界。空边集/空文件按约束允许。
【复杂度】遵守 gen_plan 第 4/7 节分层：小中档可多样，大档与满边上界 ≤K；满边/满询问 ≠ 唯一顶点/唯一键拉满。

图性质以题面为准；validator 校验同题面（u/v 用 readInt(1,n)）。
"""

TYPE_GEO = """【几何题型模块 — geometry】
优先 ACM-generator（二维）：using namespace generator::all;
  图形类：ConvexHull<T> / SimplePolygon<T> / Triangle<T>
    构造：Xxx(n, xL,xR,yL,yR)；或 Xxx(n) 后再设范围
    范围：set_xy_limit(xL,xR,yL,yR) 或 set_xy_limit("[-1e9,1e9]")；
          set_x_limit / set_y_limit（数值或范围字符串）
    生成：gen()；ConvexHull 可用 set_max_try(k)（默认 10，失败抛异常）
    输出：cout << obj → 默认先 n 再 n 行 x y；
          set_output_node_count(false) 可去掉首行 n；
          Triangle 特例：一行 x1 y1 x2 y2 x3 y3
  单点：Point<T> p; p.rand(L,R) / p.rand(xL,xR,yL,yR) / p.rand(format);
        或 rand_point<T>(…)
  约束：T 为有符号整型或浮点（禁 unsigned）；生成允许三点共线（非严格）。
  【严禁】RandomPoints 类；fill_inputs/hack/init_gen。
  仍须 registerGen + --seed/--type/--index/--count。

validator：点数、坐标范围，以及题面要求的凸性/简单多边形/共线/非退化。
"""

TYPE_ARRAY = """【数组 / 序列题型模块】
优先 testlib：vector + rnd.next(L,R)；落在 long long 内的大范围用 long long + rnd.next(-1000000000LL, 1000000000LL)。
【超 long long】元素/权值上界超出 64 位有符号整数时：禁止 long long/__int128 采样；
  用十进制字符串构造（rnd.next(\"[1-9][0-9]{L-1}\") 等），cout/printf 直接打串；validator 用 readToken。
【k 位小数 / 实数字段】题面或 special_constraints 要求一位/k 位小数时：在 [lo·10^k, hi·10^k] 整数域
  rnd.next，再按缩放打印（可含非整数，如 1.5）；禁止对该字段只用 %d / 纯 int 采样。
  validator：readDouble（或 readStrictDouble）；勿因 readDouble 能读整数就只生成整数。
排列：rnd.perm(n)（0..n-1，按题面 +1）。
可选 generator.h 函数（不是类）：
  rand_vector(…) 随机数组；
  rand_sum(k, S) / rand_sum(k,S,min_part) / rand_sum(k,S,from,to) —— 多测拆分 sum_* 优先用；
  rand_p(n) / rand_p(n, start) 排列。
【严禁】虚构类 Sequence / Permutation / String。type 用 string 分支。

validator：长度、元素范围（≤long long 用 readLong；更大用 readToken/pattern）、单调/互异等题面约束。
常见 edge：edge_n1, edge_nmax, all_equal, descending, all_negative, all_max_value, two_values, monotone, alternating。
- 大档：数值种类 ≤ K，用有限域复用凑满规模；禁止满 n 且每个元素全新大随机。
"""

TYPE_STRING = """【字符串题型模块】
优先 testlib：rnd.next(\"[a-z]{n}\") / rnd.next(\"[01]{n}\") / 逐字符 rnd.next('a','z')。
可选 generator.h：
  rand_string(n) / rand_string(n, LowerLetter|UpperLetter|…) /
  rand_string(n, \"[a-e]\") / rand_string(lo, hi, format)；
  rand_palindrome / rand_bracket_seq。
【严禁】String(n,'a','z') 类。type 用 string 分支。
【大整数按串】数值超出 long long / 位数很多时：按十进制数字串构造与校验（见公共规则「超 long long」）；
  不要用 long long/__int128 采样该字段。

【植入多个定长模式 / 保证含子串 · 硬门禁】
  题面要求至少含两个互不重叠定长模式（记长 LA、LB，如两段长度各为 3）时：
  1) 【唯一合法写法】一次枚举所有不重叠起点对 (p1,p2)，集合非空后再
     rnd.next(0, sz-1)；n 极小（n==LA+LB）时特判两种固定拼接顺序。
  2) 【严禁 · 先采再滤】禁止先 rnd 一个 p1，再把「与 p1 不重叠的 p2」推进 vector/pool
     后对 pool 做 rnd.next(0, size-1)：短串上 pool 常空 → n must be positive 崩溃。
  3) 禁止：用 p±len 拆左右区间却不保证 lo≤hi；禁止无上限 while(重叠)重采。
  4) 拒绝采样若保留：必须有上限；用尽 → 改枚举合法对，勿把 random 退化成永远同一固定串。
  5) pattern_at_start/end 等 edge 才用「固定首/尾」；random 小档也要多样且 n=LA+LB 可跑通。

  【反例 · 禁止】（先 p1 再 pool——短串必炸）
    int p1 = rnd.next(0, n - LA);
    vector<int> pool;
    for (int x = 0; x + LB <= n; ++x)
      if (不与 p1 重叠) pool.push_back(x);
    int p2 = pool[rnd.next(0, (int)pool.size() - 1)];  // size==0 → 崩溃

  【正例 · 必须】
    if (n == LA + LB) { /* 两种拼接顺序特判 */ return; }
    vector<pair<int,int>> cand;
    for (int p1 = 0; p1 + LA <= n; ++p1)
      for (int p2 = 0; p2 + LB <= n; ++p2)
        if (不重叠(p1,LA,p2,LB)) cand.push_back({p1, p2});
    auto [p1, p2] = cand[rnd.next(0, (int)cand.size() - 1)];  // n>=LA+LB 时 cand 恒非空

validator：读 S 必须 readToken / readToken(\"[a-z]{…}\")；禁止 readInt(T) 后 readString/readLine。
  再 ensuref 字符集、长度、子串/前后缀/周期等。
【定长串】满长度用 L - 已用长度补齐；禁止「块长 + 手写填充个数」口算导致 ≠L。
常见 edge：edge_n1, edge_nmax, all_same, pattern_at_start/end, no_match, long_run, two_chars。
"""

TYPE_PERMUTATION = """【排列题型模块】
优先 testlib：rnd.perm(n) → 0..n-1，再整体 +1 得 1..n。
或 generator.h：rand_p(n) / rand_p(n, start)；子集排列用 unordered_set 去重采样。
- 子集排列：从 1..n 中选出 k 个再做排列；k 小时直接枚举，k 大时用 unordered_set 采样。
- 特殊结构：逆序数极端（正序 / 逆序）、相邻差受限、循环节少。
- 常见 edge：edge_n1, edge_nmax, sorted, reversed, few_swaps, fixed_point_free。
- 大档：若排列只是下标，n 可打满；若带值域，种类 ≤ K。
- 严禁 Permutation 类。type 用 string 分支。

validator：长度、范围 1..n、无重复。
- 注意：若题目本质是「排列数组」但无特殊排列约束，可归 array。
"""

TYPE_MATRIX = r"""【矩阵 / 网格题型模块】
数值矩阵：testlib rnd.next 填 vector<vector<int>>；输出按标程：先 n m 再 n 行，或按展平。
网格图：unweight::GridGraph g(n, m); g.set_row(r) 或 set_row_column(r,c,ignore); g.gen();
  再 cout << g 或遍历 edges()；可 set_direction(true) 做有向网格。
- 退化：全 0、全 1、单位阵、对角 / 反对角、单行 / 单列。
- 迷宫 / 网格：保证起点到终点有路（先铺一条主路径再随机加墙），或按题面允许不可达。
- 大档：矩阵元素种类 ≤ K，禁止 n×m 打满且每个格随机大值。
- 常见 edge：edge_11, edge_nmax, row, col, all_zero, diagonal, anti_diagonal。
type 用 string 分支。

validator：行列规模、元素范围、题面连通 / 对称等约束。
"""

TYPE_NUMBER_THEORY = r"""【数论题型模块】
数值构造优先让标程能 cheaply 验证：先固定小素数池 / gcd / 因子 / 模数，再生成。
- 素数：预生成小素数表（如 ≤1e6），从大素数中抽样；禁止对每个 ai 做无上限试除。
- gcd / lcm：可先生成基 g，再乘互素系数，保证 gcd 可控。
- 逆元 / 同余：保证数值与模数互素；若要求非互素，按题面单独开 edge。
- 组合数 / 阶乘：n 受模数限制，大档不超过 K 个不同阶乘值。
- 模意义：用 long long；超 long long 见公共规则字符串构造。
- 常用 testlib：rnd.next / rnd.perm；rand_sum 用于多测拆分。
- 常见 edge：edge_n1, edge_nmax, all_equal, all_prime, coprime_pair, all_even, powers_of_two, square_free。
- 大档：数值种类 ≤ K（如固定小素数池循环使用），禁止满 n 且每个数全新大随机。
- 数论 validator：范围、互素（gcd=1）、素数、同余、模数约束等。
"""

TYPE_DP = r"""【动态规划 / 背包题型模块】
输入主体是序列 / 数组 / 物品时按 array 模块生成；额外注意 DP 转移敏感点。
- 背包：容量 W 与物品数量 / 体积小中大全覆盖；edge all_heavy（单件体积接近 W）、many_tiny（体积极小）、exact_fit（总体积 = W）。
- 区间 / 序列 DP：中档拉高元素种类（1e3～5e3），大档种类 ≤ K，防止 O(n²·V) 标程炸。
- 树形 DP：若输入主体是树，应判 tree 而非 dp；本模块不覆盖树形。
- 状态压缩：n 一般 ≤ 20，不要对 n=1e5 用状压。
- 常见 edge：edge_n1, edge_nmax, all_equal, all_zero, all_max_value, two_values, monotone。
- 大档：数值 / 颜色 / 段数种类 ≤ K，用有限域复用凑满规模；禁止「满 n + 满值域」。
- validator：范围、可选单调 / 互异 / 非负等。
"""

TYPE_RANGE_QUERY = r"""【区间查询 / 数据结构题型模块】
通常先输入结构（数组 / 序列 / 图），再读操作数 q。
- 双轴：结构规模 n 与操作数 q 都要小中大全覆盖；禁止 q 固定为 1 或 n 打满。
- 操作类型：更新 / 查询比例要多样；全更新导致空 .out 是合法答案（见公共规则）。
- 询问分布：point_queries / range_all / 随机区间；保证覆盖边界点。
- 大档：操作值域 ≤ K（如更新值只从固定池取），防止标程 O(q log n) 内部常数爆炸。
- 常见 edge：edge_n1, edge_nmax, q1, qmax, point_queries, range_all, only_update, only_query。
- 多测：按公共 MULTI_TEST 拆分 sum_n / sum_q。
- validator：结构字段 + 操作字段范围、下标合法、操作类型合法。
"""

TYPE_MULTI_TEST = r"""【多测题型模块】
本类型只强化「多测 + sum」约束；具体每组数据仍按真实主体题型（array / string / tree / …）构造。
- 必须按公共 MULTI_TEST 做 remain 拆分：先定 T，再 n_i = min(hint, remain / (T - i))。
- 禁 edge_T1；T = 1 已被 edge_nmax / 攻规模覆盖。
- 推荐 edge：edge_Tmax（或 big_T_small_n）、edge_nmax / small_T_big_n、sum_full。
- 大 T 强制小 n；大 n 强制小 T；禁止双顶格。
- 多测 validator：T 范围、sum_* 校验。
- 注意：输入主体是树 / 图时，problem_type 仍写 tree / graph，系统会额外注入 MULTI_TEST。
"""

TYPE_INTERACTIVE = r"""【交互题型模块】
当前框架以非交互批数据为主。若 problem_type 为 interactive：
- 默认降级：生成能让交互协议 offline 可运行的初始输入（如第一次询问前的局面）。
- 禁止在 gen.cpp 里伪造交互中间态（询问 / 回答序列）作为 .in。
- 真正的交互判定由 checker / interactor 负责，本阶段只保证初始输入合法。
- 若题目无法降级，请在 range.json 中明确说明，并直接 finish。
- 常见：先输出初始局面规模，再按主体题型（array / graph / …）构造该局面。
"""

_TYPE_MODULES = {
    "tree": TYPE_TREE,
    "weighted_tree": TYPE_TREE,
    "graph": TYPE_GRAPH,
    "weighted_graph": TYPE_GRAPH,
    "geometry": TYPE_GEO,
    "array": TYPE_ARRAY,
    "string": TYPE_STRING,
    "number_theory": TYPE_NUMBER_THEORY,
    "dp": TYPE_DP,
    "matrix": TYPE_MATRIX,
    "range_query": TYPE_RANGE_QUERY,
    "multi_test": TYPE_MULTI_TEST,
    "interactive": TYPE_INTERACTIVE,
    "permutation": TYPE_PERMUTATION,
}
