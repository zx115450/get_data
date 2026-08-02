"""Range / Gen Agent 阶段。"""
import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from agent import prompts, tools
from agent.core import run as agent_run
from agent.llm import chat_text
from server import job_store
from server.few_shots import get_few_shot_rag
from server.runners.adaptive import adaptive_steps
from server.runners.prompts import build_coder_rewrite_task, build_gen_fixer_task, build_std_block
from server.runners.snapshot import (
    has_gen_val_at_resume,
    restore_good_snapshot,
    save_good_snapshot,
)
from server.struct_hints import scan_structural_hints, scan_structural_titles
from server.text_agent import simplify_text
from utils.markup import to_plain_for_llm

RANGE_TOOL_SCHEMAS = [
    s for s in tools.TOOL_SCHEMAS
    if s["function"]["name"] in {"write_range", "finish"}
]
CODER_TOOL_SCHEMAS = [
    s for s in tools.TOOL_SCHEMAS
    if s["function"]["name"] in {
        "read_file", "read_range", "write_gen", "write_validate", "run_self_check", "finish"
    }
]
GEN_FIXER_TOOL_SCHEMAS = [
    s for s in tools.TOOL_SCHEMAS
    if s["function"]["name"] in {
        "read_file", "read_range", "write_gen", "write_validate", "run_gen", "run_validate", "run_std",
        "run_self_check", "finish",
    }
]
# 普通 Gen 阶段自检跳过特殊样例（由后续 SpecialCoder 负责）
_GEN_SELF_CHECK_ARGS = {"skip_special": True}
GEN_TOOL_SCHEMAS = GEN_FIXER_TOOL_SCHEMAS

PLAN_FILE = "gen_plan.md"


def _log_self_check(job, result: str, prefix: str = "自检结果") -> None:
    """进度写入自检结果：失败时落盘全文，进度保留全部 FAIL 行（不截断到半行）。"""
    text = result if isinstance(result, str) else str(result)
    if text.startswith("ERROR") or "\nFAIL " in ("\n" + text):
        try:
            tools._persist_self_check_fail(text)
        except Exception:
            pass
        job_store.add_progress(
            job, f"{prefix}: {tools.format_self_check_for_progress(text)}"
        )
    else:
        job_store.add_progress(
            job, f"{prefix}: {tools.format_self_check_for_progress(text)}"
        )


def _is_timeout_or_memory_fail(text: str) -> bool:
    t = (text or "").lower()
    return "timeout" in t or "memory_limit" in t


def _extract_range_plain(task: str) -> str:
    """从完整 task 文本里抽出【数据范围描述】段落。"""
    if "【数据范围描述】\n" not in task:
        return ""
    return task.split("【数据范围描述】\n")[1].split("\n\n【")[0]


def _load_range_json(job_dir: Path) -> dict | None:
    """读取并规范化 job_dir/range.json；不存在或非法时返回 None。"""
    path = job_dir / "range.json"
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    if not isinstance(data, dict) or not data.get("constraints"):
        return None
    from pipeline.gen_data import normalize_range_json

    data = normalize_range_json(dict(data))
    data.pop("std_cmd", None)
    return data


