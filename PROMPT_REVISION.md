# 提示词修改建议：公共部分与题型特指拆分

> 面向 `agent/prompts/`（`core.py` / `types.py` / `stages.py` / `builders.py`）。目标：缩短无关题型的上下文、减少重复、补齐薄弱题型的 gen 注意事项。

---

## 1. 现状与问题

### 1.1 组装链路（简图）

```text
builders.build_*_prompt(problem_type)
  ├─ core：COMMON / WRITE_CONTENT_GATE / SCALE / MULTI_TEST / PERF / BASE_GEN_* / BASE_VAL_*
  ├─ stages：CODER / FIXER / _GEN_API_GATE / _GEN_API_GATE_BY_TYPE
  └─ types：_TYPE_MODULES[problem_type]   ← 本应按题型注入
```

`_TYPE_MODULES` 已按 `problem_type` 注入，但**公共块里仍夹着大量题型 API**，导致：

| 现象 | 后果 |
| --- | --- |
| `BASE_GEN_API_MANUAL`（约 4.5k 字）含树/图/几何/数组全手册 | 做 string/array 题仍塞进 Tree/Graph 全文 |
| `_GEN_API_GATE` + `_GEN_API_GATE_*` 与 `TYPE_*` 内容重叠 | 同一规则出现 2～3 次，挤占有效注意力 |
| `number_theory` / `dp` / `range_query` / `multi_test` 复用 `TYPE_ARRAY` | 判型有了，但无专属构造门禁 |
| `interactive` 模块为空串 | 交互题几乎无 gen 指引 |
| `permutation` 在 `types.py` 有模块，但不在 `KNOWN_PROBLEM_TYPES` | 无法被 Range 写出，模块形同虚设 |

### 1.2 建议的分层原则

| 层级 | 放什么 | 示例 |
| --- | --- | --- |
| **公共硬约束** | 与题型无关、违反必炸 | 标程为准、opt/type 样板、`rnd.next(lo,hi)`、`WRITE_CONTENT_GATE`、多测 remain、PERF/K、validator 读串/收尾 |
| **题型模块 `TYPE_*`** | 仅该题型需要的 API、构造策略、常见 edge、ensuref | 树用 `unweight::Chain`；串用「一次枚举 (p1,p2)」 |
| **结构特指**（已有） | `struct_hints`：哈密顿/欧拉/DAG… | 继续走 task 侧注入，不必塞进 system 公共块 |
| **阶段特指** | Planner / Coder / Fixer / Checker 流程 | 留在 `stages.py`，勿与题型手册混写 |

一句话：**公共块只保留「任何 gen 都要遵守」；题型特指只在命中该 `problem_type` 时注入。**

---

## 2. 公共部分：建议拆出 / 保留

### 2.1 应从 `BASE_GEN_API_MANUAL` 拆到题型模块的内容

当前手册大段可按题型搬走（公共只留 1～2 行「细则见题型模块」指针）：

| 手册段落 | 迁往 |
| --- | --- |
| 四种权重命名空间 + 树构造/默认 cout | `TYPE_TREE`（加权变体可共用） |
| 图构造 / Bipartite / DAG / Forest / 边数 API | `TYPE_GRAPH` |
| 几何 ConvexHull / Point / 坐标范围 | `TYPE_GEO` |
| `rand_p` / `rand_string` / `rand_sum` / `rand_vector` | 拆到 `TYPE_ARRAY` / `TYPE_STRING` / `TYPE_PERMUTATION`；`rand_sum` 可再挂 `TYPE_MULTI` |

`build_full_prompt` 今日把整本 `BASE_GEN_RULES`（CORE + MANUAL）无条件塞入；Coder 路径已只用 `BASE_GEN_RULES_CORE`。建议：

1. **废弃「全量手册无条件注入」**：`build_full_prompt` 与 SpecialCoder 对齐 Coder——只注入 CORE + 对应 `TYPE_*`
2. **`_GEN_API_GATE` 通用段**只保留命名空间硬门禁与「禁止虚构 API」清单；树/图/几何/数组细节完全依赖 `_GEN_API_GATE_BY_TYPE` ∪ `TYPE_*`，二者择一并去重（推荐以 `TYPE_*` 为正文，`_GEN_API_GATE_*` 删或改成 `TYPE_*` 的短引用）

