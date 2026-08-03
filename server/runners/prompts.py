"""Agent task 构建器。"""
import json
import re

from server.text_agent import simplify_text, beautify_text
from utils.markup import to_plain_for_llm


CHECKER_PLAN_TARGET_CHARS = 1300
CHECKER_PLAN_SOFT_MAX_CHARS = 2000


def _brief_text(text: str, head: int, tail: int, label: str) -> str:
    t = (text or "").strip()
    if not t:
        return ""
    if len(t) <= head + tail + 80:
        return t
    return t[:head] + f"\n\n...（{label}已截断）...\n\n" + t[-tail:]


def looks_like_unique_token_answer(
    stmt_plain: str = "",
    output_plain: str = "",
    std_for_prompt: str = "",
) -> bool:
    """启发式：唯一答案（按词/整数比对）→ 宜用内置 wcmp，少开自定义 SPJ。"""
    blob = "\n".join(
        [(stmt_plain or ""), (output_plain or ""), (std_for_prompt or "")[:2000]]
    )
    blob_l = blob.lower()
    if not blob.strip():
        return False
    # 多解/构造题：不自动 wcmp
    if any(
        h in blob
        for h in ("构造一组", "构造一个", "输出任意", "不唯一", "多解", "任意一组", "任意一个")
    ):
        return False
    if any(h in blob_l for h in ("special judge", "specialjudge")):
        # 题面显式 SPJ 语义时仍可能唯一；仅排除「构造/多解」已在上面
        pass
    unique_hints = (
        "输出一个整数", "输出一行一个整数", "答案唯一", "唯一答案",
        "最小总代价", "最小值", "最大值", "最优解对应唯一",
    )
    out_hints = ("一个整数", "一行一个整数", "单个整数", "输出整数")
    has_unique = any(h in blob for h in unique_hints) or any(
        h in (output_plain or "") for h in out_hints
    )
    if not has_unique:
        # 标程几乎只打一个数也算弱信号
        std = std_for_prompt or ""
        cout_n = len(re.findall(r"\bcout\s*<<", std))
        printf_n = len(re.findall(r"\bprintf\s*\(", std))
        if cout_n + printf_n == 0 or cout_n + printf_n > 4:
            return False
        # 仍需题面有「最小/最大/最优」之一，避免误伤
        if not any(h in blob for h in ("最小", "最大", "最优", "唯一")):
            return False
    return True


def parse_builtin_checker_from_plan(plan_text: str) -> str | None:
    """从 checker_plan 解析「应使用内置 checker: wcmp」等。"""
    text = plan_text or ""
    m = re.search(
        r"应使用内置\s*checker\s*[:：]\s*(lcmp|wcmp|rcmp4|rcmp6|rcmp9|yesno)",
        text,
        re.I,
    )
    if m:
        return m.group(1).lower()
    m2 = re.search(
        r"使用内置\s*[:：]?\s*(lcmp|wcmp|rcmp4|rcmp6|rcmp9|yesno)",
        text,
        re.I,
    )
    if m2:
        return m2.group(1).lower()
    for name in ("lcmp", "wcmp", "rcmp4", "rcmp6", "rcmp9", "yesno"):
        if re.search(rf"(?i)(?:模板|选型|builtin).{{0,40}}\b{name}\b", text):
            if re.search(r"无需自定义|使用内置|内置 checker|应使用内置", text, re.I):
                return name
    return None