def _run_range_only_agent(
    job: job_store.Job,
    job_dir: Path,
    stmt_plain: str,
    range_plain: str,
    std_code: str,
    eff_type: str,
    on_event,
    special_samples_desc: str = "",
    special_samples_count: int = 1,
    auto_discover_special: bool = False,
) -> tuple[dict, str]:
    """真正跑一轮 range-only Agent，写出并校验 range.json。

    返回 (range_dict, problem_type)。题型由本次 write_range 写入 problem_type
    （不单独调大模型判型；不沿用用户下拉/历史题型）。
    若有特殊提示或 auto_discover_special，随后挖掘 special_schemes。
    """
    from pipeline.gen_data import normalize_range_json, validate_range_json
    from server.few_shots import PROBLEM_TYPE_RANGE_HINT, resolve_problem_type_from_range
    from server.range_agent import _build_all_type_hints_block

    struct_hint_block = scan_structural_hints(stmt_plain) or ""
    pre_titles = scan_structural_titles(stmt_plain) or []
    pre_titles_block = ""
    if pre_titles:
        pre_titles_block = (
            "\n\n【预扫描到的特殊结构约束（请据此填写 special_constraints）】\n"
            + "\n".join(f"- {t}" for t in pre_titles)
            + "\n请确认这些约束确实出现在题面里（不要凭空加），"
            "并补充题面里其它未被预扫描到的隐含约束。每条都要在 edge_cases 里加对应边界。\n"
        )

    std_hint = ""
    if std_code and std_code.strip():
        code = std_code.strip()
        if len(code) > 4000:
            code = code[:2000] + "\n/* ... */\n" + code[-1500:]
        std_hint = f"\n\n【标程片段，供推断题型与是否有多测 T】\n```\n{code}\n```\n"

    special_block = _build_special_samples_block(
        special_samples_desc, special_samples_count,
        auto_discover_special=auto_discover_special,
    )

    range_task = (
        f"请只产出 range.json（含 problem_type）。\n\n"
        f"【题面】\n{stmt_plain}\n\n"
        f"【数据范围描述】\n{range_plain}\n"
        f"{std_hint}"
        f"\n{PROBLEM_TYPE_RANGE_HINT}"
        f"{_build_all_type_hints_block()}"
        f"{pre_titles_block}"
        f"{struct_hint_block}"
        f"{special_block}"
        f"\ncount 必须写 15（常规样例数默认；用户未另行指定时禁止写其它数字）；"
        f"若上方有【特殊样例描述】，count 仍只写常规 15（特殊组由后续方案叠加）。"
        f"constraints 覆盖题面中的规模变量（如 n、T、m）。"
        f"edge_cases 用简短英文标识符，总数 4～6 个即可（含最小/最大规模与关键结构边界）。"
        f"写完 write_range 后 finish。"
        f"务必填写 special_constraints 字段（即使为空数组也要写）。\n"
        f"务必填写 problem_type（与题面一致的英文标识符）。\n"
    )

    job_store.add_progress(job, "【Range】启动 range-only Agent 写 range.json")
    summary = agent_run(
        range_task,
        max_steps=4,
        verbose=False,
        on_event=on_event,
        system_prompt=prompts.build_range_prompt(),
        tool_schemas=RANGE_TOOL_SCHEMAS,
        tool_limits={"write_range": 1},
    )
    job_store.add_progress(job, f"【Range】结束: {summary}")

    path = job_dir / "range.json"
    if not path.is_file():
        raise RuntimeError(f"Range Agent 未产出 range.json（summary={summary!r}）")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise RuntimeError(f"Range Agent 产出的 range.json 不是合法 JSON: {e}") from e

    data = normalize_range_json(dict(data))
    data.pop("std_cmd", None)
    typ = resolve_problem_type_from_range(data, stmt_plain, range_plain, std_code)
    data["problem_type"] = typ
    data["auto_discover_special"] = bool(auto_discover_special)
    job_store.add_progress(job, f"【Range】题型(range.json): {typ}")

    want_special = bool((special_samples_desc or "").strip()) or bool(auto_discover_special)
    if want_special:
        from server.special_discover import (
            apply_schemes_to_range,
            discover_special_schemes,
        )
        job_store.add_progress(
            job,
            "【Range】单独调用大模型：理解特殊样例 → 产出 1 条方案（mutate/build 由模型选）"
            + ("（自动挖掘）" if auto_discover_special and not (special_samples_desc or "").strip() else ""),
        )
        # 用户未提供数据方案：常规样例数固定默认 15，不信任 LLM 写的 count
        regular = 15
        schemes = discover_special_schemes(
            stmt_plain,
            range_plain,
            std_code=std_code,
            user_hint=special_samples_desc,
            problem_type=typ,
            samples_per_scheme=max(1, int(special_samples_count or 1)),
            auto_discover=bool(auto_discover_special),
        )
        data = apply_schemes_to_range(
            data,
            schemes,
            user_hint=special_samples_desc,
            regular_count=regular,
        )
        job_store.add_progress(
            job, f"【Range】特殊方案 {len(schemes)} 条，special_count={data.get('special_samples_count')}",
        )
    else:
        # 无特殊样例：总数即常规数，固定默认 15；清掉 LLM 误写的空特殊字段
        data["count"] = 15
        data.pop("special_samples_desc", None)
        if not data.get("special_schemes"):
            data.pop("special_samples_count", None)
            data.pop("special_schemes", None)

    errs = validate_range_json(data)
    if errs:
        raise RuntimeError(
            "Range Agent 产出的 range.json 不合法:\n" + "\n".join(f"  - {e}" for e in errs)
        )
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    job_store.add_progress(
        job,
        f"【Range】已写入 range.json: count={data.get('count')} "
        f"type={typ} edge_cases={data.get('edge_cases')}",
    )
    return data, typ


def _build_few_shot_block(
    job: job_store.Job,
    problem_type: str,
    stmt_plain: str,
    range_plain: str,
    std_code: str,
) -> str:
    """按题型取 few-shot，写进度日志，返回可拼进 task 的文本块（可能为空）。"""
    few_shot_block, few_shot_summary = get_few_shot_rag(
        problem_type, stmt_plain, range_plain, std_code, top_k=2
    )
    if few_shot_summary.startswith("RAG 召回"):
        short = few_shot_summary.replace("RAG 召回 2 个模板: ", "")
        job_store.add_progress(job, f"few-shot RAG: {short}")
    else:
        job_store.add_progress(job, f"few-shot: {few_shot_summary}")
    if not few_shot_block:
        return ""
    return (
        f"\n\n{few_shot_block}\n\n"
        "【few-shot 仅供参考】上面范例只示范 testlib/generator.h 写法与分支组织，"
        "不是本题的输入格式或约束。"
        "必须以本题题面、标程读入顺序、range.json、gen_plan.md 为准；"
        "多测先输出 T、有向/自环/边权等不得照抄范例。"
        "树/图 API：必须 t.gen(); cout << t 或 for (auto &e : t.edges())；"
        "get_edges() / t.shuffle() 不存在，写错会编译失败。"
    )


def _build_special_samples_block(
    special_samples_desc: str,
    special_samples_count: int,
    already_in_range: bool = False,
    auto_discover_special: bool = False,
) -> str:
    """若启用特殊样例（用户提示或自动挖掘），返回一段拼进 task 的说明文本。"""
    desc = (special_samples_desc or "").strip()
    if desc:
        note = "（已写入 range.json）" if already_in_range else ""
        return (
            f"\n\n【特殊样例描述{note}】\n"
            f"描述：{desc}\n"
            f"每方案样例数：{special_samples_count}\n"
            "要求：\n"
            "1. range.json 可含 special_samples_desc / special_schemes；\n"
            "2. count 必须写 15（常规样例数）；特殊组由选中方案叠加，不要自行加减；\n"
            "3. edge_cases 不要写 special_samples；\n"
            "4. gen_special.cpp 由后续独立 SpecialCoder 阶段编写，本阶段不要实现 special_samples。"
        )
    if auto_discover_special:
        return (
            f"\n\n【自动挖掘特殊方案】已开启\n"
            f"每方案样例数：{special_samples_count}\n"
            "系统将在写出 range 后根据标程/题面自动挖方案；"
            "edge_cases 不要写 special_samples；本阶段不要写 gen_special。\n"
        )
    return ""


