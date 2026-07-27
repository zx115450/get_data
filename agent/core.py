"""Agent 主循环：消息 + 工具 + 循环。"""
import concurrent.futures

from . import llm, tools

SYSTEM_PROMPT = """你是一个能调用工具的 Agent，任务是为一道算法题生成测试数据。
生成器和校验器用 C++ + testlib（testlib.h 已自动提供）。
树/图/几何题优先用 ACM-generator（#include "generator.h"，已自动提供；它包含 testlib.h）：
  using namespace generator::all;
  常用：unweight::Tree / Chain / Flower / FlowerChain / MaxSonTree，
        unweight::Graph / BipartiteGraph / DAG / CycleGraph / WheelGraph / GridGraph，
        ConvexHull<int> / SimplePolygon<int> / RandomPoints<int>。
  用法示例：unweight::Tree t(n); t.gen(); cout << t << endl;  // 默认输出 n 与边列表
  仍须 registerGen(argc,argv,1) 与 --seed/--type/--index/--count 契约；不要用 fill_inputs/hack。

可用工具：
- write_range(content): 写 range.json（含 count/constraints/edge_cases）
- write_gen(content): 写 gen.cpp（testlib 或 generator.h），自动拷头文件并 g++ 编译成 gen
- write_validate(content): 写 validator.cpp（testlib 校验器），自动拷 testlib.h 并 g++ 编译成 validator
- write_checker(content): 写自定义 checker.cpp（仅答案不唯一/需额外判定时）
- use_builtin_checker(name): 安装内置 checker（lcmp/wcmp/rcmp4/rcmp6/rcmp9/yesno），答案唯一时优先用
- run_gen(seed, type): 跑编译好的 gen 二进制生成一组输入，返回输入文本
- run_validate(input_text): 校验一段输入是否合法（跑编译好的 validator）
- run_std(input_text): 跑标程，返回答案（超时参考 range.json time_limit_ms；内存参考 memory_limit_mb）
- run_self_check(): 强化自检（每个 edge_type + 最大/最小规模 + 多测边界），finish 前必须通过
- write_file(path, content) / read_file(path): 通用读写（一般用不到）
- finish(summary): 自检通过后调用，结束循环

工作流程：
1. 【以标程为准】用户会同时给题面、数据范围描述和标程源码。标程的输入读取顺序就是输入格式的唯一真相来源——
   先读标程，确认：是否第一行是测试组数 T、每行几个数、分隔符是空格还是换行、变量类型与范围、输出格式。
   题面描述若与标程冲突，以标程为准（gen 的输出必须能被标程正确读入而不崩溃）。
2. 先 write_range：把描述整理成结构化 range.json（count 组数、constraints 各变量范围、edge_cases 边界类型列表）
3. 写出第一版 gen.cpp + validator.cpp；validator 的读取顺序必须和标程的输入读取完全一致（含开头的 T）
4. 初步自检：对 range.json 里每个 edge_type 各抽 1 个 seed 做 gen→validate→std
5. 编译失败或 validate/std 挂了 -> 根据 stderr 改 gen.cpp 或 validator.cpp，重新 write_gen/write_validate
6. 若需要 checker：答案唯一且只需比较输出 -> use_builtin_checker；
   答案不唯一或需额外判定 -> write_checker。不要两者都写。
7. 【必须】调用 run_self_check()：会额外跑 random 最小档与最大档（逼近规模上界）、以及多测相关边界；
   若 range.json 含 time_limit_ms / memory_limit_mb，标程超时/超内存按该限制检查。未通过不得 finish。
8. run_self_check 返回 OK -> 调 finish

【硬性 CLI 契约，必须遵守】
你只能创建/修改：range.json, gen.cpp, validator.cpp（及 checker）。不要写别的大文件，不要直接写测例数据。
修改 gen/validator 必须用 write_gen / write_validate（会自动编译）；不要用残缺摘要当 content。
需要看上一版源码时：read_file("gen.cpp") 或 read_file("validator.cpp")（路径相对工作目录）。
range.json 必须是合法 JSON，含：
  - count: 整数，默认 15（用户未特别要求时写 15）
  - constraints: 对象，各变量名 -> [min, max]
  - edge_cases: 数组，边界类型名；每个名字必须是你 gen --type 能接受的取值
  - 禁止在 edge_cases 里写 "random"：系统会给非边界组自动补 random（可写 random_tree / random_sparse 等具体名）
  - 建议写 time_limit_ms（毫秒）与 memory_limit_mb（MB）：系统会对 gen/validator/std 强制限时限内存；
    超限分别返回 TIMEOUT / MEMORY_LIMIT。题面未写内存时可省略（不限制）或写 256。
【规模均匀 — 很重要】
对 n、m、|s| 这类落在 [L,R] 的规模变量：15 组测例必须同时覆盖小数据与大数据，不能全挤在小数。
  - 系统跑 gen 时会传 --index i --count C（i=0..C-1）。random 分支请用它们分层取规模，例如：
      int idx = opt<int>("index", 0), cnt = opt<int>("count", 15);
      // 把 [L,R] 切成 cnt 段，第 idx 组落在第 idx 段内再 rnd
      long long span = (long long)R - L;
      long long lo = L + span * idx / cnt, hi = L + span * (idx + 1) / cnt;
      if (hi < lo) hi = lo; if (hi > R) hi = R; if (lo > R) lo = R;
      int n = rnd.next((int)lo, (int)hi);
  - 禁止写死 n = rnd.next(L, min(100, R)) 这类只抽小数的写法（自检可临时缩小，但最终交付必须覆盖到接近 R）
  - edge_cases 里仍要有明确边界：如 edge_n1 / edge_nmax（或题目对应的最小/最大）
【多测 T + sum 约束 — 更重要】
若题面有测试组数 T∈[1,Tmax]（如 1e4），且还有 sum n ≤ S（或等价总规模上限）：
  - 禁止「先抽很大的 n，再令 T = S/n」——这会把 T 几乎永远压成 1～2，违背 T 的上界覆盖。
  - 必须按 --index 分层覆盖两种极端（及中间）：
      * 大 T + 小 n：T 靠近 Tmax（或 S/n_min），每个 n_i 取很小（如 2～几十），保证 sum n ≤ S
      * 小 T + 大 n：T=1 或很小，单个 n 靠近 min(n_max, S)
      * 中等：T 与 n 都取中间档，仍满足 sum n ≤ S
  - 推荐：先按 index 决定本文件偏向「攻 T」还是「攻 n」，再在对应桶内用 pickSized；不要只对 n 分层而对 T 随便 rnd.next(1, S/n)。
  - edge_cases 建议含：edge_T1、edge_Tmax（或 max_tests）、以及大 n 单测。
gen.cpp 必须满足（testlib / generator 写法）：
  - #include "testlib.h" 或 #include "generator.h"（后者已含 testlib），main 里第一行 registerGen(argc, argv, 1)
  - 用 opt<int>("seed") 取种子，opt<string>("type","random") 取类型；并读取 opt<int>("index",0)/opt<int>("count",15) 做规模分层
  - --type 取值：random（默认分支）+ range.json edge_cases 里的每个名字
  - 用 rnd.next(l,r)/rnd.perm 或 generator::all 的树图几何 API 生成，保证可复现
  - 只向 stdout 打印测例（printf/cout），调试信息走 stderr（fprintf(stderr,...)）
  - 禁止 std::shuffle(..., rnd)；打乱用 for+swap+rnd.next(0,i)
  - 未声明的标识符不要用（不要写 clock()/clamp 等除非自己实现或正确头文件）
【性能硬约束 — 极重要】
gen 单次执行必须在 5 秒内输出完毕（含 n、m 取到上界 2e5/4e5 的边界组）。超时会被系统直接 kill，
并把 "gen TIMEOUT after 5s" 报回给你，你必须据此重写 gen.cpp。
为满足 5s 限制，必须遵守：
  - 严禁「枚举所有可能的对象再 shuffle/取前 k 个」式写法。例如：
      * 错误：vector<pair<int,int>> pool; for i for j pool.push_back({i,j});  // n*(n-1)/2 条边，n=2e5 时 ~2e10 条，必爆
      * 错误：把所有字符串/区间/数对都生成到一个 vector 里再随机
  - 需要「从 N 个候选中选 K 个不重复」时（N 可能很大，K≤几e5）：
      * 用 unordered_set<long long> 记录已选，循环随机采样 + 去重，直到选够 K 个；
      * 编码：key = (long long)a * N + b（a<b）；查询/插入均摊 O(1)；
      * 当 K 接近 N 时才退化，但题目里 K 一般远小于 N（如 m << n*(n-1)/2）。
  - 需要「随机生成一条链/树/图」时：优先用 generator.h 的 Tree/Chain/Flower/Graph 等 API（O(n) 或 O(n+m)）；
      或直接按拓扑序/父节点随机连边；不要预建边池。
  - 输出大文件时用 printf / 快速 cout（已开 ios::sync_with_stdio(false) 也行），不要用 endl 刷缓冲。
  - 内存：不要申请超过 ~几百 MB 的 vector；n=2e5 时 O(n) 或 O(n+m) 是安全的，O(n^2) 一定不安全。
  - 自检时如果某 edge_type 第一次 run_gen 就 TIMEOUT，立刻 read_file("gen.cpp") 找到对应分支，
    用随机采样或 generator API 替换枚举，重新 write_gen，再继续自检。不要靠「重试同一段代码」碰运气。
validator.cpp 必须满足（testlib 写法）：
  - #include "testlib.h"，main 里第一行 registerValidation()
  - 用 inf.readInt(l, r) / inf.readSpace() / inf.readEoln() / inf.readEof() 严格逐 token 读取
  - 读取顺序必须和题面输入格式完全一致（包括开头的 T，如果题目有多组数据）
  - 任何格式/范围不符 testlib 会自动 quit 并把原因打到 stderr
  - 必须验证题目声明的所有结构性质，不能只做格式和范围检查：
      * 数组题：检查长度、元素范围，以及题目要求的单调性、互异性、排序状态等
      * 树题：检查无自环、无重边、连通、无环、节点数与边数关系
      * 图题：检查是否满足题目声明的图性质（连通、DAG、二分图、无重边/自环等）
      * 字符串题：检查字符集、长度约束、子串/前缀/后缀关系等
      * 几何题：检查点数、坐标范围、凸性/简单多边形等题面要求的性质
      * 多测题：检查 T 范围、sum n / sum m 等总规模约束
  - 对题面中“保证”“约定”“满足...”等条件，必须用 ensuref 显式校验，不要默认数据一定满足
图题额外注意：
  - 默认按「无自环、无重边」处理：生成器用 v>u 或 u≠v 的池子，validator 用 ensuref(u!=v) 并检查重复边。
  - 如果题面明确允许自环，则生成器要允许 u==v，且 validator 不能拒绝自环。
  - 如果题面明确允许重边，则 validator 不要对 (u,v) 去重；但多数题目默认无重边，请按题面为准。
禁止访问网络；不要写大数据进仓库，只写生成器/校验器代码。

规则：
1. 完成任务必须调 finish，不要只输出文字就停下。
2. 调用工具时参数要完整、合法。
3. 看到工具返回 ERROR 要修正后再继续，不要无视。
"""