def build_checker_planner_user(
    stmt_plain: str,
    range_plain: str,
    output_plain: str,
    std_for_prompt: str,
    range_json: dict,
) -> str:
    """构造 Checker Planner 的 user prompt：题面为主，标程仅 I/O。"""
    parts = [
        "请为下面的题目写一份可执行的 checker 判定计划"
        f"（目标约 {CHECKER_PLAN_TARGET_CHARS} 字，勿超过 {CHECKER_PLAN_SOFT_MAX_CHARS} 字）。\n"
        "【分工】Plan 写清实现思路与判定规格；Coder 只翻译成代码。\n"
        "【资料优先级】题面 > 输出描述 > 范围/I/O > 标程。"
        "判定语义与状态转移必须来自题面；标程只用于核对读写格式，禁止把标程算法写进第 6 节"
        "（多项式「验证是否合法」步骤除外）。\n"
        "【唯一答案】若每组只需比对少数整数/词且答案唯一：第 1 节写"
        "「应使用内置 checker: wcmp」（或 lcmp），第 2 节写同名，并写明无需自定义；"
        "禁止为唯一最优值重写 Dijkstra/DP。\n"
        "【SPJ】只校验答案合法性，不校验输出格式；勿把空白/换行/_pe 当核心条件。\n"
        "【复杂度硬规范】设计 ≤1s / 运行超时 2s；首选 O(N)~O(N log N)；"
        "禁止指数 MITM、N≥5000 的 O(N^2)、满数据不可行子集和；"
        "第 6 节必须写死：复杂度预算：O(...) · N=… · 预计≤1s。\n"
        "请把模板、合法条件、与 ans 关系、第 6 节实现步骤写死。\n",
        f"\n【题面 · 判定语义主源】\n{(stmt_plain or '').strip()}",
    ]
    if (output_plain or "").strip():
        parts.append(f"\n【输出描述 / 判定规则 · 次主源】\n{output_plain.strip()}")
    parts.append(f"\n【数据范围】\n{(range_plain or '').strip()}")
    parts.append(
        f"\n【range.json】\n```json\n{json.dumps(range_json, ensure_ascii=False, indent=2)}\n```"
    )
    if (std_for_prompt or "").strip():
        std_brief = _brief_text(std_for_prompt, 300, 200, "标程")
        parts.append(
            "\n【标程 · 仅 I/O 参考】只看 cin/cout（或读写）与每组输出形态；"
            "忽略内部求最优/DP/公式，不得写入 plan 第 6 节。\n"
            f"```\n{std_brief}\n```"
        )
    parts.append(
        "\n要求：含 7 个小节；第 2 节写死模板名（唯一答案优先 wcmp）；"
        "第 4 节合法条件须来自题面（不要写格式条件）；"
        "第 6 节实现思路须为可落地的编号步骤且含「复杂度预算：O(...) · N=… · 预计≤1s」"
        "（禁止空话、禁止指数 MITM、禁止 N≥5000 的 O(N^2)、禁止抄求最优；多项式验证可写）；"
        "有保底/激活/门槛时第 6 节必须写清 if/else，禁止无条件 clamp 及未证明的「等价」；"
        "只比最优值且必须自定义时才写 maxVal=simulate；唯一答案用内置即可；"
        "第 7 节覆盖：标程正例 +（多解则）非标程正例特征或写明唯一 + 语义负例（_wa，勿主打 _pe）；"
        "构造/多解禁止与 ans 字符串全等；只输出 Markdown 计划。"
    )
    return "\n".join(parts)