def prepare_prompts(
    problem_statement: str,
    data_range_desc: str,
    output_desc: str,
    std_code: str,
    lang: str,
    eff_type: str,
    problem_type: str,
    range_json: dict | None,
    resume_failure_block: str,
    job_dir: Path,
    job: job_store.Job,
    special_samples_desc: str = "",
    special_samples_count: int = 1,
    auto_discover_special: bool = False,
) -> tuple[str, str, dict | None, str, str]:
    """准备文本并返回 (stmt_plain, task, preset, output_plain, std_for_prompt)。"""
    try:
        stmt_plain = simplify_text(problem_statement or "", kind="statement")
        job_store.add_progress(
            job,
            f"题面已简化: {len(problem_statement or '')} → {len(stmt_plain)} 字",
        )
        (job_dir / "statement_simplified.txt").write_text(stmt_plain, encoding="utf-8")
    except Exception as e:
        stmt_plain = to_plain_for_llm(problem_statement or "")
        job_store.add_progress(job, f"题面简化失败: {type(e).__name__}: {e}")

    range_plain = to_plain_for_llm(data_range_desc or "")
    if range_plain != (data_range_desc or "").strip():
        job_store.add_progress(
            job,
            f"范围描述已 markup 清洗: {len(data_range_desc or '')} → {len(range_plain)} 字",
        )

    output_plain = to_plain_for_llm(output_desc or "")

    raw_stmt = problem_statement or ""
    raw_range = data_range_desc or ""
    if len(raw_stmt) > 6000:
        raw_stmt = raw_stmt[:3000] + "\n\n...（原始题面过长，中间省略）...\n\n" + raw_stmt[-2000:]
    if len(raw_range) > 3000:
        raw_range = raw_range[:1500] + "\n\n...（原始范围描述过长，中间省略）...\n\n" + raw_range[-1000:]
    original_ref_block = ""
    if raw_stmt.strip() or raw_range.strip():
        original_ref_block = (
            "\n\n【原始题面 / 范围描述（未简化，供参考）】\n"
            f"{raw_stmt}\n\n{raw_range}\n"
            "以上原始文本可能与简化版有差异，若冲突请优先以原始文本为准。"
        )

    output_block = ""
    if output_plain.strip():
        output_block = (
            f"\n\n【输出描述】\n{output_plain}\n"
            "写 gen.cpp 时请确保输出格式与上述描述一致。"
        )

    std_for_prompt = std_code
    if len(std_for_prompt) > 12000:
        std_for_prompt = (
            std_code[:6000]
            + f"\n\n/* ... std 共 {len(std_code)} 字符，中间已省略；完整标程已编译，可用 run_std 实测 ... */\n\n"
            + std_code[-4000:]
        )
        job_store.add_progress(job, f"标程过长({len(std_code)}字)，喂给 Agent 的文本已截断；run_std 仍用完整标程")
    std_block = build_std_block(std_for_prompt, lang)

    struct_hint_block = scan_structural_hints(stmt_plain) or scan_structural_hints(problem_statement or "")
    if struct_hint_block:
        job_store.add_progress(job, "检测到题面特殊结构约束，已注入针对性提醒")

    # 题型未定时先不注入 few-shot（等 Range 阶段 LLM 判型后再补）
    few_shot_block = ""
    if problem_type:
        few_shot_block = _build_few_shot_block(
            job, problem_type, stmt_plain, range_plain, std_code,
        )

    preset = None
    range_block = ""
    special_block = _build_special_samples_block(
        special_samples_desc, special_samples_count,
        already_in_range=bool(range_json is not None and isinstance(range_json, dict) and range_json.get("constraints")),
        auto_discover_special=auto_discover_special,
    )
    if range_json is not None and isinstance(range_json, dict) and range_json.get("constraints"):
        from pipeline.gen_data import normalize_range_json, validate_range_json
        preset = normalize_range_json(dict(range_json))
        preset.pop("std_cmd", None)
        # 合并用户通过 API 传入的特殊样例描述（若 range.json 中未写）
        if special_samples_desc:
            preset.setdefault("special_samples_desc", special_samples_desc)
            preset.setdefault("special_samples_count", special_samples_count)
        if auto_discover_special:
            preset["auto_discover_special"] = True
        errs0 = validate_range_json(preset)
        if errs0:
            raise RuntimeError("GUI 提供的 range.json 不合法:\n" + "\n".join(f"  - {e}" for e in errs0))
        (job_dir / "range.json").write_text(
            __import__("json").dumps(preset, ensure_ascii=False, indent=2), encoding="utf-8")
        job_store.add_progress(
            job,
            f"【跳过写 range】使用 GUI 已给方案: count={preset.get('count')} "
            f"type={preset.get('problem_type') or '-'} "
            f"edge_cases={preset.get('edge_cases')}",
        )
        sp_note = ""
        sp = preset.get("special_constraints") or []
        if sp:
            sp_note = (
                "\n\n【range.json 已标注的特殊结构约束 — gen/validator 必须显式保证】\n"
                + "\n".join(f"- {c}" for c in sp)
                +                 "\n每条约束都要在 gen.cpp 的某个 --type 分支里真正实现，"
                "并在 validator.cpp 建议用 ensuref 校验。以编译/运行通过为准。\n"
            )
        range_block = (
            f"\n\n【已给定 range.json — 禁止再调用 write_range，不要修改它】\n"
            f"```json\n{__import__('json').dumps(preset, ensure_ascii=False, indent=2)}```\n"
            "请直接写 gen.cpp / validator.cpp，--type 必须覆盖 edge_cases 中每一个名字；"
            "对每种 edge_type 做 run_gen→run_validate→run_std 三连自检，全过后 finish。"
            f"{sp_note}{special_block}"
        )
    else:
        range_block = (
            "\n\n要求：range.json 的 count 必须写 15（常规样例数默认；用户未另行指定时禁止写其它数字）；"
            "对 [L,R] 规模变量必须用 --index/--count 分层，15 组里既有小数据也有大数据（禁止只抽到 100 以内）。"
            "若有多测 T 且 sum n 有上限：必须同时覆盖「大T+小n」和「小T+大n」，禁止先抽大 n 再令 T=S/n（会把 T 压成 1~2）。"
            "按契约：先 write_range，再写 gen.cpp/validator.cpp，"
            "对每种 edge_type 做 run_gen→run_validate→run_std 三连自检，全过后调 finish。"
            f"{special_block}"
        )

    task = (
        f"请为下面的算法题生成测试数据。\n\n"
        f"【题面】（已自动简化为纯文本）\n{stmt_plain}\n\n"
        f"【数据范围描述】\n{range_plain}\n"
        f"{output_block}"
        f"{original_ref_block}"
        f"{std_block}"
        f"{range_block}"
        f"{struct_hint_block}"
        f"{few_shot_block}"
        f"{resume_failure_block}"
    )
    return stmt_plain, task, preset, output_plain, std_for_prompt