### 2.2 建议继续留在公共 CORE 的内容

这些与题型无关，拆出去反而每题重复或漏检：

- 标程读入 = 输入格式真相；规模头以 plan 第 1 节为准
- `--seed` / `--type`(string) / `--index` / `--count` 分支前全部 `opt`
- `rnd.next` 的 `lo ≤ hi`；禁空候选采样；禁无上限拒绝采样
- 输出只打输入、禁打答案；定长拼装用 `L - used`；超 `long long` 用十进制串
- MULTI_TEST remain 拆分；SCALE 小中大全组合；PERF 两套钟与 K
- validator：`strict=false`、`readToken`、`skipBlanks+readEof`、`readLong` 带 `LL`

「多模式植入 / 先采再滤」目前同时出现在公共 CORE、Coder 规则与 `TYPE_STRING`。建议：**细则只留在 `TYPE_STRING`**；公共只留一句「有多段定长模式约束时见字符串题型模块」。

---

## 3. 各题型写 gen 的注意事项（含缺口）

下列按「已有强度 → 建议补什么」整理。edge 名仅为示例，总额仍须遵守 4～6。

### 3.1 `tree` / `weighted_tree`（已较强）

**已有**：权重前缀、`Chain`/`Flower`/`HeightTree`、多字段边手写、`cout` 对齐标程、validator 树性质。

**建议补**：

- 有根 / 无根 / 父数组格式（`p_2…p_n`）与「n-1 行边」的切换清单
- 点权 / 边权 / 双权与「多权字段 a,b」选型表（何时绝不用 `edge_weight::`）
- 常见 edge：`chain` / `star` / `flower_chain` / `high_depth` / `edge_n2` / `edge_nmax`
- 大档：树形结构可打满 `n`，但点权/边权种类 ≤ K（勿与「满边权上界」混为一谈）

### 3.2 `graph` / `weighted_graph`（已较强）

**已有**：方向/重边/自环/连通开关、二分图/DAG、点编号 1-based、禁 `O(n^2)` 边池。

**建议补**：

- `m` 相对 `n` 的稀疏 / 稠密档与 `max_edge_count()` 钳位写法
- 与 `special_constraints` 的衔接指针（细节可继续靠 `struct_hints`）：连通、DAG、二分、欧拉、哈密顿、平面、竞赛图
- 询问图（先图后 `q` 组询问）的输出顺序与「满 `q` ≠ 满唯一查询键」分层
- 常见 edge：`path` / `star` / `random_sparse` / `disconnected` / `edge_n1`（禁自环时 `m=0`）

### 3.3 `geometry`（中等）

**已有**：ConvexHull / SimplePolygon / Triangle、坐标范围、共线允许。

**建议补**：

- 退化：全共线、重复点、三点共线三角形、坐标贴上下界
- 整数坐标防溢出：叉积用 `__int128` / 转 long double 的 **validator/标程侧**提醒（gen 仍输出坐标）
- `set_max_try` 失败兜底：缩小 `n` 或改 SimplePolygon，禁止无上限重试
- 常见 edge：`collinear` / `convex_hull` / `all_same_point`（若题面允许）/ `edge_n3`

### 3.4 `string`（已较强，近期踩坑已沉淀）

**已有**：字符集、`rand_string`、多模式「一次枚举合法对」、定长补齐、`readToken`。

**建议补**：

- 字母表大小分层（`|Σ|=2` vs 26）与 K：大档循环短种子串，勿每位置全新随机却声称控种类
- 周期串 / 边界失败（KMP 类）：`period`、`almost_period`、`border_heavy`
- 括号序 / 回文：优先库函数；非法括号仅当题面允许再开 edge
- 多测字符串 + `sum_|s|`：remain 拆长度，与 MULTI_TEST 对齐一句即可

### 3.5 `array`（偏薄）

**已有**：`rnd.next`、排列、`rand_sum`、常见单调/全相等 edge。

**建议补**：