def build_checker_coder_task(
    stmt_plain: str,
    range_plain: str,
    output_plain: str,
    std_for_prompt: str,
    range_json: dict,
    failure_context: str = "",
) -> str:
    """构造 Checker Coder task：plan 为主，题面语义兜底，标程极弱。"""
    stmt_brief = _brief_text(stmt_plain, 800, 400, "题面")
    range_brief = _brief_text(range_plain, 300, 150, "范围")
    std_brief = _brief_text(std_for_prompt, 200, 150, "标程")
    out_brief = (output_plain or "").strip()
    if len(out_brief) > 600:
        out_brief = out_brief[:600] + "\n...（输出描述已截断）..."

    parts = [
        "请把 checker_plan.md 逐条翻译成完整的 checker.cpp。\n"
        "【分工】你只负责实现；禁止改判定类型/模板/合法条件/实现思路。\n"
        "【资料优先级】checker_plan.md > 题面（状态转移不清时语义兜底）"
        " > 输出描述（字段含义）> 标程（仅 ans/ouf 读写，不参与判定逻辑）。\n"
        "【约束】只写 checker.cpp；不要改 gen/validator/range；write_checker 必须完整源码。\n"
        "【SPJ】只验答案合法性（_ok/_wa），不验输出格式；读完所需字段后 "
        "while (!ouf.seekEof()) ouf.readToken(); 再 quit，避免 dirt 假 PE。\n"
        "【复杂度】严格按 plan 第 6 节预算实现（目标 ≤1s，超时 2s）；"
        "禁止改用 MITM/指数/大 N 的 N^2。\n"
        "【API】只用 testlib 真实接口（readInt/readToken/readLine/quitf 等）；"
        "禁止 isNumber 等幻觉函数；readEoln/readEof/readSpace 返回 void，"
        "禁止把 readEoln 当 bool；探测用 seekEoln/seekEof；"
        "带空格整句用 readLine/readString，禁止 readToken 读整句或 readToken(\"s\")。\n",
    ]
    if stmt_brief:
        parts.append(
            f"\n【题面 · 语义兜底】plan 对保底/激活/门槛写不清或与题面冲突时以此为准"
            f"（勿用标程算法覆盖）。\n{stmt_brief}"
        )
    if out_brief:
        parts.append(f"\n【输出描述 · 字段含义】\n{out_brief}")
    if range_brief:
        parts.append(f"\n【数据范围摘要】\n{range_brief}")
    parts.append(
        f"\n【range.json】\n```json\n{json.dumps(range_json, ensure_ascii=False, indent=2)}\n```"
    )
    if std_brief:
        parts.append(
            "\n【标程 · 极弱参考】仅核对 ans/ouf 每组输出几个数；禁止照抄内部算法。\n"
            f"```\n{std_brief}\n```"
        )
    if failure_context:
        parts.append(f"\n{failure_context[:1000]}\n")
    parts.append(
        "\n要求：\n"
        "1. 先 read_file('checker_plan.md') 一次；状态转移不清时可再读 "
        "statement.txt / statement_simplified.txt。\n"
        "2. 按 plan 第 2 节装模板，再严格按第 6 节「实现思路」步骤 write_checker"
        "（每步最多一次；最多 2 次编译成功；编译失败不计次）。\n"
        "3. 【规格优先级】plan 第 4/5/6/7 节为主；保底/激活等与题面冲突时服从题面；"
        "禁止用标程覆盖判定逻辑。\n"
        "4. 题意模拟按 plan/题面分支实现；禁止无条件 "
        "`if (cur < k) cur = k`；最优值题必须先 simulate(ans) 再验 ouf。\n"
        "5. 写完并编译成功后系统自动 run_checker_self_check()：正例须 _ok，负例须 _wa；"
        "禁止未自检连写；禁止严格格式/_pe。\n"
        "6. 自检 OK 后 finish；FAIL [LOGIC] 才允许第二轮成功 write_checker"
        "（优先按题面修模拟语义，禁止改成与 ans 全等）。"
    )
    return "\n".join(parts)


def build_checker_task(
    stmt_plain: str,
    range_plain: str,
    output_plain: str,
    std_for_prompt: str,
    range_json: dict,
    failure_context: str = "",
) -> str:
    """兼容旧名：等同 Checker Coder task。"""
    return build_checker_coder_task(
        stmt_plain, range_plain, output_plain, std_for_prompt, range_json,
        failure_context=failure_context,
    )


def build_reviewer_task(
    stmt_plain: str,
    range_plain: str,
    range_json: dict,
    gen_agent_summary: str,
) -> str:
    """为 Reviewer Agent 构造 task（精简版）。"""
    stmt_brief = (stmt_plain or "").strip()
    if len(stmt_brief) > 1200:
        stmt_brief = stmt_brief[:600] + "\n\n...（题面已截断）...\n\n" + stmt_brief[-400:]
    range_brief = (range_plain or "").strip()
    if len(range_brief) > 800:
        range_brief = range_brief[:400] + "\n...（范围已截断）...\n" + range_brief[-200:]
    parts = [
        "【角色】Reviewer\n"
        "【目标】审查当前工作目录的 gen.cpp，输出结构化审查报告。\n"
        "【约束】只读、只测、不写文件；禁止调用 write_gen / write_validate / write_checker / write_range / write_file。",
        f"\n【题面摘要】\n{stmt_brief}",
        f"\n【数据范围摘要】\n{range_brief}",
        f"\n【range.json】\n```json\n{json.dumps(range_json, ensure_ascii=False, indent=2)}```",
        f"\n【Gen Agent 执行摘要】\n{gen_agent_summary[:500]}",
        "\n工作目录已有 gen.cpp / validator.cpp / range.json / 标程。\n"
        "请用 read_file 和 run_gen / run_validate / run_std 进行审查，"
        "最后调 finish(summary)，summary 必须是结构化审查报告（MUST_FIX / SHOULD_FIX / OK）。",
    ]
    return "\n".join(parts)


