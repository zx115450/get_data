"""Checker / Batch Fixer 阶段。"""
import os
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from agent import prompts, tools
from agent.core import run as agent_run
from agent.llm import chat_text
from agent.tools import BUILTIN_CHECKERS, use_builtin_checker
from server import job_store
from server.runners.prompts import (
    CHECKER_PLAN_SOFT_MAX_CHARS,
    CHECKER_PLAN_TARGET_CHARS,
    build_batch_fixer_task,
    build_checker_coder_task,
    build_checker_planner_user,
    looks_like_unique_token_answer,
    parse_builtin_checker_from_plan,
)
from server.runners.snapshot import (
    restore_good_snapshot,
    save_good_snapshot,
)

CHECKER_PLAN_FILE = "checker_plan.md"

CHECKER_TOOL_SCHEMAS = [
    s for s in tools.TOOL_SCHEMAS
    if s["function"]["name"] in {
        "write_checker", "use_checker_template", "read_file",
        "run_checker", "run_checker_self_check", "finish",
    }
]


def _checker_plan_looks_complete(plan_text: str) -> bool:
    """粗略检查 checker plan 是否含 7 节、模板、实现思路、复杂度预算与自检信号。"""
    if not plan_text or len(plan_text) < 80:
        return False
    text = plan_text.lower()
    if not all(h in text for h in ("1.", "2.", "3.", "4.", "5.", "6.", "7.")):
        return False
    has_template = any(
        name in text
        for name in (
            "construct_verify", "any_of_answers", "graph_path", "permutation",
            "subset", "sequence_property", "point_set", "matching", "tree_parent",
            "lcmp", "wcmp", "rcmp", "yesno", "内置",
        )
    )
    has_verdict = ("_ok" in text) or ("_wa" in text) or ("_pe" in text) or ("自检" in plan_text)
    has_impl = (
        ("实现思路" in plan_text)
        or ("实现步骤" in plan_text)
        or ("执行顺序" in plan_text)
        or (plan_text.count("-") >= 4)  # 步骤/条件条目
    )
    # SPJ 复杂度预算：须出现 O(...) 或「复杂度」字样（内置 checker 计划可极简豁免）
    builtin_only = any(
        k in text for k in ("内置 checker", "builtin", "lcmp", "wcmp", "rcmp", "yesno")
    ) and ("无需自定义" in plan_text or "使用内置" in plan_text or "应使用内置" in plan_text)
    has_complexity = (
        builtin_only
        or ("复杂度" in plan_text)
        or ("o(" in text)
        or ("ο(" in text)  # 偶发希腊字母
    )
    return has_template and has_verdict and has_impl and has_complexity


BATCH_FIXER_TOOL_SCHEMAS = [
    s for s in tools.TOOL_SCHEMAS
    if s["function"]["name"] in {
        "read_file", "write_gen", "write_validate", "run_gen", "run_validate", "run_std",
        "run_self_check", "finish",
    }
]
BATCH_SPECIAL_FIXER_TOOL_SCHEMAS = [
    s for s in tools.TOOL_SCHEMAS
    if s["function"]["name"] in {
        "read_file", "write_special_gen", "write_special_check",
        "run_gen", "run_validate", "run_property_check", "run_std",
        "run_self_check", "finish",
    }
]