- 有序 / 逆序 / 近似有序 / 两值 / 全极值 / 含负数零
- 离散化敏感：中档拉高唯一值，大档 ≤ K 复用（已与 PERF 呼应，写进 TYPE 更醒目）
- 下标 0/1、闭开区间与标程一致的自检句

### 3.6 `permutation`（模块有、枚举无）

**缺口**：不在 `KNOWN_PROBLEM_TYPES` / few-shot，Range 写不出该类型。

**建议**：

1. 纳入 `KNOWN_PROBLEM_TYPES`，或明确「排列题归 `array`，删独立模块」二选一
2. 若保留：补「多重集排列 / 环 / 交换序列 / 逆序数极端」edge；validator「恰好排列」ensuref 模板

### 3.7 `matrix`（偏薄）

**已有**：数值矩阵 + `GridGraph` 一行。

**建议补**：

- 输出是「`n m` + `n` 行」还是「一行展平」必须以标程为准
- 全 0 / 单位 / 对角 / 单行单列 / 反对角；网格图连通与墙（若 01 迷宫）
- 大档：矩阵规模打满时元素种类 ≤ K

### 3.8 `number_theory`（复用 ARRAY，缺口大）

Few-shot 已有素数/互素/全偶等，但 **TYPE 无专属条文**。

**建议新建 `TYPE_NUMBER_THEORY`**：

- 构造优先「因式分解友好」：固定小素数池、`gcd` 可控（先采样 `g` 再乘系数）、质数表预生成（禁对每个 `ai` 试到 `√1e9` 无上限）
- 模数 / 逆元题：保证与模互素；禁止大档暴力判素数导致 gen 超时
- 组合数 / 阶乘相关：`n` 受 mod 限制时写清上界，避免无意义超模数据
- 常见 edge：`all_prime` / `all_equal` / `coprime_pair` / `all_even` / `powers_of_two` / `edge_n1`

### 3.9 `dp`（复用 ARRAY，缺口大）

**建议新建 `TYPE_DP`**（或 `array` 子节「DP 敏感」）：

- 背包：容量 `W` 与物品体积的小中大组合；`all_heavy`（单件就近 `W`）、`many_tiny`
- 区间 / 序列 DP：长度分层 + 值域 ≤ K（防 `O(n²·V)` 标程炸）
- 树形 DP：应判 `tree` 而非 `dp`；Range 提示里强调「输入主体是树 → tree」
- 转移高度依赖「种类 / 段数」时：中档测多样，大档控段数/颜色数 ≤ K

### 3.10 `range_query`（复用 ARRAY，缺口大）

**建议新建 `TYPE_RANGE_QUERY`**：

- 操作序列：`q` 与结构规模双轴；更新 / 查询比例（全更新空输出合法见 `RULES`）
- 时间线敏感：同一下标高频改、整段覆盖、随机点查
- 禁「满 `n` + 满 `q` + 每条操作新值」三联拉爆；大档值域 ≤ K
- 常见 edge：`q1` / `qmax` / `point_queries` / `range_all` / `only_update` / `only_query`

### 3.11 `multi_test`（复用 ARRAY，缺口中等）

公共已有 `MULTI_TEST`，但题型模块未强化「本类型的主矛盾就是 T×sum」。

**建议 `TYPE_MULTI_TEST` 短模块**：

- 强制 remain；禁 `edge_T1`；推荐 `edge_Tmax` / `big_T_small_n` / `edge_nmax`
- 组内结构仍跟真实子题型（数组/串/图）走——可写「子构造复用对应 TYPE，本模块只管 T/sum」
- 避免把一切多测题都标成 `multi_test` 而丢掉 tree/graph 模块：Range 原则改为「有 T+sum 时注入 MULTI_TEST 公共块；`problem_type` 仍按输入主体」

### 3.12 `interactive`（空模块，缺口最大）

**建议至少落地最小 `TYPE_INTERACTIVE`**：

- 本框架若以「非交互批数据」为主：写明「交互题降级为：按交互协议可还原的静态输入」或「暂不支持，Range 勿选」
- 若支持：gen 只生成初始局面 / 询问上界；禁止伪造交互中间态当 `.in`；checker/interactor 职责边界一句