# Planner 篇幅：提示目标 ~1600；超过软上限则压缩补写一次（含复杂度预算节）
PLAN_TARGET_CHARS = 1600
PLAN_SOFT_MAX_CHARS = 2400


def _build_planner_task(
    stmt_plain: str,
    range_plain: str,
    output_plain: str,
    std_for_prompt: str,
    range_json: dict,
    eff_type: str,
    special_samples_desc: str = "",
    special_samples_count: int = 1,
) -> tuple[str, str]:
    """构造 Planner 阶段的 (system_prompt, user_prompt)。使用纯文本 chat，不调用工具。

    特殊样例由后续 SpecialCoder 单独规划，此处只提示 count 含特殊组、edge_cases 勿写 special_samples。
    """
    special_note = ""
    if (special_samples_desc or "").strip():
        special_note = (
            f"\n\n【特殊样例说明】range.json 含 special_samples_desc（{special_samples_count} 组），"
            "count 已包含这些组；edge_cases 不要写 special_samples；"
            "本 plan 只规划 gen.cpp / validator.cpp，不要规划 gen_special.cpp。"
        )
    user_prompt = (
        "请为下面的算法题写一份简短的 gen.cpp / validator.cpp 生成计划"
        f"（目标约 {PLAN_TARGET_CHARS} 字，勿超过 {PLAN_SOFT_MAX_CHARS} 字）。\n\n"
        f"【题面】\n{stmt_plain}\n\n"
        f"【数据范围】\n{range_plain}\n"
        f"{output_plain}\n"
        f"{std_for_prompt}\n"
        f"\n【range.json】\n```json\n{json.dumps(range_json, ensure_ascii=False, indent=2)}```\n"
        f"{special_note}"
        f"\n题型: {eff_type}\n"
        "\nedge_cases 每个一行；必须含第 7 节「复杂度与规模预算」，其中强制写清「有效状态预算」"
        "（最大档唯一顶点/字符串/权值种类等上界；满输出规模≠满状态）；不要复述题面或粘贴大段伪代码。\n"
    )
    return prompts.build_planner_prompt(), user_prompt


def _plan_looks_complete(plan_text: str) -> bool:
    """粗略检查 plan 是否包含关键小节，避免模型只写一两句。"""
    if not plan_text or len(plan_text) < 80:
        return False
    text = plan_text.lower()
    required = [
        "1.", "2.", "3.", "4.", "5.", "6.", "7.", "8.",  # 8 节含复杂度预算
    ]
    if not all(h in text for h in required):
        return False
    # 第 7 节：复杂度 + 有效状态预算（上界/池/状态 等）
    has_complexity = ("复杂" in plan_text) or ("o(" in text) or ("复杂度" in plan_text)
    has_state_budget = (
        ("有效状态" in plan_text)
        or ("状态预算" in plan_text)
        or ("状态上界" in plan_text)
        or (("预算" in plan_text) and ("状态" in plan_text or "池" in plan_text))
    )
    return has_complexity and has_state_budget


def _plan_too_long(plan_text: str) -> bool:
    return bool(plan_text) and len(plan_text) > PLAN_SOFT_MAX_CHARS


def _build_coder_task(
    stmt_plain: str,
    range_plain: str,
    output_plain: str,
    std_for_prompt: str,
    range_json: dict,
    eff_type: str,
    resume_failure_block: str,
    special_samples_desc: str = "",
    special_samples_count: int = 1,
) -> str:
    """构造 Coder Agent 的 task，要求根据 gen_plan.md 写代码。"""
    special_note = ""
    if (special_samples_desc or "").strip():
        special_note = (
            f"\n注意：特殊样例（{special_samples_count} 组）由后续 SpecialCoder 编写 gen_special.cpp；"
            "本阶段不要写 gen_special，也不要在 gen.cpp 实现 special_samples。\n"
        )
    return (
        "请根据当前工作目录的 gen_plan.md 写完整的 gen.cpp 和 validator.cpp。\n\n"
        "【content 书写 · 必读】write_gen / write_validate 的 arguments 必须含完整 content"
        "（从 #include 到 main 结尾 }）；禁止空调用、半截、摘要；宜短而全，防止 JSON 截断"
        "（出现 recovered / missing_content 须立刻整份重写）。"
        "实现必须带上题面+标程+range 全部上下文（edge_cases 分支、多测 T、约束变量 opt）。\n\n"
        f"【题面】\n{stmt_plain}\n\n"
        f"【数据范围】\n{range_plain}\n"
        f"{output_plain}\n"
        f"{std_for_prompt}\n"
        f"\n【range.json】\n```json\n{__import__('json').dumps(range_json, ensure_ascii=False, indent=2)}```\n"
        "要求：\n"
        "1. 只 read_file('gen_plan.md') 一次；range.json 已在上方，禁止再读。\n"
        "2. 首轮勿读 gen.cpp / validator.cpp；读完 plan 后直接 write_gen + write_validate"
        "（各自带完整 content，可并行）。\n"
        "3. 严格按 plan + 本题标程实现；task 里的 few-shot/参考范例仅作 API/风格参考，"
        "禁止照抄其「第一行 n m」或无自环约定（标程有 T 则先输出 T；n=1 按题面决定自环或 m=0）。\n"
        "4. 不要遗漏多测 / sum 约束 / edge_case 分支；写 gen 时对照上方全部上下文。\n"
        "5. gen.cpp 必须注册 seed / index / count / type 以及 range.json 中所有变量。\n"
        "6. 严格按 gen_plan 第 7 节「有效状态预算」实现：满规模≠满状态。\n"
        "7. 对每种 edge_type 做 run_gen → run_validate → run_std 三连自检。\n"
        "8. 最后调用 run_self_check() 做强化自检，通过后 finish。\n"
        f"{special_note}{resume_failure_block}"
    )


