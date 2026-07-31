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
    if s["function"]["name"] in {"write_range", "read_file", "read_range", "finish"}
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
GEN_TOOL_SCHEMAS = GEN_FIXER_TOOL_SCHEMAS

PLAN_FILE = "gen_plan.md"


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
) -> tuple[dict, str]:
    """真正跑一轮 range-only Agent，写出并校验 range.json。

    返回 (range_dict, problem_type)。若未给定题型，先单独 LLM 判型再写 range。
    """
    from pipeline.gen_data import normalize_range_json, validate_range_json
    from server.few_shots import classify_problem_type_llm, normalize_problem_type
    from server.range_agent import _build_type_hint_block

    typ = normalize_problem_type(eff_type)
    if not typ:
        job_store.add_progress(job, "【Range】单独调用大模型判断题型…")
        typ = classify_problem_type_llm(stmt_plain, range_plain, std_code)
        job_store.add_progress(job, f"【Range】题型(LLM): {typ}")
    else:
        job_store.add_progress(job, f"【Range】题型(用户指定): {typ}")

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
        std_hint = f"\n\n【标程片段，仅供推断是否有多测 T】\n```\n{code}\n```\n"

    range_task = (
        f"请只产出 range.json。\n\n"
        f"【题面】\n{stmt_plain}\n\n"
        f"【数据范围描述】\n{range_plain}\n"
        f"{std_hint}"
        f"\n【已判定题型】{typ}\n"
        f"{_build_type_hint_block(typ)}"
        f"{pre_titles_block}"
        f"{struct_hint_block}"
        f"\ncount 默认 15。constraints 覆盖题面中的规模变量（如 n、T、m）。"
        f"edge_cases 用简短英文标识符。写完 write_range 后 finish。"
        f"务必填写 special_constraints 字段（即使为空数组也要写）。\n"
    )

    job_store.add_progress(job, "【Range】启动 range-only Agent 写 range.json")
    summary = agent_run(
        range_task,
        max_steps=8,
        verbose=False,
        on_event=on_event,
        system_prompt=prompts.build_range_prompt(),
        tool_schemas=RANGE_TOOL_SCHEMAS,
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
    data["problem_type"] = typ
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
        "请参照上面 RAG 召回范例的写法风格（校验严格度、--type 分支方式），"
        "为本次题目写 gen.cpp 和 validator.cpp。"
        "树/图：必须 t.gen(); cout << t 或 for (auto &e : t.edges())；"
        "禁止 get_edges() / t.shuffle()。"
    )


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
    if range_json is not None and isinstance(range_json, dict) and range_json.get("constraints"):
        from pipeline.gen_data import normalize_range_json, validate_range_json
        preset = normalize_range_json(dict(range_json))
        preset.pop("std_cmd", None)
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
                + "\n每条约束都要在 gen.cpp 的某个 --type 分支里真正实现，"
                "并在 validator.cpp 用 ensuref 校验。不要只写 random 分支。\n"
            )
        range_block = (
            f"\n\n【已给定 range.json — 禁止再调用 write_range，不要修改它】\n"
            f"```json\n{__import__('json').dumps(preset, ensure_ascii=False, indent=2)}```\n"
            "请直接写 gen.cpp / validator.cpp，--type 必须覆盖 edge_cases 中每一个名字；"
            "对每种 edge_type 做 run_gen→run_validate→run_std 三连自检，全过后 finish。"
            f"{sp_note}"
        )
    else:
        range_block = (
            "\n\n要求：range.json 的 count 默认 15；"
            "对 [L,R] 规模变量必须用 --index/--count 分层，15 组里既有小数据也有大数据（禁止只抽到 100 以内）。"
            "若有多测 T 且 sum n 有上限：必须同时覆盖「大T+小n」和「小T+大n」，禁止先抽大 n 再令 T=S/n（会把 T 压成 1~2）。"
            "按契约：先 write_range，再写 gen.cpp/validator.cpp，"
            "对每种 edge_type 做 run_gen→run_validate→run_std 三连自检，全过后调 finish。"
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


def _build_planner_task(
    stmt_plain: str,
    range_plain: str,
    output_plain: str,
    std_for_prompt: str,
    range_json: dict,
    eff_type: str,
) -> tuple[str, str]:
    """构造 Planner 阶段的 (system_prompt, user_prompt)。使用纯文本 chat，不调用工具。"""
    user_prompt = (
        "请为下面的算法题写一份 gen.cpp / validator.cpp 的生成计划。\n\n"
        f"【题面】\n{stmt_plain}\n\n"
        f"【数据范围】\n{range_plain}\n"
        f"{output_plain}\n"
        f"{std_for_prompt}\n"
        f"\n【range.json】\n```json\n{json.dumps(range_json, ensure_ascii=False, indent=2)}```\n"
        f"\n题型: {eff_type}\n"
    )
    return prompts.build_planner_prompt(), user_prompt


def _plan_looks_complete(plan_text: str) -> bool:
    """粗略检查 plan 是否包含关键小节，避免模型只写一两句。"""
    if not plan_text or len(plan_text) < 100:
        return False
    text = plan_text.lower()
    required = [
        "1.", "2.", "3.", "4.", "5.", "6.", "7.",  # 至少含编号小节
    ]
    return all(h in text for h in required)


def _build_coder_task(
    stmt_plain: str,
    range_plain: str,
    output_plain: str,
    std_for_prompt: str,
    range_json: dict,
    eff_type: str,
    resume_failure_block: str,
) -> str:
    """构造 Coder Agent 的 task，要求根据 gen_plan.md 写代码。"""
    return (
        "请根据当前工作目录的 gen_plan.md 写完整的 gen.cpp 和 validator.cpp。\n\n"
        f"【题面】\n{stmt_plain}\n\n"
        f"【数据范围】\n{range_plain}\n"
        f"{output_plain}\n"
        f"{std_for_prompt}\n"
        f"\n【range.json】\n```json\n{__import__('json').dumps(range_json, ensure_ascii=False, indent=2)}```\n"
        "要求：\n"
        "1. 先 read_file('gen_plan.md') 读取计划。\n"
        "2. 严格按 plan 实现，不要遗漏多测 / sum 约束 / edge_case 分支。\n"
        "3. gen.cpp 必须注册 seed / index / count / type 以及 range.json 中所有变量。\n"
        "4. 对每种 edge_type 做 run_gen → run_validate → run_std 三连自检。\n"
        "5. 最后调用 run_self_check() 做强化自检，通过后 finish。\n"
        f"{resume_failure_block}"
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
) -> tuple[str, str]:
    """启动 Range/Gen Agent。Plan-and-Execute：先写 gen_plan.md，再按 plan 写代码。

    返回 (Agent summary, 生效题型)。
    """
    from server.few_shots import (
        classify_problem_type_llm,
        normalize_problem_type,
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
        job_store.add_progress(job, "【续跑】已有 range.json，直接进入 gen/validator 修复阶段")
    elif not need_range_stage:
        stage_keys = ["gen"]
    else:
        stage_keys = ["range", "gen"]

    typ = normalize_problem_type(eff_type)
    if not typ and isinstance(range_json, dict):
        typ = normalize_problem_type(str(range_json.get("problem_type") or ""))

    # ---- Range 阶段：未提供方案时先判题型再写 range.json ----
    if need_range_stage:
        job_store.add_progress(
            job,
            f"Agent prompt stages: {stage_keys} | type={typ or '(待 LLM 判定)'}"
        )
        range_json, typ = _run_range_only_agent(
            job, job_dir, stmt_plain, range_plain, std_code, typ, on_event,
        )
        # prepare_prompts 时若无题型会跳过 few-shot，此处补上
        if "【参考范例" not in task and "few-shot" not in task.lower():
            task = task + _build_few_shot_block(
                job, typ, stmt_plain, range_plain, std_code,
            )
    else:
        # 已有 range 但题型仍空：单独 LLM 判一次，并写回 range.json
        if not typ:
            job_store.add_progress(job, "【题型】方案已有但未含 problem_type，单独调用大模型判断…")
            typ = classify_problem_type_llm(stmt_plain, range_plain, std_code)
            if isinstance(range_json, dict):
                range_json = dict(range_json)
                range_json["problem_type"] = typ
                range_path.write_text(
                    json.dumps(range_json, ensure_ascii=False, indent=2), encoding="utf-8"
                )
            job_store.add_progress(job, f"【题型】LLM: {typ}")
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
        )
        job_store.add_progress(job, "【Plan】启动 Planner 单次生成 gen_plan.md")
        try:
            plan_text = chat_text(system_prompt, user_prompt, temperature=0.3)
        except Exception as e:
            plan_text = ""
            job_store.add_progress(job, f"【Plan】Planner 调用失败: {type(e).__name__}: {e}")

        # 如果太短/缺关键小节，可再补一次
        if not _plan_looks_complete(plan_text):
            job_store.add_progress(job, "【Plan】首次计划不完整，补一次更详细版本")
            retry_prompt = (
                f"{user_prompt}\n\n"
                "【上一次计划被判定为不完整】请严格按以下 7 个小节重新写计划，"
                "每节至少 2-3 条要点，不要省略：\n"
                "1. 输入格式 2. 范围参数 3. 多测与 sum 约束 4. 规模分层 5. edge_cases 映射 6. validator 校验点 7. 实现顺序"
            )
            try:
                plan_text = chat_text(system_prompt, retry_prompt, temperature=0.3)
            except Exception as e:
                job_store.add_progress(job, f"【Plan】Planner 补写失败: {type(e).__name__}: {e}")

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
    )
    job_store.add_progress(job, f"Coder 结束: {coder_summary}")

    max_fixer_attempts = 4
    fixer_summary = ""

    def _full_gate(self_check_result: str, prefix: str) -> tuple[bool, str]:
        """Gen Agent 唯一大数据/抗压门禁：完整自检。

        通过返回 (True, 摘要)；失败则进入一次 tiny 修复 + 再次完整自检，
        整次 Gen Agent 最多 2 次完整自检。
        """
        # 第 1 次完整自检
        job_store.add_progress(
            job, f"{prefix} 强制跑 run_self_check(fast_mode=False) 做交付前完整自检"
        )
        self_check_result = tools.run_self_check(fast_mode=False)
        job_store.add_progress(job, f"自检结果: {self_check_result[:500]}")

        if isinstance(self_check_result, str) and self_check_result.startswith("OK"):
            save_good_snapshot(job_dir, include_in_out=False)
            msg = f"{prefix} 完整自检通过"
            job_store.add_progress(job, msg)
            job_store.add_progress(job, f"Agent 结束: {msg}")
            return True, msg

        # 完整自检失败：按错误类型分流，用 tiny 模式快速迭代修复，再完整一次
        job_store.add_progress(
            job,
            f"{prefix} 完整自检失败（大数据/抗压未通过），进入 tiny 修复轮: "
            f"{self_check_result[:500]}"
        )

        is_structural = _classify_self_check_error(self_check_result) == "structural"
        if is_structural:
            # 性能/骨架问题：直接 Rewrite
            job_store.add_progress(job, "【完整自检失败】结构性/性能问题，启动 Coder Rewrite")
            retry_task = build_coder_rewrite_task(
                stmt_plain, range_plain, range_json or {},
                self_check_result,
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
            )
            job_store.add_progress(job, f"Coder Rewrite (tiny 修复) 结束: {retry_summary}")
            tiny_result = tools.run_self_check(tiny_mode=True)
            job_store.add_progress(job, f"tiny 自检结果: {tiny_result[:500]}")
            if not isinstance(tiny_result, str) or not tiny_result.startswith("OK"):
                restore_good_snapshot(job_dir, suffix=".fixer_bak")
                msg = f"{prefix} 完整自检失败后 Rewrite + tiny 仍失败"
                job_store.add_progress(job, msg)
                return False, msg
        else:
            # 局部/大数据边界问题：用 Gen Fixer tiny 最多 2 轮
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
                )
                job_store.add_progress(
                    job, f"Fixer tiny 第 {attempt}/2 轮结束: {retry_summary}"
                )
                tiny_result = tools.run_self_check(tiny_mode=True)
                job_store.add_progress(job, f"tiny 自检结果: {tiny_result[:500]}")
                if isinstance(tiny_result, str) and tiny_result.startswith("OK"):
                    retry_ok = True
                    break
            if not retry_ok:
                restore_good_snapshot(job_dir, suffix=".fixer_bak")
                msg = f"{prefix} 完整自检失败后 Fixer tiny 用尽仍失败"
                job_store.add_progress(job, msg)
                return False, msg

        # 第 2 次完整自检
        job_store.add_progress(
            job, f"{prefix} 修复后再次跑 run_self_check(fast_mode=False) 完整自检"
        )
        self_check_result = tools.run_self_check(fast_mode=False)
        job_store.add_progress(job, f"自检结果: {self_check_result[:500]}")

        if isinstance(self_check_result, str) and self_check_result.startswith("OK"):
            save_good_snapshot(job_dir, include_in_out=False)
            msg = f"{prefix} 完整自检（第 2 次）通过"
            job_store.add_progress(job, msg)
            job_store.add_progress(job, f"Agent 结束: {msg}")
            return True, msg

        restore_good_snapshot(job_dir, suffix=".fixer_bak")
        msg = f"{prefix} 完整自检第 2 次仍失败: {self_check_result[:500]}"
        job_store.add_progress(job, msg)
        return False, msg

    # Coder 第一版：快速自检，只验证结构/中小数据
    job_store.add_progress(job, "【Execute】Coder 后跑 run_self_check(fast_mode=True) 做结构验证")
    self_check_result = tools.run_self_check(fast_mode=True)
    job_store.add_progress(job, f"自检结果: {self_check_result[:500]}")

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
        )
        job_store.add_progress(job, f"Fixer 第 {attempt} 轮结束: {fixer_summary}")

        job_store.add_progress(job, "【Fixer】强制跑 run_self_check(fast_mode=True) 验证修复产物")
        self_check_result = tools.run_self_check(fast_mode=True)
        job_store.add_progress(job, f"自检结果: {self_check_result[:500]}")

        if isinstance(self_check_result, str) and self_check_result.startswith("OK"):
            ok, full_msg = _full_gate(self_check_result, f"Fixer 第 {attempt} 轮")
            return full_msg, typ

        if attempt >= max_fixer_attempts:
            job_store.add_progress(
                job,
                f"Fixer 用尽 {max_fixer_attempts} 轮，快速自检仍失败: {self_check_result[:500]}"
            )
            break

        job_store.add_progress(job, f"【Fixer】第 {attempt} 轮自检失败，进入下一轮修复")

    # 若 Fixer 次数用尽或结构性错误，进入一次 Coder Rewrite
    if _needs_coder_rewrite(self_check_result, max_fixer_attempts, max_fixer_attempts):
        job_store.add_progress(job, "【Coder Rewrite】骨架重写：按失败摘要重新设计 gen/validator")
        rewrite_task = build_coder_rewrite_task(
            stmt_plain, range_plain, range_json or {},
            self_check_result,
            f"Coder 摘要: {coder_summary}\nFixer 摘要: {fixer_summary}",
        )
        rewrite_summary = agent_run(
            rewrite_task,
            max_steps=12,
            verbose=False,
            on_event=on_event,
            system_prompt=prompts.build_coder_rewrite_prompt(typ),
            tool_schemas=CODER_TOOL_SCHEMAS,
            write_check_discipline=True,
            self_check_fast=True,
        )
        job_store.add_progress(job, f"Coder Rewrite 结束: {rewrite_summary}")

        job_store.add_progress(job, "【Coder Rewrite】强制跑 run_self_check(fast_mode=True) 验证重写产物")
        self_check_result = tools.run_self_check(fast_mode=True)
        job_store.add_progress(job, f"自检结果: {self_check_result[:500]}")

        if isinstance(self_check_result, str) and self_check_result.startswith("OK"):
            ok, full_msg = _full_gate(self_check_result, "Coder Rewrite")
            return full_msg, typ

        # Rewrite 仍失败：回退 .fixer_bak 基线
        restore_good_snapshot(job_dir, suffix=".fixer_bak")
        final_summary = f"Coder Rewrite 后快速自检仍失败: {self_check_result[:500]}"
        job_store.add_progress(job, final_summary)
        job_store.add_progress(job, f"Agent 结束: {final_summary}")
        return final_summary, typ

    # 未触发重写（理论上不会到这里，但兜底）
    restore_good_snapshot(job_dir, suffix=".fixer_bak")
    final_summary = f"Coder + Fixer 用尽 {max_fixer_attempts} 轮，快速自检仍失败: {self_check_result[:500]}"
    job_store.add_progress(job, final_summary)
    job_store.add_progress(job, f"Agent 结束: {final_summary}")
    return final_summary, typ