def build_fixer_task(
    stmt_plain: str,
    range_plain: str,
    range_json: dict,
    review_report: str,
    gen_agent_summary: str,
) -> str:
    """为 3.5 阶段 Fixer Agent 构造 task（精简版）。"""
    stmt_brief = (stmt_plain or "").strip()
    if len(stmt_brief) > 1200:
        stmt_brief = stmt_brief[:600] + "\n\n...（题面已截断）...\n\n" + stmt_brief[-400:]
    range_brief = (range_plain or "").strip()
    if len(range_brief) > 800:
        range_brief = range_brief[:400] + "\n...（范围已截断）...\n" + range_brief[-200:]

    # 只把 MUST_FIX 和 SHOULD_FIX 相关行带进来，避免报告太长
    report_lines = (review_report or "").splitlines()
    focused = [ln for ln in report_lines if any(k in ln for k in ("MUST_FIX", "MUST FIX", "SHOULD_FIX", "SHOULD FIX", "必须修", "建议修", "严重", "超时", "性能问题"))]
    focused_text = "\n".join(focused[:15]) if focused else (review_report or "")[:1000]

    parts = [
        "【角色】Fixer\n"
        "【目标】根据 Reviewer 报告修复当前工作目录的 gen.cpp。\n"
        "【约束】只写 gen.cpp；不要改 range.json / validator.cpp / checker.cpp；write_gen 必须完整源码。",
        f"\n【题面摘要】\n{stmt_brief}",
        f"\n【数据范围摘要】\n{range_brief}",
        f"\n【range.json】\n```json\n{json.dumps(range_json, ensure_ascii=False, indent=2)}```",
        f"\n【Gen Agent 执行摘要】\n{gen_agent_summary[:500]}",
        f"\n【Reviewer 报告（聚焦 MUST_FIX/SHOULD_FIX）】\n```\n{focused_text}\n```",
        "\n工作目录已有 gen.cpp / validator.cpp / review_report.txt。\n"
        "请优先 read_file(\"review_report.txt\") 和 read_file(\"gen.cpp\")，"
        "按 MUST_FIX 问题修复，用 write_gen 写完整源码并编译，"
        "最后调 run_self_check() 自检，通过后 finish。",
    ]
    return "\n".join(parts)


def build_gen_fixer_task(
    stmt_plain: str,
    range_plain: str,
    range_json: dict,
    self_check_result: str,
    attempt: int,
    max_attempts: int,
) -> str:
    """为 Gen Agent 自检失败后的 Fixer Agent 构造 task（精简版）。"""
    # 题面可能很长，Fixer 阶段只保留关键信息摘要
    stmt_brief = (stmt_plain or "").strip()
    if len(stmt_brief) > 1200:
        stmt_brief = stmt_brief[:600] + "\n\n...（题面已截断，完整版见 gen_plan.md 或工作目录 statement_simplified.txt）...\n\n" + stmt_brief[-400:]

    range_brief = (range_plain or "").strip()
    if len(range_brief) > 800:
        range_brief = range_brief[:400] + "\n...（范围已截断）...\n" + range_brief[-200:]

    # 自检失败日志结构化：只保留前 N 个 FAIL 块
    error_log = _extract_failure_blocks(self_check_result, max_blocks=6, max_chars=1200)

    parts = [
        "【角色】Gen Fixer\n"
        "【目标】当前工作目录已有 gen.cpp / validator.cpp，但强化自检未通过。"
        "请根据失败日志修复，使 run_self_check() 返回 OK。\n"
        "【约束】只修改 gen.cpp / validator.cpp；禁止修改 gen_special.cpp、range.json、checker.cpp、标程；"
        "write_gen / write_validate 必须传【完整 content】（从 #include 到 main 结尾 }）；"
        "禁止空调用、半截、__OMITTED_SOURCE__；宜短而全，避免 JSON 截断。"
        "修复时优先遵守 gen_plan.md 策略，题面摘要仅作冲突对照。"
        "特殊样例由后续 SpecialCoder 处理，本阶段忽略 special_samples。",
        f"\n【题面摘要】\n{stmt_brief}",
        f"\n【数据范围摘要】\n{range_brief}",
        f"\n【range.json】\n```json\n{json.dumps(range_json, ensure_ascii=False, indent=2)}```",
        f"\n【自检失败摘要】\n```\n{error_log}\n```",
        "\n【动作】\n"
        "1. 先 read_file(\"gen_plan.md\") 对齐策略，再 read_file(\"gen.cpp\") / \"validator.cpp\"。\n"
        "2. 根据失败类型判断根因：gen TIMEOUT/MEMORY → 优化算法；"
        "validate FAILED → 先看 stderr：Unexpected white-space 修 validator 补 readSpace；"
        "其余优先修 gen；std FAILED → 对齐格式/降低规模。\n"
        "   unused key seed/type/index/count → 在 type 分支前补齐全部 opt<>()。\n"
        "   编译 no match for operator== / opt<int>(\"type\") / if (type == 0) → "
        "改成 string type = opt<string>(\"type\",\"random\")，并用字符串比较分支。\n"
        "   Expected EOF → 先看 gen 是否只打了合法输入：是则 validator 补 readEoln 再 readEof；"
        "若多打了答案/排列则修 gen。\n"
        "   Unexpected white-space - token expected → validator 同行连续 readInt/readLong "
        "缺 readSpace（或改用 readInts）；优先修 validator，勿删 gen 空格。\n"
        "   Expected integer, but \"...\" 或读到答案文案/排列 → 优先怀疑 gen 打成了答案，"
        "按 plan 第 1 节只打印输入，勿放宽 validator。\n"
        "3. 用 write_gen / write_validate 写完整修复后源码，编译失败时继续修正；"
        "不得推翻 plan 的 edge_cases/API 选型"
        "（但若 plan 第 5/8 节误把答案当 gen 输出，以第 1 节输入格式为准修正 gen）。"
        "TIMEOUT 时按 gen_plan 有效状态预算降密度（满规模≠满状态）。\n"
        "4. 调用 run_self_check() 验证；通过后调 finish(summary) 说明改动点与根因。\n"
        f"这是第 {attempt}/{max_attempts} 轮自动修复；若本轮仍失败，将回退基线并中止本阶段。",
    ]
    return "\n".join(parts)


