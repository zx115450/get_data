"""Agent task 构建器。"""
import json

from server.text_agent import simplify_text, beautify_text
from utils.markup import to_plain_for_llm


def build_checker_task(
    stmt_plain: str,
    range_plain: str,
    output_plain: str,
    std_for_prompt: str,
    range_json: dict,
    failure_context: str = "",
) -> str:
    """为自定义 checker 构造 Agent 的 task（精简版）。"""
    stmt_brief = (stmt_plain or "").strip()
    if len(stmt_brief) > 1200:
        stmt_brief = stmt_brief[:600] + "\n\n...（题面已截断）...\n\n" + stmt_brief[-400:]
    range_brief = (range_plain or "").strip()
    if len(range_brief) > 800:
        range_brief = range_brief[:400] + "\n...（范围已截断）...\n" + range_brief[-200:]

    parts = [
        "【角色】Checker Agent\n"
        "【目标】为本题写一个 checker.cpp（Special Judge）。\n"
        "【约束】只写 checker.cpp；不要改 gen.cpp / validator.cpp / range.json；write_checker 必须完整源码。",
        f"\n【题面摘要】\n{stmt_brief}",
        f"\n【数据范围摘要】\n{range_brief}",
    ]
    if output_plain.strip():
        parts.append(f"\n【输出描述 / 判定规则】\n{output_plain}")
    if std_for_prompt.strip():
        # 标程只保留输入读取相关头尾，checker 阶段不需要完整源码
        std_brief = std_for_prompt
        if len(std_brief) > 1500:
            std_brief = std_brief[:600] + "\n\n...（标程已截断，可用 read_file 查看完整）...\n\n" + std_brief[-300:]
        parts.append(f"\n【标程源码片段（供理解判定规则）】\n{std_brief}")
    parts.append(
        "\n【已有产物】\n"
        f"- range.json: {json.dumps(range_json, ensure_ascii=False, indent=2)}\n"
        "- 工作目录已有 gen.cpp / validator.cpp / 标程，可用 read_file 查看。\n"
    )
    if failure_context:
        parts.append(f"\n{failure_context[:1000]}\n")
    parts.append(
        "\n要求：\n"
        '1. 优先用 use_checker_template("construct_verify") 安装骨架，再 read_file("checker.cpp") 查看 TODO 位置。\n'
        '2. 用 write_checker 写完整 checker.cpp，必须 #include "testlib.h" 并调用 registerTestlibCmd(argc, argv)。\n'
        "3. 按 (inf, ouf, ans) 顺序读取文件并判定；多解时检查选手输出的合法性，不要直接字符串全等。\n"
        "4. 编译成功后必须调用 run_checker_self_check()：正例（标程输出）必须 _ok，负例（扰动输出）必须 _wa/_pe。\n"
        "5. run_checker_self_check() 返回 OK 后调 finish。"
    )
    return "\n".join(parts)


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
        "修复时仍须对照题面+标程+range 上下文。"
        "特殊样例由后续 SpecialCoder 处理，本阶段忽略 special_samples。",
        f"\n【题面摘要】\n{stmt_brief}",
        f"\n【数据范围摘要】\n{range_brief}",
        f"\n【range.json】\n```json\n{json.dumps(range_json, ensure_ascii=False, indent=2)}```",
        f"\n【自检失败摘要】\n```\n{error_log}\n```",
        "\n【动作】\n"
        "1. 先 read_file(\"gen.cpp\") 和 read_file(\"validator.cpp\") 查看当前源码。\n"
        "2. 根据失败类型判断根因：gen TIMEOUT/MEMORY → 优化算法；validate FAILED → 优先修 gen；"
        "std FAILED → 对齐格式/降低规模。\n"
        "3. 用 write_gen / write_validate 写完整修复后源码，编译失败时继续修正。"
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
        "【约束】按 plan 重新设计骨架，不要局部补丁；"
        "write_gen / write_validate 必须传【完整 content】，宜短而全，禁止截断/空调用/__OMITTED_SOURCE__；"
        "重写须带上题面+标程+range 全部上下文；禁止修改 range.json / 标程 / gen_special.cpp。"
        "特殊样例由后续 SpecialCoder 处理。",
        f"\n【题面摘要】\n{stmt_brief}",
        f"\n【数据范围摘要】\n{range_brief}",
        f"\n【range.json】\n```json\n{json.dumps(range_json, ensure_ascii=False, indent=2)}```",
        f"\n【前序 Coder/Fixer 摘要】\n{gen_agent_summary[:500]}",
        f"\n【自检失败摘要（结构性信号）】\n```\n{error_log}\n```",
        "\n常见需重写信号：\n"
        "- 大量 edge_cases 缺分支或大规模 FAIL；\n"
        "- gen TIMEOUT / MEMORY（O(n^2) 枚举或预建大池子）；\n"
        "- 输入格式与标程读入顺序不匹配；\n"
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
            "【目标】修复 gen_special.cpp，使特殊样例批量生成不再失败。\n"
            "【约束】只 write_special_gen；禁止改 gen.cpp / validator.cpp / range.json。",
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
            "1. read_file('gen_special.cpp') 与 read_file('gen.cpp')，对齐格式。\n"
            "2. 按 special_schemes 各方案的 construct_mode 修复："
            "mutate=底稿+局部 patch；build=从零构造；保证 must_hold；不要改 validator。\n"
            "3. write_special_gen 写完整源码（保留其他方案分支）。\n"
            "4. run_self_check() 通过后 finish。"
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
        "请先读标程，确认输入格式（是否首行 T、每行字段、分隔符、范围），"
        "gen 的输出必须能被该标程正确读入。"
    )
