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
from server.runners.prompts import build_std_block
from server.runners.snapshot import (
    has_gen_val_at_resume,
    restore_good_snapshot,
)
from server.struct_hints import scan_structural_hints
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
RANGE_TOOL_SCHEMAS = [
    s for s in tools.TOOL_SCHEMAS
    if s["function"]["name"] in {"write_range", "read_file", "read_range", "finish"}
]

PLAN_FILE = "gen_plan.md"


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

    few_shot_block, few_shot_summary = get_few_shot_rag(
        problem_type, stmt_plain, range_plain, std_code, top_k=2
    )
    if few_shot_summary.startswith("RAG 召回"):
        short = few_shot_summary.replace("RAG 召回 2 个模板: ", "")
        job_store.add_progress(job, f"few-shot RAG: {short}")
    else:
        job_store.add_progress(job, f"few-shot: {few_shot_summary}")
    if few_shot_block:
        few_shot_block = (
            f"\n\n{few_shot_block}\n\n"
            "请参照上面 RAG 召回范例的写法风格（校验严格度、--type 分支方式），"
            "为本次题目写 gen.cpp 和 validator.cpp。"
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
) -> str:
    """启动 Range/Gen Agent。Plan-and-Execute：先写 gen_plan.md，再按 plan 写代码。返回 Agent summary。"""
    full_prompt = prompts.build_full_prompt(
        eff_type,
        range_json=range_json,
        problem_statement=stmt_plain,
        std_code=std_code,
    )
    resume_stage = resume_info.get("stage") if resume_info else None
    if resume_stage == "checker" and (job_dir / "range.json").is_file() and has_gen_val_at_resume(job_dir):
        job_store.add_progress(job, "【续跑】父任务 checker 阶段失败，跳过 gen/validator Agent")
        return "checker 阶段续跑：跳过 gen/validator Agent"

    if resume_info and (job_dir / "range.json").is_file():
        stage_prompts = {"gen": full_prompt}
        stage_tool_schemas = {"gen": GEN_TOOL_SCHEMAS}
        job_store.add_progress(job, "【续跑】已有 range.json，直接进入 gen/validator 修复阶段")
    elif range_json is not None:
        stage_prompts = {"gen": full_prompt}
        stage_tool_schemas = {"gen": GEN_TOOL_SCHEMAS}
    else:
        stage_prompts = {
            "range": prompts.build_range_prompt(),
            "gen": full_prompt,
        }
        stage_tool_schemas = {
            "range": RANGE_TOOL_SCHEMAS,
            "gen": GEN_TOOL_SCHEMAS,
        }
    job_store.add_progress(
        job,
        f"Agent prompt stages: {list(stage_prompts.keys())} | type={eff_type}"
    )

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
            task.split("【数据范围描述】\n")[1].split("\n\n【")[0] if "【数据范围描述】" in task else "",
            "", "", range_json or {}, eff_type,
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

    # ---- Execute 阶段：Coder 硬自检循环（写 → 自检 → 限轮修复）----
    max_coder_attempts = 2
    coder_summary = ""
    for attempt in range(1, max_coder_attempts + 1):
        job_store.add_progress(job, f"【Execute】Coder 第 {attempt}/{max_coder_attempts} 轮：写 gen/validator")
        coder_task = _build_coder_task(
            stmt_plain,
            task.split("【数据范围描述】\n")[1].split("\n\n【")[0] if "【数据范围描述】" in task else "",
            "", "", range_json or {}, eff_type,
            resume_failure_block if attempt == 1 else "",
        )
        # 第一轮用原始 task 补充上下文；第二轮只给失败摘要
        if attempt == 1:
            coder_task = f"{task}\n\n【额外要求：Plan-and-Execute】\n{coder_task}"

        coder_summary = agent_run(
            coder_task,
            max_steps=15,
            verbose=False,
            on_event=on_event,
            system_prompt=prompts.build_coder_prompt(),
            tool_schemas=CODER_TOOL_SCHEMAS,
        )
        job_store.add_progress(job, f"Coder 第 {attempt} 轮结束: {coder_summary}")

        # 真跑一遍自检，不依赖模型是否记得调用
        job_store.add_progress(job, "【Execute】强制跑 run_self_check 验证 Coder 产物")
        self_check_result = tools.run_self_check()
        job_store.add_progress(job, f"自检结果: {self_check_result[:500]}")

        if isinstance(self_check_result, str) and self_check_result.startswith("OK"):
            coder_summary = f"Coder 自检通过 ({attempt}/{max_coder_attempts})"
            job_store.add_progress(job, coder_summary)
            break

        if attempt >= max_coder_attempts:
            coder_summary = f"Coder 用尽 {max_coder_attempts} 轮，自检仍失败: {self_check_result[:500]}"
            job_store.add_progress(job, coder_summary)
            break

        job_store.add_progress(job, f"【Execute】自检失败，进入第 {attempt + 1} 轮修复")
        # 下一轮把失败摘要喂给 Coder，让它读文件后再写一次
        resume_failure_block = (
            "\n\n【上一轮自检失败】\n"
            f"{self_check_result[:1500]}\n"
            "请重新读取 gen.cpp 和 validator.cpp，针对失败原因修复后再次 write_gen + write_validate，然后 run_self_check。"
        )

    job_store.add_progress(job, f"Agent 结束: {coder_summary}")
    return coder_summary
