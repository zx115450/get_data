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
    """为自定义 checker 构造第二轮 Agent 的 task。"""
    parts = [
        "请为本题写一个 checker.cpp（Special Judge）。",
        f"\n【题面】\n{stmt_plain}",
        f"\n【数据范围】\n{range_plain}",
    ]
    if output_plain.strip():
        parts.append(f"\n【输出描述 / 判定规则】\n{output_plain}")
    if std_for_prompt.strip():
        parts.append(f"\n【标程源码片段】\n{std_for_prompt}")
    parts.append(
        "\n【已有产物】\n"
        f"- range.json: {json.dumps(range_json, ensure_ascii=False, indent=2)}\n"
        "- 工作目录已有 gen.cpp / validator.cpp / 标程，可用 read_file 查看。\n"
    )
    if failure_context:
        parts.append(f"\n{failure_context}\n")
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
    """为 Reviewer Agent 构造 task。"""
    parts = [
        "请审查当前工作目录的 gen.cpp。",
        f"\n【题面】\n{stmt_plain}",
        f"\n【数据范围】\n{range_plain}",
        f"\n【range.json】\n{json.dumps(range_json, ensure_ascii=False, indent=2)}",
        f"\n【Gen Agent 执行摘要】\n{gen_agent_summary}",
        "\n工作目录已有 gen.cpp / validator.cpp / range.json / 标程。"
        "请用 read_file 和 run_gen / run_validate / run_std 进行审查，"
        "最后调 finish(summary)，summary 必须是结构化审查报告。",
    ]
    return "\n".join(parts)


def build_fixer_task(
    stmt_plain: str,
    range_plain: str,
    range_json: dict,
    review_report: str,
    gen_agent_summary: str,
) -> str:
    """为 Fixer Agent 构造 task。"""
    parts = [
        "请根据 Reviewer 报告修复当前工作目录的 gen.cpp。",
        f"\n【题面】\n{stmt_plain}",
        f"\n【数据范围】\n{range_plain}",
        f"\n【range.json】\n{json.dumps(range_json, ensure_ascii=False, indent=2)}",
        f"\n【Gen Agent 执行摘要】\n{gen_agent_summary}",
        f"\n【Reviewer 报告】\n{review_report}",
        "\n工作目录已有 gen.cpp / validator.cpp / review_report.txt。"
        "请优先 read_file(\"review_report.txt\") 和 read_file(\"gen.cpp\")，"
        "按 MUST_FIX 问题修复，用 write_gen 写完整源码并编译，"
        "最后调 run_self_check() 自检，通过后 finish。",
    ]
    return "\n".join(parts)


def build_batch_fixer_task(
    stmt_plain: str,
    range_plain: str,
    range_json: dict,
    failures: list[dict],
    attempt: int,
    max_attempts: int,
) -> str:
    """为批量生成失败后的修复 Agent 构造 task。"""
    by_type: dict[str, list[dict]] = {}
    for f in failures:
        by_type.setdefault(f.get("planned_type", "unknown"), []).append(f)

    summary_lines = []
    for typ, fs in sorted(by_type.items()):
        summary_lines.append(f"- type={typ}: {len(fs)} 组失败")
        for f in fs[:3]:
            err = (f.get("error") or "").replace("\n", " ")
            summary_lines.append(f"  * #{f.get('index')}: {err[:160]}")

    sample_blocks = []
    for f in failures[:5]:
        preview = f.get("input_preview") or ""
        if preview:
            sample_blocks.append(
                f"# 组号 {f.get('index')} (type={f.get('planned_type')})\n"
                f"```\n{preview[:600]}\n```"
            )
    sample_block = "\n\n".join(sample_blocks)

    parts = [
        "请修复当前工作目录的 gen.cpp / validator.cpp，使批量生成阶段不再失败。",
        f"\n这是第 {attempt}/{max_attempts} 轮自动修复。"
        "若本轮仍失败，将保留已生成组合法测例并中止任务。",
        f"\n【题面】\n{stmt_plain}",
        f"\n【数据范围】\n{range_plain}",
        f"\n【range.json】\n{json.dumps(range_json, ensure_ascii=False, indent=2)}",
        f"\n【失败统计】\n共 {len(failures)} 组失败\n" + "\n".join(summary_lines),
    ]
    if sample_block:
        parts.append(
            "\n【失败样例输入预览（供定位根因）】\n"
            f"{sample_block}\n"
            "每段预览已截断到 600 字符；完整数据范围以 range.json 为准。"
        )
    parts.append(
        "\n要求：\n"
        "1. 先 read_file('gen.cpp') 和 read_file('validator.cpp')，确认当前实现。\n"
        "2. 根据失败类型判断根因：\n"
        "   - validate FAILED：通常是 gen 输出违反约束（如范围、结构、sum、T 范围）；优先修 gen，"
        "必要时再调整 validator（不能为了过校验而牺牲正确性）。\n"
        "   - gen FAILED / TIMEOUT / MEMORY：生成器逻辑或规模控制问题；修 gen。\n"
        "   - std FAILED / TIMEOUT / MEMORY：数据规模对标程太大或输入格式不匹配；修 gen 降低规模 / 对齐格式。\n"
        "3. 用 write_gen 和 / 或 write_validate 写完整源码，不要只输出片段。\n"
        "4. 禁止修改 range.json；不要改 count、edge_cases、constraints。\n"
        "5. 改完后必须调用 run_self_check() 做强化自检，确保返回 OK。\n"
        "6. 自检通过后调 finish，说明本次修复改动点与根因。"
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