def _classify_self_check_error(self_check_result: str) -> str:
    """判断自检失败属于局部问题还是结构性问题。

    返回："local" 或 "structural"
    """
    text = (self_check_result or "").lower()

    structural_signals = [
        "timeout",
        "memory_limit",
        "unused key",
        "registergen",
        "read eof",
        "ensuref",
        "missing",
        "no edge",
        "edge_case",
    ]
    local_signals = [
        "off-by-one",
        "range error",
        "wrong format",
        "trailing",
        "space",
        "expected",
    ]
    structural_score = sum(1 for s in structural_signals if s in text)
    local_score = sum(1 for s in local_signals if s in text)

    # 大量 FAIL 行也视为结构性
    fail_count = text.count("fail")
    if fail_count >= 6:
        structural_score += 1
    if structural_score > local_score:
        return "structural"
    return "local"


def _needs_coder_rewrite(
    self_check_result: str,
    fixer_attempts: int,
    max_fixer_attempts: int,
) -> bool:
    """是否应带提示重开 Coder 重写骨架。

    触发条件：
    - 已用尽 Fixer 次数；或
    - 自检错误被判定为结构性问题（且 Fixer 至少已尝试 2 次）。
    """
    if fixer_attempts >= max_fixer_attempts:
        return True
    cls = _classify_self_check_error(self_check_result)
    if cls == "structural" and fixer_attempts >= 2:
        return True
    return False


