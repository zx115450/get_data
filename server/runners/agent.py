"""Range / Gen Agent 阶段。"""
import json
import re
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
from pipeline.gen_data import (
    DEFAULT_REGULAR_COUNT,
    MIN_REGULAR_COUNT,
    _clamp_regular_count,
    infer_regular_count,
)

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


def _range_core_signature(data: dict) -> str:
    """用于判断审核前后 range 核心字段是否变化（复用 vs 重写）。"""
    core = {
        "problem_type": data.get("problem_type") or "",
        "constraints": data.get("constraints") or {},
        "edge_cases": data.get("edge_cases") or [],
        "special_constraints": data.get("special_constraints") or [],
        "count": data.get("count"),
    }
    return json.dumps(core, ensure_ascii=False, sort_keys=True, default=str)


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
    existing_range: dict | None = None,
) -> tuple[dict, str, bool]:
    """跑一轮 range-only Agent：无已有则新建；有则审核（合理复用 / 不合理重写）。

    返回 (range_dict, problem_type, reused)。
    reused=True 表示核心字段相对已有方案未改（可继续沿用旧 gen_plan）。
    """
    from pipeline.gen_data import normalize_range_json, validate_range_json
    from server.few_shots import PROBLEM_TYPE_RANGE_HINT, resolve_problem_type_from_range
    from server.range_agent import _build_all_type_hints_block

    path = job_dir / "range.json"
    existing: dict | None = None
    existing_note = ""
    before_sig = ""
    if isinstance(existing_range, dict) and existing_range.get("constraints"):
        cand = normalize_range_json(dict(existing_range))
        cand.pop("std_cmd", None)
        verrs = validate_range_json(cand)
        if verrs:
            existing_note = (
                "\n\n【已有 range.json（校验失败，必须重写）】\n"
                f"问题：{'; '.join(verrs[:6])}\n"
                f"```json\n{json.dumps(cand, ensure_ascii=False, indent=2)}\n```\n"
                "请 write_range 写出修正后的完整 JSON，再 finish（summary 以「重写:」开头）。\n"
            )
            job_store.add_progress(
                job, f"【Range】已有 range 校验失败，强制重写: {verrs[0]}"
            )
        else:
            existing = cand
            before_sig = _range_core_signature(existing)
            path.write_text(
                json.dumps(existing, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            existing_note = (
                "\n\n【已有 range.json（待审核）】\n"
                f"```json\n{json.dumps(existing, ensure_ascii=False, indent=2)}\n```\n"
                "请判断是否合理：合理则不要 write_range，直接 finish（summary 以「复用:」开头）；"
                "不合理则 write_range 完整重写后再 finish（summary 以「重写:」开头）。\n"
            )
            job_store.add_progress(
                job,
                f"【Range】审核已有方案: count={existing.get('count')} "
                f"type={existing.get('problem_type') or '-'} "
                f"edge_cases={existing.get('edge_cases')}",
            )

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

    if existing is not None:
        action_line = (
            "若判定已有 range 合理：禁止 write_range，直接 finish；"
            "若不合理：write_range 重写完整 JSON 后 finish。"
        )
        progress_label = "【Range】启动 range-only Agent 审核已有 range.json"
    elif existing_note:
        action_line = "已有 range 校验失败，必须 write_range 重写后 finish。"
        progress_label = "【Range】启动 range-only Agent 重写非法 range.json"
    else:
        action_line = "无已有方案：write_range 写出完整 JSON 后 finish。"
        progress_label = "【Range】启动 range-only Agent 写 range.json"

    range_task = (
        f"请规划/审核 range.json（含 problem_type）。\n\n"
        f"【题面】\n{stmt_plain}\n\n"
        f"【数据范围描述】\n{range_plain}\n"
        f"{std_hint}"
        f"{existing_note}"
        f"\n{PROBLEM_TYPE_RANGE_HINT}"
        f"{_build_all_type_hints_block()}"
        f"{pre_titles_block}"
        f"{struct_hint_block}"
        f"{special_block}"
        f"\ncount 由你根据覆盖需求自定（常规样例数），不得小于 {MIN_REGULAR_COUNT}；"
        f"统一规则 count ≥ max(15, 3^k)（k=小中大轴数；三维即 ≥27，不要写建议≥30）；"
        f"若上方有【特殊样例描述】，count 仍只写常规数（特殊组由后续方案叠加）。"
        f"constraints 覆盖题面中的规模变量（如 n、T、m）。"
        f"edge_cases 总数 4～6：优先 edge_n1/edge_nmax，其余给 special_constraints 关键结构（可合并，勿超 6）。"
        f"约束极值名须带 edge_ 前缀（edge_k_min，禁止 k_min）；结构名可无前缀。"
        f"{action_line}"
        f"务必填写 special_constraints 字段（即使为空数组也要写）。\n"
        f"务必填写 problem_type（与题面一致的英文标识符）。\n"
    )

    job_store.add_progress(job, progress_label)
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

    if not path.is_file():
        if existing is not None:
            path.write_text(
                json.dumps(existing, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            job_store.add_progress(job, "【Range】Agent 未写文件，回退复用审核前方案")
        else:
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

    after_sig = _range_core_signature(data)
    summary_l = (summary or "").strip()
    reused = bool(
        existing is not None
        and before_sig
        and after_sig == before_sig
    )
    if existing is not None and summary_l.startswith("复用") and after_sig == before_sig:
        reused = True
    if existing is not None and summary_l.startswith("重写"):
        reused = after_sig == before_sig  # 自称重写但内容相同仍视为复用

    want_special = bool((special_samples_desc or "").strip()) or bool(auto_discover_special)
    if want_special:
        from server.special_discover import (
            apply_schemes_to_range,
            discover_special_schemes,
        )
        existing_schemes = list(data.get("special_schemes") or [])
        if reused and existing_schemes:
            # count 已是「常规+特殊」总数，须先剥特殊再叠加，避免重复加
            regular = infer_regular_count(data)
            data = apply_schemes_to_range(
                data,
                existing_schemes,
                user_hint=special_samples_desc or data.get("special_samples_desc") or "",
                regular_count=regular,
            )
            job_store.add_progress(
                job,
                f"【Range】复用已有特殊方案 {len(existing_schemes)} 条，"
                f"special_count={data.get('special_samples_count')}",
            )
        else:
            job_store.add_progress(
                job,
                "【Range】单独调用大模型：理解特殊样例 → 产出 1 条方案（mutate/build 由模型选）"
                + ("（自动挖掘）" if auto_discover_special and not (special_samples_desc or "").strip() else ""),
            )
            # 新建/重写后 LLM 写的是常规数；若文件里仍残留旧特殊计数也先剥掉
            regular = infer_regular_count(data)
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
        # 无特殊样例：信任 LLM 的 count，夹到下限
        data["count"] = _clamp_regular_count(data.get("count"))
        data.pop("special_samples_desc", None)
        if not data.get("special_schemes"):
            data.pop("special_samples_count", None)
            data.pop("special_schemes", None)

    # special 叠加后可能改 count；复用判定仍以 Agent 审核后的核心字段为准
    errs = validate_range_json(data)
    if errs:
        raise RuntimeError(
            "Range Agent 产出的 range.json 不合法:\n" + "\n".join(f"  - {e}" for e in errs)
        )
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    action = "复用" if reused else ("重写" if existing is not None or existing_note else "新建")
    job_store.add_progress(
        job,
        f"【Range】{action} range.json: count={data.get('count')} "
        f"type={typ} edge_cases={data.get('edge_cases')}",
    )
    return data, typ, reused


def _build_planner_few_shot_block(
    job: job_store.Job,
    problem_type: str,
    stmt_plain: str,
    range_plain: str,
    std_code: str,
) -> str:
    """取压缩版 few-shot（结构要点），写进度日志，返回可拼进 Planner prompt 的文本块。"""
    few_shot_block, few_shot_summary = get_few_shot_rag(
        problem_type, stmt_plain, range_plain, std_code, top_k=2, compact=True,
    )
    if few_shot_summary.startswith("RAG 召回"):
        short = few_shot_summary.replace("RAG 召回 2 个模板: ", "")
        job_store.add_progress(job, f"few-shot(Planner压缩) RAG: {short}")
    else:
        job_store.add_progress(job, f"few-shot(Planner压缩): {few_shot_summary}")
    if not few_shot_block:
        return ""
    return (
        f"\n\n【参考结构要点 · 压缩 few-shot · 仅供 Planner】\n{few_shot_block}\n\n"
        "【用法 · 只借通用骨架】允许借鉴：registerGen、"
        "string type = opt<string>(\"type\",\"random\") + 字符串比较分支、"
        "opt(seed/index/count)、index 解组数/规模/数值轴的小中大全组合（轴名以本题 constraints 为准）、"
        "generator.h 的 gen()/edges()、validator 的 read*/readEoln/readEof/ensuref 模式。\n"
        "【禁止】opt<int>(\"type\") / if (type == 0)；禁止借范例的输入字段形状"
        "（几行几个数、n+数组、边列表形态、printf 字段顺序）"
        "与范例约束常数；禁止把要点扩写成完整源码或伪代码。\n"
        "第 1 节输入格式、是否多测、自环/有向/边权一律只写本题标程读入；"
        "第 4/8 节必须写清各轴小中大全组合（变量名用本题的，不必叫 t/n/ai）；"
        "禁止 random 恒组数=1、禁止数值轴全程打满；"
        "第 5/8 节只描述如何打印【本题输入】，不得套用范例输出形态。"
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
            "2. count 由你自定（常规样例数，不得小于 15）；特殊组由选中方案叠加，不要自行加减；\n"
            "3. edge_cases 不要写 special_samples；\n"
            "4. gen_special.cpp 由后续独立 SpecialCoder 阶段编写，本阶段不要实现 special_samples。"
        )
    if auto_discover_special:
        return (
            f"\n\n【自动挖掘特殊方案】已开启\n"
            f"每方案样例数：{special_samples_count}\n"
            "系统将在写出 range 后根据标程/题面自动挖方案；"
            "count 由你自定（常规样例数，不得小于 15）；"
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
    """准备文本并返回 (stmt_plain, task, preset, output_plain, std_for_prompt)。

    task 供 Coder 使用，不含完整 few-shot；压缩要点在 Plan 阶段单独注入。
    problem_type / eff_type 保留入参以兼容调用方（题型在 run_gen_agent 内再解析）。
    """
    _ = (eff_type, problem_type)  # 兼容签名；few-shot 改由 Planner 消费
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

    # few-shot 压缩要点只喂 Planner；range 每轮由 Range Agent 审核/重写，不再把 GUI 方案锁死为 preset
    preset = None
    special_block = _build_special_samples_block(
        special_samples_desc, special_samples_count,
        already_in_range=bool(range_json is not None and isinstance(range_json, dict) and range_json.get("constraints")),
        auto_discover_special=auto_discover_special,
    )
    if range_json is not None and isinstance(range_json, dict) and range_json.get("constraints"):
        from pipeline.gen_data import normalize_range_json, validate_range_json
        candidate = normalize_range_json(dict(range_json))
        candidate.pop("std_cmd", None)
        if special_samples_desc:
            candidate.setdefault("special_samples_desc", special_samples_desc)
            candidate.setdefault("special_samples_count", special_samples_count)
        if auto_discover_special:
            candidate["auto_discover_special"] = True
        verrs = validate_range_json(candidate)
        (job_dir / "range.json").write_text(
            json.dumps(candidate, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        if verrs:
            job_store.add_progress(
                job,
                "【Range 候选】GUI/历史方案校验未通过，将交 Range Agent 重写: "
                + verrs[0],
            )
        else:
            job_store.add_progress(
                job,
                f"【Range 候选】已有方案待审核: count={candidate.get('count')} "
                f"type={candidate.get('problem_type') or '-'} "
                f"edge_cases={candidate.get('edge_cases')}",
            )
    range_block = (
        "\n\n【range 流程】每次开始生成都会跑 Range Agent："
        "有已有 range.json 则先审核（合理复用 / 不合理重写），无则新建。"
        "最终以审核后的 range.json 为准编写 gen/validator。"
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
        f"{resume_failure_block}"
    )
    return stmt_plain, task, preset, output_plain, std_for_prompt


# Planner 篇幅：目标 ~1400；超过软上限则压缩补写一次（含复杂度预算节）
PLAN_TARGET_CHARS = 1400
PLAN_SOFT_MAX_CHARS = 4000


def _truncate_for_ref(text: str, head: int, tail: int, label: str) -> str:
    """截断长文本作冲突对照摘要。"""
    t = (text or "").strip()
    if not t:
        return ""
    limit = head + tail
    if len(t) <= limit + 80:
        return t
    return (
        t[:head]
        + f"\n\n...（{label}已截断，完整策略见 gen_plan.md）...\n\n"
        + t[-tail:]
    )


def _build_planner_task(
    stmt_plain: str,
    range_plain: str,
    output_plain: str,
    std_block: str,
    range_json: dict,
    eff_type: str,
    special_samples_desc: str = "",
    special_samples_count: int = 1,
    few_shot_block: str = "",
) -> tuple[str, str]:
    """构造 Planner 阶段的 (system_prompt, user_prompt)。使用纯文本 chat，不调用工具。

    必须含标程（std_block），以便写死输入格式与复杂度预算。
    few_shot_block 应为压缩结构要点（非完整源码）。
    """
    special_note = ""
    if (special_samples_desc or "").strip():
        special_note = (
            f"\n\n【特殊样例说明】range.json 含 special_samples_desc（{special_samples_count} 组），"
            "count 已包含这些组；edge_cases 不要写 special_samples；"
            "本 plan 只规划 gen.cpp / validator.cpp，不要规划 gen_special.cpp。"
        )
    output_block = ""
    if (output_plain or "").strip():
        output_block = (
            "\n【输出描述 · 仅供理解题意 / 估标程瓶颈】\n"
            "注意：以下是【标程】的输出格式，不是 gen 的输出格式。"
            "gen 只生成测例输入；禁止把下列答案格式/文案写进 gen 的 cout 步骤。\n"
            f"{output_plain.strip()}\n"
        )
    user_prompt = (
        "请为下面的算法题写一份可执行的 gen.cpp / validator.cpp 生成计划"
        f"（目标约 {PLAN_TARGET_CHARS} 字，勿超过 {PLAN_SOFT_MAX_CHARS} 字）。\n"
        "Coder 将严格按 plan 实现，请把策略写死（第 5/6 节必须可照做；第 8 节只用固定 4 行模板）。\n"
        "【提醒】gen stdout = 输入；标程 cout = 答案；二者不要写混。\n\n"
        f"【题面】\n{stmt_plain}\n\n"
        f"【数据范围】\n{range_plain}\n"
        f"{output_block}"
        f"{std_block}"
        f"\n【range.json】\n```json\n{json.dumps(range_json, ensure_ascii=False, indent=2)}```\n"
        f"{special_note}"
        f"{few_shot_block}"
        f"\n题型: {eff_type}\n"
        "\n要求：第 2 节仅 1 行 constraints/opt；edge_cases 每个一行（打满上界则同行写 ≤K复用）；"
        "第 6 节 ensuref 清单或 read*+readEoln+readEof；"
        "第 7 节只写 O(...)+瓶颈一句+K=整数（禁止逐 edge 展开）；"
        "第 8 节固定 4 行模板（禁止展开 edge / 禁止贴 opt 代码）；"
        "第 1 节输入格式只以标程为准；few-shot 只借通用骨架，禁止借范例输入字段形状。\n"
    )
    return prompts.build_planner_prompt(), user_prompt


def _plan_looks_complete(plan_text: str) -> bool:
    """粗略检查 plan 是否包含关键小节与可执行决策信号。"""
    if not plan_text or len(plan_text) < 80:
        return False
    text = plan_text.lower()
    required = [
        "1.", "2.", "3.", "4.", "5.", "6.", "7.", "8.",  # 8 节含复杂度预算
    ]
    if not all(h in text for h in required):
        return False
    # 第 7 节：复杂度 + K（状态密度可在第 4/5 节）
    has_complexity = ("复杂" in plan_text) or ("o(" in text) or ("复杂度" in plan_text)
    has_state_budget = (
        ("有效状态" in plan_text)
        or ("状态预算" in plan_text)
        or ("状态上界" in plan_text)
        or (("预算" in plan_text) and ("状态" in plan_text or "池" in plan_text or "k" in text))
        or (("有限域" in plan_text or "复用" in plan_text) and bool(re.search(r"K\s*[=≤＜<]\s*\d+", plan_text, re.I)))
    )
    # 抽出明确写出的 K=数字；过大视为假预算（数组上限伪装）
    k_vals = [int(x) for x in re.findall(r"K\s*[=≤＜<]\s*(\d+)", plan_text, flags=re.I)]
    has_numeric_k = bool(k_vals) or bool(
        re.search(
            r"(?:唯一[^。\n]{0,20}\d{2,})"
            r"|(?:上界[^。\n]{0,12}\d{2,})"
            r"|(?:有限域[^。\n]{0,12}\d{2,})"
            r"|(?:池[^。\n]{0,8}\d{2,})",
            plan_text,
            re.I,
        )
    )
    k_too_large = any(k > 5000 for k in k_vals)
    fake_budget = bool(
        re.search(
            r"(理论全集|与(?:输出)?规模同阶|与\s*m\s*同阶"
            r"|≤\s*2\s*[×x*]?\s*m|<=\s*2\s*[×x*]?\s*m|≤\s*2m|<=\s*2m"
            r"|刚好(?:装进|在\s*K)|数组(?:长度|上界|大小).{0,12}(?:决定|取作|作为)\s*K"
            r"|K\s*[=≤＜<]\s*(?:数组|上界))",
            plan_text,
            re.I,
        )
    )
    # 小中档多样 + 最大档控 K：需有分层信号
    has_tier = bool(
        re.search(r"(小档|中档|大档)", plan_text)
        and re.search(r"(有限域|复用|状态域|状态密度|半开|放宽)", plan_text)
    )
    # 第 8 / API：include 决策
    has_include = (
        "testlib" in text
        or "generator.h" in text
        or "#include" in text
        or "include" in text
    )
    # 第 6：validator 可执行信号
    has_val = ("readeof" in text) or ("ensuref" in text) or ("validator" in text) or ("校验" in plan_text)
    # 第 8：实现思路（步骤级，非空话）
    has_impl = (
        ("实现思路" in plan_text)
        or ("实现顺序" in plan_text)
        or ("registergen" in text)
        or ("write_gen" in text)
    )
    return (
        has_complexity
        and has_state_budget
        and has_numeric_k
        and (not k_too_large)
        and (not fake_budget)
        and has_tier
        and has_include
        and has_val
        and has_impl
    )


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
    """构造 Coder Agent 的精简 task：以 gen_plan.md + range.json 为主，题面/标程仅作冲突对照。"""
    special_note = ""
    if (special_samples_desc or "").strip():
        special_note = (
            f"\n注意：特殊样例（{special_samples_count} 组）由后续 SpecialCoder 编写 gen_special.cpp；"
            "本阶段不要写 gen_special，也不要在 gen.cpp 实现 special_samples。\n"
        )
    stmt_ref = _truncate_for_ref(stmt_plain, 450, 250, "题面")
    range_ref = _truncate_for_ref(range_plain, 300, 150, "范围描述")
    std_ref = _truncate_for_ref(std_for_prompt, 500, 400, "标程")
    output_ref = (output_plain or "").strip()
    if len(output_ref) > 400:
        output_ref = output_ref[:400] + "\n...（输出描述已截断）..."

    conflict_parts = [f"【冲突对照摘要 · 题型 {eff_type}】仅当与 gen_plan 冲突或格式不明时参考；禁止据此改策略。"]
    if stmt_ref:
        conflict_parts.append(f"\n【题面摘要】\n{stmt_ref}")
    if range_ref:
        conflict_parts.append(f"\n【数据范围摘要】\n{range_ref}")
    if output_ref:
        conflict_parts.append(
            "\n【输出描述摘要 · 标程输出格式，非 gen 输出】\n"
            f"{output_ref}"
        )
    if std_ref:
        conflict_parts.append(
            f"\n【标程摘要（读入格式对照）】\n```\n{std_ref}\n```"
        )
    conflict_block = "\n".join(conflict_parts)

    skeleton = ""
    if (eff_type or "") in ("tree", "weighted_tree", "graph", "weighted_graph"):
        skeleton = (
            "\n【最短可编骨架 · 树/图 · 必遵守】\n"
            '#include "generator.h"\n'
            "using namespace std;\n"
            "using namespace generator::all;\n"
            "int main(int argc, char* argv[]) {\n"
            "  registerGen(argc, argv, 1);\n"
            '  int seed = opt<int>("seed", 0);\n'
            '  string type = opt<string>("type", "random");  // 禁止 opt<int>("type")\n'
            '  int index = opt<int>("index", 0), count = opt<int>("count", 30);\n'
            "  // 再 opt 全部 constraints\n"
            "  // 无边权或多字段边（如 u v a b）：\n"
            "  //   unweight::Tree t(n); t.gen();\n"
            "  //   for (auto &e : t.edges()) { /* u v + 本题边字段；"
            "权用 rnd.next(1, 1000000000) */ }\n"
            "  // 单边权：edge_weight::Tree<int> + set_edges_weight_function；"
            "禁止 weight:: / set_weight_limit / 1e9\n"
            '  if (type == "random") { /* ... */ }\n'
            '  else if (type == "edge_xxx") { /* 与 range 同名 */ }\n'
            "  return 0;\n"
            "}\n"
        )

    return (
        "请把 gen_plan.md 逐条翻译成完整的 gen.cpp 和 validator.cpp。\n"
        "【分工】你只负责实现；禁止重新设计 edge_cases / API / 预算；"
        "按第 5/6 节落地，第 8 节仅为短模板顺序提示。\n"
        "【冲突原则】若 plan 第 5 节与第 1 节输入格式或标程读入矛盾"
        "（例如要求 gen 打印答案/失败文案/排列），以第 1 节 + 标程读入为准，只打印输入。\n\n"
        "【content 书写 · 必读】write_gen / write_validate 的 arguments 必须含完整 content"
        "（从 #include 到 main 结尾 }）；禁止空调用、半截、摘要；宜短而全，防止 JSON 截断"
        "（出现 recovered / missing_content 须立刻整份重写）。\n"
        f"{skeleton}\n"
        f"【range.json】\n```json\n{json.dumps(range_json, ensure_ascii=False, indent=2)}```\n\n"
        f"{conflict_block}\n\n"
        "要求：\n"
        "1. 只 read_file('gen_plan.md') 一次；range.json 已在上方，禁止再读。\n"
        "2. 首轮勿读 gen.cpp / validator.cpp；读完 plan 后直接 write_gen + write_validate"
        "（各自带完整 content，可并行）。\n"
        "3. 【规格优先级】gen_plan.md 第 5/6/7 节（第 8 节为短模板）> range.json > 上方冲突对照摘要；"
        "但「gen 只打印输入」高于错误的答案输出步骤；API 另遵守系统【generator.h API】硬约束。\n"
        "4. 覆盖 plan/range 中全部 edge_cases 分支与 constraints 变量 opt"
        "（seed/index/count/type + 全部约束名）。\n"
        "   【type 硬约束】必须 `string type = opt<string>(\"type\", \"random\")`，"
        "并用 `if (type == \"random\")` / `else if (type == \"edge_xxx\")` 分支；"
        "若 plan 误写 `opt<int>(\"type\")` 或 `type == 0`，以本条为准改成 string"
        "（否则编译 no match for operator==）。\n"
        "5. 严格按 gen_plan 第 4/5/7 节：小中档多样、大档与打满上界的 edge ≤K；满规模≠满状态。"
        "若 plan 的 K 过大，大档仍按 ≤500 实现。\n"
        "6. validator 按 plan 第 6 节清单实现（校验输入，不校验答案）。\n"
        "7. 对每种 edge_type 做 run_gen → run_validate → run_std 三连自检。\n"
        "8. 最后调用 run_self_check()，通过后 finish。\n"
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
    output_plain: str = "",
    std_for_prompt: str = "",
    lang: str = "cpp",
) -> tuple[str, str]:
    """启动 Range/Gen Agent。Plan-and-Execute：先写 gen_plan.md，再按 plan 写代码。

    Planner 使用完整标程/输出描述写死策略（输出描述降权为标程格式，非 gen 输出）；
    Coder 默认只拿 plan + range + 冲突对照摘要，冲突时以输入格式为准。

    返回 (Agent summary, 生效题型)。
    """
    from server.few_shots import normalize_problem_type

    range_plain = _extract_range_plain(task)
    if not (std_for_prompt or "").strip():
        std_for_prompt = std_code or ""
        if len(std_for_prompt) > 12000:
            std_for_prompt = (
                std_code[:6000]
                + f"\n\n/* ... std 共 {len(std_code)} 字符，中间已省略 ... */\n\n"
                + std_code[-4000:]
            )
    planner_std_block = build_std_block(std_for_prompt, lang) if (std_for_prompt or "").strip() else ""
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

    # 每次开始生成都跑 Range：无方案则新建；有方案则审核（合理复用 / 不合理重写）
    stage_keys = ["range", "gen"]
    if resume_info and range_path.is_file():
        if has_gen_val_at_resume(job_dir):
            job_store.add_progress(
                job, "【续跑】已有 range + gen/validator；仍先审核 range，再进入修复/完善",
            )
        else:
            job_store.add_progress(
                job,
                "【续跑】仅有 range.json（无 gen/validator）；先审核 range，再编写 gen/validator",
            )

    existing_for_review = range_json if isinstance(range_json, dict) else None
    job_store.add_progress(
        job,
        f"Agent prompt stages: {stage_keys} | range="
        + ("审核已有" if existing_for_review and existing_for_review.get("constraints") else "新建"),
    )
    range_json, typ, range_reused = _run_range_only_agent(
        job, job_dir, stmt_plain, range_plain, std_code, "", on_event,
        special_samples_desc=special_samples_desc,
        special_samples_count=special_samples_count,
        auto_discover_special=auto_discover_special,
        existing_range=existing_for_review,
    )
    job_store.add_progress(
        job,
        f"Agent prompt stages: {stage_keys} | type={typ} | range_"
        + ("reused" if range_reused else "rewritten"),
    )

    typ = typ or "array"

    if (job_dir / "gen.cpp.good").is_file() or (job_dir / "gen.py.good").is_file():
        restore_good_snapshot(job_dir)
        job_store.add_progress(job, "已还原 gen/validator 好版本快照")

    # ---- Plan 阶段：单次纯文本生成 gen_plan.md（复用时若已有 plan 则跳过）----
    plan_path = job_dir / PLAN_FILE
    # range 被重写后旧 plan 可能过期，强制重做 Plan
    if plan_path.is_file() and not range_reused:
        try:
            plan_path.unlink()
            job_store.add_progress(job, "【Plan】range 已重写，清除旧 gen_plan.md")
        except OSError as e:
            job_store.add_progress(job, f"【Plan】清除旧 gen_plan.md 失败: {e}")
    if plan_path.is_file():
        job_store.add_progress(job, "检测到已有 gen_plan.md，跳过 Plan 阶段")
        plan_summary = "复用已有 gen_plan.md"
    else:
        planner_few_shot = _build_planner_few_shot_block(
            job, typ, stmt_plain, range_plain, std_code,
        )
        system_prompt, user_prompt = _build_planner_task(
            stmt_plain,
            range_plain,
            output_plain,
            planner_std_block,
            range_json or {},
            typ,
            special_samples_desc=special_samples_desc,
            special_samples_count=special_samples_count,
            few_shot_block=planner_few_shot,
        )
        job_store.add_progress(
            job,
            "【Plan】启动 Planner（含标程/输出描述 + 压缩 few-shot）生成 gen_plan.md",
        )
        try:
            plan_text = chat_text(system_prompt, user_prompt, temperature=0.3)
        except Exception as e:
            plan_text = ""
            job_store.add_progress(job, f"【Plan】Planner 调用失败: {type(e).__name__}: {e}")

        # 太短/缺关键小节：补一次完整但仍然简短的版本
        if not _plan_looks_complete(plan_text):
            job_store.add_progress(job, "【Plan】首次计划不完整，补一次可执行完整版")
            multi_retry = ""
            if (typ or "") == "multi_test" or (
                isinstance(range_json, dict)
                and (
                    (range_json.get("problem_type") == "multi_test")
                    or (
                        "t" in {str(k).lower() for k in (range_json.get("constraints") or {})}
                        and any(
                            "sum" in str(k).lower()
                            for k in (range_json.get("constraints") or {})
                        )
                    )
                )
            ):
                multi_retry = (
                    "\n【分布硬约束】第 4/8 节必须写清本题组数/规模/数值轴（名从 constraints 来，"
                    "不一定叫 t/n/ai）的小中大【全组合】（如 bA=i%3,bB=(i/3)%3,bC=(i/9)%3）；"
                    "禁止 random 恒组数=1；禁止数值全程打满；有 sum 时禁止双顶格。"
                )
            retry_prompt = (
                f"{user_prompt}\n\n"
                "【上一次计划被判定为不完整】请严格按 8 个小节重写，保持简短"
                f"（目标约 {PLAN_TARGET_CHARS} 字）：\n"
                "1. 输入格式（对照标程写死） "
                "2. 范围参数（仅 1 行 opt+constraints） "
                "3. 多测与 sum "
                "4. 规模分层（含小/中/大状态密度：小宽、中半开、大≤K） "
                "5. edge_cases（每行一个；打满上界同行写 ≤K复用；禁止一边一新状态） "
                "6. validator（ensuref 清单或 read*+readEoln+readEof） "
                "7. 复杂度预算（仅 O(...)+瓶颈一句+K=整数≤500；禁止逐 edge） "
                "8. 实现思路（固定 4 行模板：include / 全 opt / random+引用第5节 / validator+自检；禁止展开 edge）"
                f"{multi_retry}"
            )
            try:
                plan_text = chat_text(system_prompt, retry_prompt, temperature=0.3)
            except Exception as e:
                job_store.add_progress(job, f"【Plan】Planner 补写失败: {type(e).__name__}: {e}")

        # 补写后仍不完整：再补一次（避免把 K=数组上限等假预算交给 Coder）
        if plan_text.strip() and not _plan_looks_complete(plan_text):
            job_store.add_progress(job, "【Plan】补写后仍不完整，再补一次（强制 K≤500 分层）")
            retry2 = (
                f"{user_prompt}\n\n"
                "【仍不合格】必须同时满足：\n"
                "- 第 2 节仅 1 行；第 4 节写清小档放宽 / 中档半开 / 大档≤K；\n"
                "- 第 5 节打满上界的 edge 同行写 ≤K复用；\n"
                "- 第 7 节只写 O(...)+瓶颈+K=具体整数且 K≤500（禁止逐 edge、禁止数组长度假预算）；\n"
                "- 第 8 节固定 4 行模板，禁止展开 edge；\n"
                "- 保留 8 节与 readEoln/readEof / include。\n"
                f"目标约 {PLAN_TARGET_CHARS} 字；只输出合格 Markdown 计划。"
            )
            try:
                plan_text = chat_text(system_prompt, retry2, temperature=0.2)
            except Exception as e:
                job_store.add_progress(job, f"【Plan】第二次补写失败: {type(e).__name__}: {e}")

        # 过长：压缩一次（仍须保留 8 节，含复杂度预算与可执行决策）
        if _plan_too_long(plan_text):
            job_store.add_progress(
                job,
                f"【Plan】计划过长 ({len(plan_text)} 字符 > {PLAN_SOFT_MAX_CHARS})，压缩一次",
            )
            compress_prompt = (
                "请把下面的 gen_plan 压缩为更短 Markdown，保留全部 8 个小节与每个 edge_case 一行映射，"
                "尤其保留第 4 节状态密度分层、第 5 节打满上界的 ≤K复用、第 6 节 ensuref/readEoln/readEof、"
                "第 7 节仅 O(...) 与 K≤500（禁止逐 edge；禁止改成数组长度/与规模同阶假预算）。"
                "第 2 节压成 1 行；第 8 节压成固定 4 行模板并【删除】一切 edge 展开与 opt/type 示例代码"
                "（改为一句：分支前全 opt + string type，细则见 Coder 模板）。"
                f"全文控制在约 {PLAN_TARGET_CHARS} 字以内（软上限 {PLAN_SOFT_MAX_CHARS}）。"
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
            if _plan_looks_complete(plan_text):
                plan_summary = f"已生成 gen_plan.md ({len(plan_text)} 字符)"
            else:
                plan_summary = (
                    f"已生成 gen_plan.md ({len(plan_text)} 字符，仍缺分层/K 约束，"
                    "Coder 须按系统 PERF：大档 K≤500、小中档多样)"
                )
            job_store.add_progress(job, plan_summary)
        else:
            plan_summary = "Planner 未生成 gen_plan.md，Coder 将回退使用完整 task"
            job_store.add_progress(job, plan_summary)

    # ---- Execute 阶段：Coder 一次编码 + Gen Fixer 最多 4 轮 + 可选 Coder 重写 1 次 ----
    job_store.add_progress(job, "【Execute】Coder 第 1/1 轮：按 gen_plan 实现 gen/validator")
    coder_task = _build_coder_task(
        stmt_plain, range_plain, output_plain, std_for_prompt, range_json or {}, typ,
        resume_failure_block,
        special_samples_desc=special_samples_desc,
        special_samples_count=special_samples_count,
    )
    if not plan_path.is_file():
        # 无 plan 时回退：给完整上下文，避免 Coder 无规格可依
        coder_task = f"{task}\n\n【额外要求：无 gen_plan 回退】\n{coder_task}"
        job_store.add_progress(job, "【Execute】无 gen_plan.md，Coder 使用完整 task 回退")
    else:
        job_store.add_progress(job, "【Execute】Coder 精简上下文：plan + range + 冲突对照摘要")

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
            tools._clear_out_pairs(job_dir / "out", int(rj.get("count") or DEFAULT_REGULAR_COUNT))
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