def build_coder_rewrite_task(
    stmt_plain: str,
    range_plain: str,
    range_json: dict,
    self_check_result: str,
    gen_agent_summary: str,
) -> str:
    """为 Coder 骨架重写构造 task（精简版）。"""
    stmt_brief = (stmt_plain or "").strip()
    if len(stmt_brief) > 1200:
        stmt_brief = stmt_brief[:600] + "\n\n...（题面已截断，完整版见 gen_plan.md 或工作目录 statement_simplified.txt）...\n\n" + stmt_brief[-400:]

    range_brief = (range_plain or "").strip()
    if len(range_brief) > 800:
        range_brief = range_brief[:400] + "\n...（范围已截断）...\n" + range_brief[-200:]

    error_log = _extract_failure_blocks(self_check_result, max_blocks=8, max_chars=1500)

    parts = [
        "【角色】Coder Rewrite\n"
        "【目标】当前 gen.cpp / validator.cpp 骨架存在结构性问题，Fixer 无法收敛，需按 gen_plan.md 重新写出完整新版。\n"
        "【约束】按 gen_plan.md 重新实现骨架，不要局部补丁、不要另起一套策略；"
        "write_gen / write_validate 必须传【完整 content】，宜短而全，禁止截断/空调用/__OMITTED_SOURCE__；"
        "禁止修改 range.json / 标程 / gen_special.cpp。"
        "特殊样例由后续 SpecialCoder 处理。",
        f"\n【题面摘要】\n{stmt_brief}",
        f"\n【数据范围摘要】\n{range_brief}",
        f"\n【range.json】\n```json\n{json.dumps(range_json, ensure_ascii=False, indent=2)}```",
        f"\n【前序 Coder/Fixer 摘要】\n{gen_agent_summary[:500]}",
        f"\n【自检失败摘要（结构性信号）】\n```\n{error_log}\n```",
        "\n【动作】先 read_file(\"gen_plan.md\")，再按 plan 第 5/6/7/8 节整份重写。\n"
        "若 plan 第 5/8 节与第 1 节/标程读入矛盾（gen 打答案），以第 1 节输入格式为准。\n"
        "\n常见需重写信号：\n"
        "- 大量 edge_cases 缺分支或大规模 FAIL；\n"
        "- gen TIMEOUT / MEMORY（O(n^2) 枚举或预建大池子）；\n"
        "- 输入格式与标程读入顺序不匹配；或 validate 像 gen 打成了答案；\n"
        "- 连续多轮 Fixer 无法收敛的同类错误。\n"
        "\n动作：\n"
        "1. 先 read_file(\"gen_plan.md\") 一次（range.json 已在 task 中，不必再读）。\n"
        "2. 再 read_file 当前 gen.cpp / validator.cpp 了解失败实现。\n"
        "3. 按 plan 重写完整 gen.cpp / validator.cpp，可一次 write_gen + write_validate 同时写；"
        "遵守有效状态预算（满规模≠满状态）。\n"
        "4. 立即调用 run_self_check()；通过则 finish。外层会对 TIMEOUT 再强制完整自检，tiny/fast 通过不算交付。\n"
        "这是骨架重写；请按 FAIL 行（尤其 TIMEOUT 的 type/index）写对。",
    ]
    return "\n".join(parts)