def run_gen_agent(
    job: job_store.Job,
    job_dir: Path,
    task: str,
    eff_type: str,
    range_json: dict | None,
    resume_info: dict | None,
    on_event,
    stmt_plain: str = "",
    std_code: str = "",
    resume_failure_block: str = "",
    special_samples_desc: str = "",
    special_samples_count: int = 1,
    auto_discover_special: bool = False,
) -> tuple[str, str]:
    """启动 Range/Gen Agent。Plan-and-Execute：先写 gen_plan.md，再按 plan 写代码。

    返回 (Agent summary, 生效题型)。
    """
    from server.few_shots import (
        normalize_problem_type,
        resolve_problem_type_from_range,
    )

    range_plain = _extract_range_plain(task)
    range_path = job_dir / "range.json"

    # 续跑 / 磁盘已有方案时，把 range.json 读进内存，供后续 Plan/Coder 使用
    if range_json is None and range_path.is_file():
        range_json = _load_range_json(job_dir)

    resume_stage = resume_info.get("stage") if resume_info else None
    if resume_stage == "checker" and range_path.is_file() and has_gen_val_at_resume(job_dir):
        job_store.add_progress(job, "【续跑】父任务 checker 阶段失败，跳过 gen/validator Agent")
        typ = normalize_problem_type(eff_type) or normalize_problem_type(
            str((range_json or {}).get("problem_type") or "")
        ) or "array"
        return "checker 阶段续跑：跳过 gen/validator Agent", typ

    need_range_stage = range_json is None
    if resume_info and range_path.is_file() and not need_range_stage:
        stage_keys = ["gen"]
        if has_gen_val_at_resume(job_dir):
            job_store.add_progress(
                job, "【续跑】已有 range + gen/validator，进入修复/完善阶段",
            )
        else:
            job_store.add_progress(
                job,
                "【续跑】仅有 range.json（无 gen/validator），将重新编写 gen/validator",
            )
    elif not need_range_stage:
        stage_keys = ["gen"]
    else:
        stage_keys = ["range", "gen"]

    typ = normalize_problem_type(eff_type)
    if not typ and isinstance(range_json, dict):
        typ = normalize_problem_type(str(range_json.get("problem_type") or ""))

    # ---- Range 阶段：未提供方案时由 Range Agent 一并写出 problem_type ----
    if need_range_stage:
        job_store.add_progress(
            job,
            f"Agent prompt stages: {stage_keys} | type=(写入 range.json.problem_type)"
        )
        range_json, typ = _run_range_only_agent(
            job, job_dir, stmt_plain, range_plain, std_code, "", on_event,
            special_samples_desc=special_samples_desc,
            special_samples_count=special_samples_count,
            auto_discover_special=auto_discover_special,
        )
        # prepare_prompts 时若无题型会跳过 few-shot，此处补上
        if "【参考范例" not in task and "few-shot" not in task.lower():
            task = task + _build_few_shot_block(
                job, typ, stmt_plain, range_plain, std_code,
            )
    else:
        # 已有 range 但题型仍空：关键词兜底写回（不再单独调大模型）
        if not typ:
            typ = resolve_problem_type_from_range(
                range_json if isinstance(range_json, dict) else None,
                stmt_plain, range_plain, std_code,
            )
            if isinstance(range_json, dict):
                range_json = dict(range_json)
                range_json["problem_type"] = typ
                range_path.write_text(
                    json.dumps(range_json, ensure_ascii=False, indent=2), encoding="utf-8"
                )
            job_store.add_progress(job, f"【题型】从 range/关键词: {typ}")
            if "【参考范例" not in task:
                task = task + _build_few_shot_block(
                    job, typ, stmt_plain, range_plain, std_code,
                )
        job_store.add_progress(
            job,
            f"Agent prompt stages: {stage_keys} | type={typ}"
        )

    typ = typ or "array"

    if (job_dir / "gen.cpp.good").is_file() or (job_dir / "gen.py.good").is_file():
        restore_good_snapshot(job_dir)
        job_store.add_progress(job, "已还原 gen/validator 好版本快照")

    # ---- Plan 阶段：单次纯文本生成 gen_plan.md（复用时若已有 plan 则跳过）----
    plan_path = job_dir / PLAN_FILE
    if plan_path.is_file():
        job_store.add_progress(job, "检测到已有 gen_plan.md，跳过 Plan 阶段")
        plan_summary = "复用已有 gen_plan.md"
    else:
        system_prompt, user_prompt = _build_planner_task(
            stmt_plain,
            range_plain,
            "", "", range_json or {}, typ,
            special_samples_desc=special_samples_desc,
            special_samples_count=special_samples_count,
        )
        job_store.add_progress(job, "【Plan】启动 Planner 单次生成 gen_plan.md")
        try:
            plan_text = chat_text(system_prompt, user_prompt, temperature=0.3)
        except Exception as e:
            plan_text = ""
            job_store.add_progress(job, f"【Plan】Planner 调用失败: {type(e).__name__}: {e}")

        # 太短/缺关键小节：补一次完整但仍然简短的版本
        if not _plan_looks_complete(plan_text):
            job_store.add_progress(job, "【Plan】首次计划不完整，补一次简洁完整版")
            retry_prompt = (
                f"{user_prompt}\n\n"
                "【上一次计划被判定为不完整】请严格按 8 个小节重写，保持简短"
                f"（目标约 {PLAN_TARGET_CHARS} 字）：\n"
                "1. 输入格式 2. 范围参数 3. 多测与 sum 4. 规模分层 "
                "5. edge_cases（每名一行）6. validator "
                "7. 复杂度与规模预算（必须含有效状态上界数字/表达式） "
                "8. 实现顺序（最多 3 条）"
            )
            try:
                plan_text = chat_text(system_prompt, retry_prompt, temperature=0.3)
            except Exception as e:
                job_store.add_progress(job, f"【Plan】Planner 补写失败: {type(e).__name__}: {e}")

        # 过长：压缩一次（仍须保留 8 节，含复杂度预算）
        if _plan_too_long(plan_text):
            job_store.add_progress(
                job,
                f"【Plan】计划过长 ({len(plan_text)} 字符 > {PLAN_SOFT_MAX_CHARS})，压缩一次",
            )
            compress_prompt = (
                "请把下面的 gen_plan 压缩为更短 Markdown，保留全部 8 个小节与每个 edge_case 一行映射，"
                "尤其保留第 7 节的 O(...) 与「有效状态预算」数字，"
                f"全文控制在约 {PLAN_TARGET_CHARS} 字以内（硬上限 {PLAN_SOFT_MAX_CHARS}）。"
                "删除复述、伪代码和空话；只输出压缩后的计划。\n\n"
                f"【原计划】\n{plan_text}"
            )
            try:
                compressed = chat_text(system_prompt, compress_prompt, temperature=0.2)
                if _plan_looks_complete(compressed) and len(compressed) < len(plan_text):
                    plan_text = compressed
            except Exception as e:
                job_store.add_progress(job, f"【Plan】压缩失败（沿用原计划）: {type(e).__name__}: {e}")

        if plan_text.strip():
            plan_path.write_text(plan_text, encoding="utf-8")
            plan_summary = f"已生成 gen_plan.md ({len(plan_text)} 字符)"
            job_store.add_progress(job, plan_summary)
        else:
            plan_summary = "Planner 未生成 gen_plan.md，Coder 将直接按原 task 生成"
            job_store.add_progress(job, plan_summary)

    # ---- Execute 阶段：Coder 一次编码 + Gen Fixer 最多 4 轮 + 可选 Coder 重写 1 次 ----
    job_store.add_progress(job, "【Execute】Coder 第 1/1 轮：写 gen/validator")
    coder_task = _build_coder_task(
        stmt_plain, range_plain, "", "", range_json or {}, typ,
        resume_failure_block,
        special_samples_desc=special_samples_desc,
        special_samples_count=special_samples_count,
    )
    coder_task = f"{task}\n\n【额外要求：Plan-and-Execute】\n{coder_task}"

    coder_summary = agent_run(
        coder_task,
        max_steps=12,
        verbose=False,
        on_event=on_event,
        system_prompt=prompts.build_coder_prompt(typ),
        tool_schemas=CODER_TOOL_SCHEMAS,
        write_check_discipline=True,
        self_check_fast=True,
        self_check_args=_GEN_SELF_CHECK_ARGS,
    )
    job_store.add_progress(job, f"Coder 结束: {coder_summary}")

    max_fixer_attempts = 4
    fixer_summary = ""

    def _run_coder_rewrite(fail_text: str, label: str) -> str:
        """跑一轮 Coder Rewrite，返回 agent summary。"""
        job_store.add_progress(job, f"【{label}】启动 Coder Rewrite（按失败摘要重写 gen/validator）")
        retry_task = build_coder_rewrite_task(
            stmt_plain, range_plain, range_json or {},
            fail_text,
            f"Coder 摘要: {coder_summary}\nFixer 摘要: {fixer_summary}",
        )
        retry_summary = agent_run(
            retry_task,
            max_steps=12,
            verbose=False,
            on_event=on_event,
            system_prompt=prompts.build_coder_rewrite_prompt(typ),
            tool_schemas=CODER_TOOL_SCHEMAS,
            write_check_discipline=True,
            self_check_fast=True,
            self_check_args=_GEN_SELF_CHECK_ARGS,
        )
        job_store.add_progress(job, f"{label} 结束: {retry_summary}")
        return retry_summary

    def _full_gate(self_check_result: str, prefix: str) -> tuple[bool, str]:
        """Gen Agent 唯一大数据/抗压门禁：完整自检。

        通过返回 (True, 摘要)；失败则修复后再完整自检。
        TIMEOUT/MEMORY 类：Rewrite 后必须以 full 通过为准（不以 tiny/fast 收工）。
        """
        # 第 1 次完整自检
        job_store.add_progress(
            job, f"{prefix} 强制跑 run_self_check(fast_mode=False) 做交付前完整自检"
        )
        self_check_result = tools.run_self_check(fast_mode=False, **_GEN_SELF_CHECK_ARGS)
        _log_self_check(job, self_check_result)

        if isinstance(self_check_result, str) and self_check_result.startswith("OK"):
            save_good_snapshot(job_dir, include_in_out=False)
            msg = f"{prefix} 完整自检通过"
            job_store.add_progress(job, msg)
            job_store.add_progress(job, f"Agent 结束: {msg}")
            return True, msg

        fail_preview = tools.format_self_check_for_progress(self_check_result)
        job_store.add_progress(
            job,
            f"{prefix} 完整自检失败（大数据/抗压未通过），进入修复轮:\n{fail_preview}",
        )

        is_structural = _classify_self_check_error(self_check_result) == "structural"
        is_timeoutish = _is_timeout_or_memory_fail(self_check_result)

        if is_structural or is_timeoutish:
            # 性能/骨架/超时：Rewrite；TIMEOUT 必须以 full 验收
            job_store.add_progress(
                job,
                "【完整自检失败】结构性/性能问题"
                + ("（含 TIMEOUT/MEMORY）" if is_timeoutish else "")
                + "，启动 Coder Rewrite",
            )
            _run_coder_rewrite(self_check_result, "Coder Rewrite")

            if is_timeoutish:
                job_store.add_progress(
                    job,
                    "【TIMEOUT/MEMORY】Rewrite 后跳过 tiny 收工，直接强制完整自检",
                )
            else:
                tiny_result = tools.run_self_check(tiny_mode=True, **_GEN_SELF_CHECK_ARGS)
                _log_self_check(job, tiny_result, "tiny 自检结果")
                if not isinstance(tiny_result, str) or not tiny_result.startswith("OK"):
                    restore_good_snapshot(job_dir, suffix=".fixer_bak")
                    msg = f"{prefix} 完整自检失败后 Rewrite + tiny 仍失败"
                    job_store.add_progress(job, msg)
                    return False, msg
        else:
            # 局部问题：Gen Fixer tiny 最多 2 轮
            job_store.add_progress(job, "【完整自检失败】局部问题，启动 Gen Fixer (tiny 模式)")
            retry_ok = False
            for attempt in range(1, 3):
                retry_task = build_gen_fixer_task(
                    stmt_plain, range_plain, range_json or {},
                    self_check_result, attempt, 2,
                )
                retry_summary = agent_run(
                    retry_task,
                    max_steps=12,
                    verbose=False,
                    on_event=on_event,
                    system_prompt=prompts.build_gen_fixer_prompt(typ),
                    tool_schemas=GEN_FIXER_TOOL_SCHEMAS,
                    write_check_discipline=True,
                    self_check_fast=True,
                    self_check_args=_GEN_SELF_CHECK_ARGS,
                )
                job_store.add_progress(
                    job, f"Fixer tiny 第 {attempt}/2 轮结束: {retry_summary}"
                )
                tiny_result = tools.run_self_check(tiny_mode=True, **_GEN_SELF_CHECK_ARGS)
                _log_self_check(job, tiny_result, "tiny 自检结果")
                if isinstance(tiny_result, str) and tiny_result.startswith("OK"):
                    retry_ok = True
                    break
            if not retry_ok:
                restore_good_snapshot(job_dir, suffix=".fixer_bak")
                msg = f"{prefix} 完整自检失败后 Fixer tiny 用尽仍失败"
                job_store.add_progress(job, msg)
                return False, msg

        # 第 2 次完整自检（TIMEOUT Rewrite 后的必过门禁）
        job_store.add_progress(
            job, f"{prefix} 修复后再次跑 run_self_check(fast_mode=False) 完整自检"
        )
        self_check_result = tools.run_self_check(fast_mode=False, **_GEN_SELF_CHECK_ARGS)
        _log_self_check(job, self_check_result)

        if isinstance(self_check_result, str) and self_check_result.startswith("OK"):
            save_good_snapshot(job_dir, include_in_out=False)
            msg = f"{prefix} 完整自检（第 2 次）通过"
            job_store.add_progress(job, msg)
            job_store.add_progress(job, f"Agent 结束: {msg}")
            return True, msg

        # TIMEOUT/MEMORY 仍失败：再给一次 Rewrite，且仍必须以 full 通过
        if is_timeoutish or _is_timeout_or_memory_fail(self_check_result):
            job_store.add_progress(
                job,
                "【TIMEOUT/MEMORY】完整自检第 2 次仍失败，再启动一轮 Coder Rewrite，"
                "必须以 full 通过为准（见 self_check_last_fail.txt）",
            )
            _run_coder_rewrite(self_check_result, "Coder Rewrite #2 (TIMEOUT)")
            job_store.add_progress(
                job, f"{prefix} TIMEOUT Rewrite #2 后强制完整自检"
            )
            self_check_result = tools.run_self_check(
                fast_mode=False, **_GEN_SELF_CHECK_ARGS
            )
            _log_self_check(job, self_check_result)
            if isinstance(self_check_result, str) and self_check_result.startswith("OK"):
                save_good_snapshot(job_dir, include_in_out=False)
                msg = f"{prefix} TIMEOUT Rewrite 后完整自检通过"
                job_store.add_progress(job, msg)
                job_store.add_progress(job, f"Agent 结束: {msg}")
                return True, msg

        restore_good_snapshot(job_dir, suffix=".fixer_bak")
        # 避免半截 out/ 被阶段 4 误判为「测例已齐全」
        try:
            rj = range_json or {}
            tools._clear_out_pairs(job_dir / "out", int(rj.get("count") or 15))
        except Exception:
            pass
        msg = (
            f"{prefix} 完整自检最终仍失败:\n"
            f"{tools.format_self_check_for_progress(self_check_result)}"
        )
        job_store.add_progress(job, msg)
        return False, msg

    # Coder 第一版：快速自检，只验证结构/中小数据
    job_store.add_progress(job, "【Execute】Coder 后跑 run_self_check(fast_mode=True) 做结构验证")
    self_check_result = tools.run_self_check(fast_mode=True, **_GEN_SELF_CHECK_ARGS)
    _log_self_check(job, self_check_result)

    if isinstance(self_check_result, str) and self_check_result.startswith("OK"):
        ok, full_msg = _full_gate(self_check_result, "Coder")
        return full_msg, typ

    # Coder 快速自检失败：保存当前产物为 .fixer_bak 基线，不破坏已有的 .good 快照
    save_good_snapshot(job_dir, include_in_out=False, suffix=".fixer_bak")
    job_store.add_progress(job, "Coder 快速自检失败，已保存 .fixer_bak 基线")

    # Gen Fixer 循环：全部用快速自检，避免大数据压测反复跑
    for attempt in range(1, max_fixer_attempts + 1):
        # 判断是否需要进入骨架重写
        if _needs_coder_rewrite(self_check_result, attempt - 1, max_fixer_attempts):
            job_store.add_progress(
                job,
                f"【Fixer】第 {attempt} 轮触发结构性重写：错误类型为 {_classify_self_check_error(self_check_result)}，"
                "或 Fixer 次数已用尽，启动 Coder Rewrite"
            )
            break

        job_store.add_progress(job, f"【Fixer】第 {attempt}/{max_fixer_attempts} 轮：根据自检失败日志修复")
        fixer_task = build_gen_fixer_task(
            stmt_plain, range_plain, range_json or {},
            self_check_result, attempt, max_fixer_attempts,
        )
        fixer_summary = agent_run(
            fixer_task,
            max_steps=12,
            verbose=False,
            on_event=on_event,
            system_prompt=prompts.build_gen_fixer_prompt(typ),
            tool_schemas=GEN_FIXER_TOOL_SCHEMAS,
            write_check_discipline=True,
            self_check_fast=True,
            self_check_args=_GEN_SELF_CHECK_ARGS,
        )
        job_store.add_progress(job, f"Fixer 第 {attempt} 轮结束: {fixer_summary}")

        job_store.add_progress(job, "【Fixer】强制跑 run_self_check(fast_mode=True) 验证修复产物")
        self_check_result = tools.run_self_check(fast_mode=True, **_GEN_SELF_CHECK_ARGS)
        _log_self_check(job, self_check_result)

        if isinstance(self_check_result, str) and self_check_result.startswith("OK"):
            ok, full_msg = _full_gate(self_check_result, f"Fixer 第 {attempt} 轮")
            return full_msg, typ

        if attempt >= max_fixer_attempts:
            job_store.add_progress(
                job,
                "Fixer 用尽 "
                f"{max_fixer_attempts} 轮，快速自检仍失败:\n"
                f"{tools.format_self_check_for_progress(self_check_result)}",
            )
            break

        job_store.add_progress(job, f"【Fixer】第 {attempt} 轮自检失败，进入下一轮修复")

    # 若 Fixer 次数用尽或结构性错误，进入一次 Coder Rewrite（必须以 full 收工）
    if _needs_coder_rewrite(self_check_result, max_fixer_attempts, max_fixer_attempts):
        _run_coder_rewrite(self_check_result, "Coder Rewrite")

        # TIMEOUT 类：不要只靠 fast 收工；其余也优先进 full_gate
        job_store.add_progress(
            job, "【Coder Rewrite】强制跑 run_self_check(fast_mode=True) 做冒烟"
        )
        self_check_result = tools.run_self_check(fast_mode=True, **_GEN_SELF_CHECK_ARGS)
        _log_self_check(job, self_check_result)

        if isinstance(self_check_result, str) and self_check_result.startswith("OK"):
            ok, full_msg = _full_gate(self_check_result, "Coder Rewrite")
            return full_msg, typ

        # fast 仍失败但若是 TIMEOUT 残留线索：仍尝试 full_gate 无意义；回退
        # 若 fast 失败但是 TIMEOUT 场景下 rewrite 可能修了大档——仍强制再跑一次 full
        if _is_timeout_or_memory_fail(self_check_result):
            job_store.add_progress(
                job,
                "【TIMEOUT/MEMORY】Rewrite 后 fast 仍失败，仍强制完整自检验收",
            )
            ok, full_msg = _full_gate(self_check_result, "Coder Rewrite")
            return full_msg, typ

        restore_good_snapshot(job_dir, suffix=".fixer_bak")
        final_summary = (
            "Coder Rewrite 后快速自检仍失败:\n"
            f"{tools.format_self_check_for_progress(self_check_result)}"
        )
        job_store.add_progress(job, final_summary)
        job_store.add_progress(job, f"Agent 结束: {final_summary}")
        return final_summary, typ

    # 未触发重写（理论上不会到这里，但兜底）
    restore_good_snapshot(job_dir, suffix=".fixer_bak")
    final_summary = (
        f"Coder + Fixer 用尽 {max_fixer_attempts} 轮，快速自检仍失败:\n"
        f"{tools.format_self_check_for_progress(self_check_result)}"
    )
    job_store.add_progress(job, final_summary)
    job_store.add_progress(job, f"Agent 结束: {final_summary}")
    return final_summary, typ