def run_batch_failure_fix(
    job: job_store.Job,
    job_dir: Path,
    stmt_plain: str,
    range_plain: str,
    produced: dict,
    failures: list[dict],
    on_event,
    attempt: int = 1,
    max_attempts: int = 2,
    hard_self_check: bool = True,
    special_failures_only: bool = False,
) -> tuple[bool, str]:
    """根据批量生成阶段的失败报告，启动 Fixer Agent 修复 gen/validator（或 gen_special），并强制自检。"""
    tools.set_context(str(job_dir), produced.get("std_cmd", ""))
    # 用 .fixer_bak 保存基线，不污染 .good 快照
    save_good_snapshot(job_dir, include_in_out=False, suffix=".fixer_bak")

    def _is_special_fail(f: dict) -> bool:
        t = str(f.get("planned_type") or "")
        return t == "special_samples" or t.startswith("special:")

    all_special = bool(failures) and all(_is_special_fail(f) for f in failures)
    use_special_fixer = special_failures_only or all_special
    sc_args = {"special_only": True} if use_special_fixer else {"skip_special": True}

    job_store.add_progress(
        job,
        f"【批量修复 {attempt}/{max_attempts}】保存 .fixer_bak 基线，启动 "
        f"{'Special' if use_special_fixer else 'Gen'} Batch Fixer",
    )

    task = build_batch_fixer_task(
        stmt_plain, range_plain, produced, failures, attempt, max_attempts,
        special_only=use_special_fixer,
    )
    try:
        fixer_summary = agent_run(
            task,
            max_steps=12,
            verbose=False,
            on_event=on_event,
            system_prompt=(
                prompts.build_special_fixer_prompt()
                if use_special_fixer
                else prompts.build_gen_fixer_prompt()
            ),
            tool_schemas=(
                BATCH_SPECIAL_FIXER_TOOL_SCHEMAS
                if use_special_fixer
                else BATCH_FIXER_TOOL_SCHEMAS
            ),
            write_check_discipline=True,
            self_check_fast=True,
            self_check_args=sc_args,
        )
        job_store.add_progress(job, f"Batch Fixer 结束: {fixer_summary}")
    except Exception as e:
        job_store.add_progress(job, f"Batch Fixer 调用失败: {type(e).__name__}: {e}")
        return False, f"Batch Fixer 调用失败: {e}"

    try:
        (job_dir / "batch_fix_report.txt").write_text(str(fixer_summary), encoding="utf-8")
    except Exception as e:
        job_store.add_progress(job, f"batch_fix_report.txt 写盘失败（非致命）: {type(e).__name__}: {e}")

    if hard_self_check:
        # 批量修复中间轮次用快速自检，最后一轮用完整模式
        use_fast = attempt < max_attempts
        job_store.add_progress(
            job,
            f"Batch Fixer 修改后强制自检：run_self_check(fast_mode={use_fast}, {sc_args})",
        )
        self_check_result = tools.run_self_check(fast_mode=use_fast, **sc_args)
        if isinstance(self_check_result, str) and self_check_result.startswith("ERROR"):
            # 回退到 .fixer_bak 基线，不破坏 .good 快照
            restore_good_snapshot(job_dir, suffix=".fixer_bak")
            job_store.add_progress(job, "Batch Fixer 自检失败，已回退到 .fixer_bak 基线")
            return False, f"Batch Fixer 自检失败: {self_check_result}"
        job_store.add_progress(job, "Batch Fixer 自检通过")
    return True, "Batch Fixer 自检通过"