def _extract_failure_blocks(text: str, max_blocks: int = 6, max_chars: int = 1200) -> str:
    """从自检/批量失败日志中提取前 N 个 FAIL/ERROR 块，用于 Fixer 锚定。"""
    if not text:
        return ""
    lines = text.splitlines()
    blocks = []
    current = []
    for line in lines:
        if any(k in line for k in ("FAIL", "ERROR", "MUST_FIX", "GIVE UP", "TIMEOUT", "MEMORY")):
            if current:
                blocks.append("\n".join(current))
            current = [line]
        elif current:
            current.append(line)
    if current:
        blocks.append("\n".join(current))
    # 如果没找到 FAIL，则保留头尾
    if not blocks:
        return text[:max_chars] + (f"\n...[{len(text)} chars truncated]..." if len(text) > max_chars else "")
    chosen = blocks[:max_blocks]
    result = "\n\n".join(chosen)
    if len(result) > max_chars:
        result = result[:max_chars] + f"\n...[{len(result)} chars truncated]..."
    return result


def build_batch_fixer_task(
    stmt_plain: str,
    range_plain: str,
    range_json: dict,
    failures: list[dict],
    attempt: int,
    max_attempts: int,
    special_only: bool = False,
) -> str:
    """为批量生成失败后的修复 Agent 构造 task（精简版）。"""
    stmt_brief = (stmt_plain or "").strip()
    if len(stmt_brief) > 1200:
        stmt_brief = stmt_brief[:600] + "\n\n...（题面已截断）...\n\n" + stmt_brief[-400:]

    range_brief = (range_plain or "").strip()
    if len(range_brief) > 800:
        range_brief = range_brief[:400] + "\n...（范围已截断）...\n" + range_brief[-200:]

    # 按类型聚合，只保留 Top 5 样例
    by_type: dict[str, list[dict]] = {}
    for f in failures:
        by_type.setdefault(f.get("planned_type", "unknown"), []).append(f)

    summary_lines = []
    for typ, fs in sorted(by_type.items()):
        summary_lines.append(f"- type={typ}: {len(fs)} 组失败")
        for f in fs[:2]:  # 每类型只展示 2 个代表
            err = (f.get("error") or "").replace("\n", " ")
            summary_lines.append(f"  * #{f.get('index')}: {err[:120]}")

    sample_blocks = []
    for f in failures[:5]:
        preview = f.get("input_preview") or ""
        if preview:
            sample_blocks.append(
                f"# 组号 {f.get('index')} (type={f.get('planned_type')})\n"
                f"```\n{preview[:300]}\n```"
            )
    sample_block = "\n\n".join(sample_blocks)

    if special_only:
        parts = [
            "【角色】Special Batch Fixer\n"
            "【目标】修复 gen_special.cpp / check_special.cpp，使特殊样例批量生成不再失败。\n"
            "【约束】只 write_special_gen / write_special_check；禁止改 gen.cpp / validator.cpp / range.json。",
            f"\n这是第 {attempt}/{max_attempts} 轮自动修复；若本轮仍失败，将保留成功测例并中止任务。",
            f"\n【题面摘要】\n{stmt_brief}",
            f"\n【数据范围摘要】\n{range_brief}",
            f"\n【range.json】\n```json\n{json.dumps(range_json, ensure_ascii=False, indent=2)}```",
            f"\n【失败统计】\n共 {len(failures)} 组失败\n" + "\n".join(summary_lines),
        ]
        if sample_block:
            parts.append(
                "\n【失败样例输入预览】\n"
                f"{sample_block}\n"
            )
        parts.append(
            "\n要求：\n"
            "1. read_file('gen_special.cpp')、check_special.cpp 与 gen.cpp，对齐格式。\n"
            "2. 按 special_schemes 的 construct_mode 修复："
            "mutate=底稿+局部 patch；build=从零构造；保证 must_hold；不要改 validator。\n"
            "3. property_check FAILED 时优先加强构造；断言写错才改 check_special。\n"
            "4. write_special_gen / write_special_check 写完整源码（保留其他方案分支）。\n"
            "5. run_self_check() 通过后 finish。"
        )
        return "\n".join(parts)

    parts = [
        "【角色】Batch Fixer\n"
        "【目标】修复 gen.cpp / validator.cpp，使批量生成阶段不再失败。\n"
        "【约束】只修改 gen.cpp / validator.cpp；禁止改 gen_special.cpp / range.json；write_* 必须完整源码。",
        f"\n这是第 {attempt}/{max_attempts} 轮自动修复；若本轮仍失败，将保留成功测例并中止任务。",
        f"\n【题面摘要】\n{stmt_brief}",
        f"\n【数据范围摘要】\n{range_brief}",
        f"\n【range.json】\n```json\n{json.dumps(range_json, ensure_ascii=False, indent=2)}```",
        f"\n【失败统计】\n共 {len(failures)} 组失败\n" + "\n".join(summary_lines),
    ]
    if sample_block:
        parts.append(
            "\n【失败样例输入预览（供定位根因）】\n"
            f"{sample_block}\n"
            "每段预览已截断到 300 字符；完整数据范围以 range.json 为准。"
        )
    parts.append(
        "\n要求：\n"
        "1. 先 read_file('gen.cpp') 和 read_file('validator.cpp')，确认当前实现。\n"
        "2. 根据失败类型判断根因：\n"
        "   - validate FAILED：优先修 gen，必要时再调整 validator。\n"
        "   - gen FAILED / TIMEOUT / MEMORY：修 gen。\n"
        "   - std FAILED / TIMEOUT / MEMORY：修 gen 降低规模 / 对齐格式。\n"
        "3. 用 write_gen 和 / 或 write_validate 写完整源码。\n"
        "4. 禁止修改 range.json；不要改 count、edge_cases、constraints。\n"
        "5. 改完后必须调用 run_self_check() 做强化自检，确保返回 OK。\n"
        "6. 自检通过后调 finish，说明改动点与根因。"
    )
    return "\n".join(parts)