### 3.13 跨题型、公共里仍偏弱的点（建议补公共或独立短块）

| 主题 | 建议 |
| --- | --- |
| 构造题（输出方案由标程算） | 已有「gen≠标程」；可再强调：合法性由 validator 保输入，勿在 gen 里预校验答案 |
| 多解 / SPJ | 留在 Checker 提示；gen 侧只保证输入多样，不保证唯一答案 |
| 大整数 / 高精度 | CORE 已有字符串构造；可加「位数轴」小中大（位数而非数值） |
| 概率 / 期望题 | 固定种子可复现；避免依赖极低概率事件作为唯一正确路径 |
| 函数 / 多项式 / 生成函数 | 缺模块；可暂归 `array`，补「系数种类 ≤ K、度数分层」 |
| 配对堆 / 可并堆等冷门结构 | 靠 `special_constraints` + struct_hints 扩展，不必新 problem_type |

---

## 4. 推荐改造步骤（实施顺序）

### P0 — 拆分与去重（收益最大）

1. 将 `BASE_GEN_API_MANUAL` 按树/图/几何/数组函数拆入对应 `TYPE_*`，CORE 仅保留通用流程一句
2. `build_full_prompt` / SpecialCoder 停止整本手册注入，改为 CORE + `TYPE_*`
3. 合并 `_GEN_API_GATE_BY_TYPE` 与 `TYPE_*` 重复段落，只维护一处
4. 把「多模式植入」细则从公共/Coder 长文缩成引用，正文只留 `TYPE_STRING`

### P1 — 补齐薄弱题型模块

1. 新增：`TYPE_NUMBER_THEORY`、`TYPE_DP`、`TYPE_RANGE_QUERY`、`TYPE_MULTI_TEST`（短）、`TYPE_INTERACTIVE`（明确支持边界）
2. 充实：`TYPE_ARRAY`、`TYPE_MATRIX`、`TYPE_GEO` 的 edge 与分层注意
3. 决定 `permutation`：进 `KNOWN_PROBLEM_TYPES` 或并入 `array` 并删除死模块
4. Range 分类原则：修正「多测 ⇒ 只能 multi_test」的倾向，改为主体题型 + 条件注入 `MULTI_TEST`

### P2 — 校验与回归

1. `tests/test_prompts.py`：断言 `build_coder_prompt("string")` **不含** `unweight::Tree` 长手册；`build_coder_prompt("tree")` **含** Chain/Flower 门禁
2. 对各 `problem_type` 做字符数/关键词快照，防止公共块回潮
3. 与 `struct_hints` / few-shot 的 edge 命名对照表（避免 hint 要求 `edge_hamiltonian_*` 而 TYPE 从未提及）

---

## 5. 拆分后的目标形态（示意）

```text
# 任意题型都有
WRITE_CONTENT_GATE
COMMON_CORE + SCALE + (MULTI_TEST?) + (PERF?)
BASE_GEN_RULES_CORE      # 无长手册
BASE_VAL_RULES
阶段提示（Coder/Fixer/…）

# 仅命中时追加
TYPE_TREE | TYPE_GRAPH | TYPE_STRING | TYPE_NUMBER_THEORY | …
（可选）struct_hints 已在 task，不进 system
```

预期效果：string/array 类 Coder 提示明显变短；树/图仍保留完整 API；数论/DP/区间查询不再「空挂 TYPE_ARRAY」。

---

## 6. 自检清单（改提示词时用）

- [ ] 该句是否对**所有**题型成立？是 → 公共；否 → `TYPE_*`
- [ ] 同一规则是否在 CORE / GATE / TYPE / Coder 出现超过一次？→ 留一处 + 短引用
- [ ] 新题型模块是否含：推荐 API、禁止写法、validator 要点、4～6 个示例 edge、与 K/分层的关系
- [ ] `KNOWN_PROBLEM_TYPES`、`_TYPE_MODULES`、`_GEN_API_GATE_BY_TYPE`、few-shot 四表是否同增同删
- [ ] 修改后跑 `tests/test_prompts.py`，并抽一题 string、一题 tree 看注入是否对症