def run_checker(
    job: job_store.Job,
    job_dir: Path,
    special_judge: bool,
    builtin_checker: str,
    stmt_plain: str,
    range_plain: str,
    output_plain: str,
    std_for_prompt: str,
    produced: dict,
    resume_failure_block: str,
    on_event,
) -> None:
    """Special Judge 阶段：内置直接安装；自定义走 Plan + Coder。"""
    if not special_judge:
        return
    job_store.add_progress(job, "【阶段 3.5/5】生成 checker")
    builtin_checker = (builtin_checker or "").strip().lower()
    if builtin_checker and builtin_checker not in BUILTIN_CHECKERS:
        raise ValueError(f"builtin_checker 只支持: {', '.join(BUILTIN_CHECKERS)}")

    tools.set_context(str(job_dir), produced["std_cmd"])

    # 未指定内置且像唯一 token 答案 → 默认 wcmp，跳过自定义 SPJ
    if not builtin_checker and looks_like_unique_token_answer(
        stmt_plain, output_plain, std_for_prompt,
    ):
        builtin_checker = "wcmp"
        job_store.add_progress(
            job,
            "【Checker】判定为唯一 token 答案，默认安装内置 wcmp（跳过自定义 SPJ）",
        )

    if builtin_checker:
        msg = use_builtin_checker(builtin_checker)
        job_store.add_progress(job, f"安装内置 checker: {msg}")
    else:
        plan_path = job_dir / CHECKER_PLAN_FILE
        plan_text = ""
        if plan_path.is_file():
            job_store.add_progress(job, "检测到已有 checker_plan.md，跳过 Checker Plan")
            try:
                plan_text = plan_path.read_text(encoding="utf-8")
            except OSError:
                plan_text = ""
        else:
            system_prompt = prompts.build_checker_planner_prompt()
            user_prompt = build_checker_planner_user(
                stmt_plain, range_plain, output_plain, std_for_prompt, produced,
            )
            job_store.add_progress(job, "【Checker Plan】启动 Planner 生成 checker_plan.md")
            try:
                plan_text = chat_text(system_prompt, user_prompt, temperature=0.3)
            except Exception as e:
                plan_text = ""
                job_store.add_progress(
                    job, f"【Checker Plan】调用失败: {type(e).__name__}: {e}",
                )

            if not _checker_plan_looks_complete(plan_text):
                job_store.add_progress(job, "【Checker Plan】首次不完整，补一次")
                retry_prompt = (
                    f"{user_prompt}\n\n"
                    "【上一次计划被判定为不完整】请严格按 7 个小节重写，保持简短"
                    f"（目标约 {CHECKER_PLAN_TARGET_CHARS} 字）：\n"
                    "1. 判定类型 2. 模板选型（写死一个名）3. 读入顺序 "
                    "4. 合法条件清单 5. 与 ans 的关系 "
                    "6. 实现思路（含复杂度预算：O(...) · N=… · 预计≤1s）"
                    "7. 错误码与自检"
                )
                try:
                    plan_text = chat_text(system_prompt, retry_prompt, temperature=0.3)
                except Exception as e:
                    job_store.add_progress(
                        job, f"【Checker Plan】补写失败: {type(e).__name__}: {e}",
                    )

            if plan_text and len(plan_text) > CHECKER_PLAN_SOFT_MAX_CHARS:
                job_store.add_progress(
                    job,
                    f"【Checker Plan】过长 ({len(plan_text)}>{CHECKER_PLAN_SOFT_MAX_CHARS})，压缩一次",
                )
                compress_prompt = (
                    "请压缩下面的 checker_plan，保留全部 7 节、模板名、合法条件清单、"
                    "实现思路步骤与「复杂度预算」一行，"
                    f"约 {CHECKER_PLAN_TARGET_CHARS} 字以内。只输出压缩后的计划。\n\n"
                    f"【原计划】\n{plan_text}"
                )
                try:
                    compressed = chat_text(system_prompt, compress_prompt, temperature=0.2)
                    if _checker_plan_looks_complete(compressed) and len(compressed) < len(plan_text):
                        plan_text = compressed
                except Exception as e:
                    job_store.add_progress(
                        job, f"【Checker Plan】压缩失败（沿用原计划）: {type(e).__name__}: {e}",
                    )

            if plan_text.strip():
                plan_path.write_text(plan_text, encoding="utf-8")
                job_store.add_progress(
                    job, f"【Checker Plan】已生成 checker_plan.md ({len(plan_text)} 字符)",
                )
            else:
                job_store.add_progress(
                    job, "【Checker Plan】未生成计划，Coder 将仅靠冲突对照摘要实现",
                )

        # Plan 声明内置 checker → 直接安装，不跑自定义 Coder
        plan_builtin = parse_builtin_checker_from_plan(plan_text)
        if plan_builtin and plan_builtin in BUILTIN_CHECKERS:
            msg = use_builtin_checker(plan_builtin)
            job_store.add_progress(
                job, f"【Checker Plan】声明内置 {plan_builtin}，安装: {msg}",
            )
        else:
            job_store.add_progress(job, "【Checker Coder】按 checker_plan.md 实现 checker.cpp")
            checker_task = build_checker_coder_task(
                stmt_plain, range_plain, output_plain, std_for_prompt, produced,
                failure_context=resume_failure_block,
            )
            checker_summary = agent_run(
                checker_task,
                max_steps=25,
                verbose=False,
                on_event=on_event,
                system_prompt=prompts.build_checker_coder_prompt(),
                tool_schemas=CHECKER_TOOL_SCHEMAS,
                checker_write_discipline=True,
                tool_limits={"write_checker": 2},
            )
            job_store.add_progress(job, f"Checker Coder 结束: {checker_summary}")

    checker_exe = "checker.exe" if os.name == "nt" else "checker"
    has_checker = (job_dir / checker_exe).exists() or (job_dir / "checker.cpp").exists()
    if not has_checker:
        raise RuntimeError(
            "已开启 special judge，但 checker 生成失败"
            "（未指定内置 checker，且自定义 checker 未能产出）"
        )

    # P0：自检通过才算交付；未通过不写 verified 标记（打包阶段将跳过 checker.zip）
    verified_marker = job_dir / "checker_verified.ok"
    if verified_marker.is_file():
        try:
            verified_marker.unlink()
        except OSError:
            pass
    verify = tools.run_checker_self_check()
    preview = verify if len(verify) <= 1800 else (verify[:1400] + "\n...\n" + verify[-300:])
    if isinstance(verify, str) and verify.startswith("OK"):
        verified_marker.write_text("ok\n", encoding="utf-8")
        job_store.add_progress(job, "checker 自检通过，允许打包 checker.zip")
        job_store.add_progress(job, "checker 产物 OK")
    else:
        job_store.add_progress(
            job,
            "checker 自检未通过，跳过 checker.zip 交付（测例 data.zip 仍可打包）。\n"
            f"{preview}",
        )
        # 有源码但未验证：仍算阶段完成，但不宣称产物 OK
        job_store.add_progress(job, "checker 产物未验证（自检失败）")