def prepare_plain_texts(problem_statement: str, data_range_desc: str) -> tuple[str, str, str, str, str]:
    """返回 (stmt_plain, range_plain, output_plain, raw_stmt_for_prompt, raw_range_for_prompt)。"""
    stmt_plain = problem_statement
    try:
        stmt_plain = simplify_text(problem_statement or "", kind="statement")
    except Exception:
        stmt_plain = to_plain_for_llm(problem_statement or "")

    range_plain = to_plain_for_llm(data_range_desc or "")
    output_plain = to_plain_for_llm("")  # 占位；由调用方填充

    raw_stmt = problem_statement or ""
    raw_range = data_range_desc or ""
    if len(raw_stmt) > 6000:
        raw_stmt = raw_stmt[:3000] + "\n\n...（原始题面过长，中间省略）...\n\n" + raw_stmt[-2000:]
    if len(raw_range) > 3000:
        raw_range = raw_range[:1500] + "\n\n...（原始范围描述过长，中间省略）...\n\n" + raw_range[-1000:]
    return stmt_plain, range_plain, output_plain, raw_stmt, raw_range


def build_std_block(std_code: str, lang: str) -> str:
    """构造喂给 Agent 的标程片段 block。"""
    std_for_prompt = std_code
    if len(std_for_prompt) > 12000:
        std_for_prompt = (
            std_code[:6000]
            + f"\n\n/* ... std 共 {len(std_code)} 字符，中间已省略；完整标程已编译，可用 run_std 实测 ... */\n\n"
            + std_code[-4000:]
        )
    return (
        f"\n\n【标程源码（lang={lang}）】\n```\n{std_for_prompt}\n```\n"
        "请先读标程，确认【读入格式】（是否首行 T、每行字段、分隔符、范围），"
        "gen 的输出必须能被该标程正确读入。"
        "标程用于确认读入格式与复杂度瓶颈；不要把标程的 cout/答案构造逻辑写进 gen。"
    )