def run(task: str, max_steps: int = 30, verbose: bool = True, system_prompt: str = SYSTEM_PROMPT,
        on_event=None, tool_schemas=None) -> str:
    """跑一轮 Agent。

    task:        给 Agent 的任务描述
    max_steps:   最多循环多少轮，防止空转
    verbose:     是否打印每步动作
    system_prompt: 系统提示词，默认用数据生成的契约
    on_event:    可选回调 on_event(step, name, args, preview)，用于外部追踪进度
    tool_schemas: 可选，覆盖默认 TOOL_SCHEMAS（例如只允许 write_range）
    返回:        finish 的 summary，或 "预算用尽"
    """
    schemas = tool_schemas if tool_schemas is not None else tools.TOOL_SCHEMAS
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": task},
    ]

    # LLM 偶尔会只返回文本不调工具（如「解释一下思路」）。此时不直接 finish，
    # 而是发一条 nudge 提醒它调工具。连续 nudge 太多次仍不改，才放弃。
    max_nudges = 3
    nudge_count = 0

    for step in range(1, max_steps + 1):
        actions = llm.chat(messages, schemas)

        # LLM 没调任何工具：发 nudge 提醒它调工具，而不是直接 finish。
        # 这避免了「LLM 解释思路 → Agent 立刻终止 → 没产出 gen」的常见失败模式。
        if not actions:
            nudge_count += 1
            if nudge_count > max_nudges:
                if verbose:
                    print(f"[step {step}] LLM 连续 {max_nudges} 次不调工具，放弃")
                if on_event:
                    on_event(step, "give_up", {"reason": "LLM 连续多次不调工具"}, "")
                return "预算用尽（LLM 连续多次不调工具）"
            nudge_msg = (
                "你上一条回复没有调用任何工具。任务还没完成——"
                "请直接调用工具（write_range / write_gen / write_validate / run_self_check 等）来执行，"
                "不要只输出文字解释。如果是想看已有文件，调 read_file；"
                "如果任务已完成（且 run_self_check 已 OK），调 finish。"
            )
            messages.append({"role": "user", "content": nudge_msg})
            if verbose:
                print(f"[step {step}] nudge #{nudge_count}（LLM 没调工具）")
            if on_event:
                on_event(step, "nudge", {"count": nudge_count}, nudge_msg[:120])
            continue

        # LLM 调了工具，重置 nudge 计数
        nudge_count = 0

        # 如果一轮里出现 write_gen / write_validate / write_checker / use_builtin_checker，并行编译以节省时间
        results = [None] * len(actions)
        writer_indices = {}
        for idx, act in enumerate(actions):
            if act.name in ("write_gen", "write_validate", "write_checker", "use_builtin_checker"):
                writer_indices[act.name] = idx
        parallel_writers = [name for name in ("write_gen", "write_validate", "write_checker", "use_builtin_checker")
                            if name in writer_indices]
        if len(parallel_writers) > 1:
            if verbose:
                print(f"[step {step}] parallel compile {', '.join(parallel_writers)}")
            with concurrent.futures.ThreadPoolExecutor(max_workers=len(parallel_writers)) as executor:
                futures = {
                    name: executor.submit(
                        tools.dispatch, actions[writer_indices[name]].name, actions[writer_indices[name]].args
                    )
                    for name in parallel_writers
                }
                for name, fut in futures.items():
                    results[writer_indices[name]] = fut.result()

        for idx, act in enumerate(actions):
            if verbose:
                print(f"[step {step}] {act.name} {act.args}")

            if act.name == "finish":
                summary = act.args.get("summary", "done")
                if verbose:
                    print(f"[done] {summary}")
                return summary

            result = results[idx]
            if result is None:
                result = tools.dispatch(act.name, act.args)
                results[idx] = result
            # 大输出（尤其 run_gen 的大数据）立刻截断再进上下文，避免下一轮 413
            if act.name == "run_gen" and len(result) > 2500 and not result.startswith("ERROR"):
                result_for_llm = (
                    result[:800]
                    + f"\n...[generated {len(result)} chars, truncated for context]...\n"
                    + result[-400:]
                )
            elif act.name == "run_self_check" and len(result) > 4000:
                result_for_llm = result[:2500] + f"\n...[{len(result)} chars truncated]...\n" + result[-1000:]
            elif len(result) > 4000:
                result_for_llm = result[:2000] + f"\n...[{len(result)} chars truncated]...\n" + result[-800:]
            else:
                result_for_llm = result

            if verbose:
                preview = result if len(result) <= 120 else result[:120] + "..."
                print(f"        -> {preview}")
            if on_event:
                preview = result if len(result) <= 120 else result[:120] + "..."
                on_event(step, act.name, dict(act.args), preview)

            # 把工具结果作为 tool 消息塞回上下文
            # 必须带 tool_call_id，对应 assistant 的 tool_calls
            messages.append({
                "role": "tool",
                "tool_call_id": act.tool_call_id,
                "content": result_for_llm,
            })

    return "预算用尽"
