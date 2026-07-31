"""Checker / Batch Fixer 阶段。"""
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from agent import prompts, tools
from agent.core import run as agent_run
from agent.tools import BUILTIN_CHECKERS, use_builtin_checker
from server import job_store
from server.runners.prompts import (
    build_batch_fixer_task,
    build_checker_task,
)
from server.runners.snapshot import (
    restore_good_snapshot,
    save_good_snapshot,
)

CHECKER_TOOL_SCHEMAS = [
    s for s in tools.TOOL_SCHEMAS
    if s["function"]["name"] in {
        "write_checker", "use_checker_template", "read_file",
        "run_checker", "run_checker_self_check", "finish",
    }
]
BATCH_FIXER_TOOL_SCHEMAS = [
    s for s in tools.TOOL_SCHEMAS
    if s["function"]["name"] in {
        "read_file", "write_gen", "write_validate", "run_gen", "run_validate", "run_std",
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
) -> tuple[bool, str]:
    """根据批量生成阶段的失败报告，启动 Fixer Agent 修复 gen/validator，并强制自检。"""
    tools.set_context(str(job_dir), produced.get("std_cmd", ""))
    # 用 .fixer_bak 保存基线，不污染 .good 快照
    save_good_snapshot(job_dir, include_in_out=False, suffix=".fixer_bak")
    job_store.add_progress(job, f"【批量修复 {attempt}/{max_attempts}】保存 gen/validator .fixer_bak 基线，启动 Fixer Agent")

    task = build_batch_fixer_task(stmt_plain, range_plain, produced, failures, attempt, max_attempts)
    try:
        fixer_summary = agent_run(
            task,
            max_steps=12,
            verbose=False,
            on_event=on_event,
            system_prompt=prompts.build_fixer_prompt(),
            tool_schemas=BATCH_FIXER_TOOL_SCHEMAS,
            write_check_discipline=True,
            self_check_fast=True,
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
        job_store.add_progress(job, f"Batch Fixer 修改后强制自检：重新跑 run_self_check(fast_mode={use_fast})")
        self_check_result = tools.run_self_check(fast_mode=use_fast)
        if isinstance(self_check_result, str) and self_check_result.startswith("ERROR"):
            # 回退到 .fixer_bak 基线，不破坏 .good 快照
            restore_good_snapshot(job_dir, suffix=".fixer_bak")
            job_store.add_progress(job, "Batch Fixer 自检失败，已回退到 gen/validator .fixer_bak 基线")
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
    """Special Judge 阶段：安装内置或运行 Agent 写 checker。"""
    if not special_judge:
        return
    job_store.add_progress(job, "【阶段 3.5/5】生成 checker")
    builtin_checker = (builtin_checker or "").strip().lower()
    if builtin_checker and builtin_checker not in BUILTIN_CHECKERS:
        raise ValueError(f"builtin_checker 只支持: {', '.join(BUILTIN_CHECKERS)}")

    if builtin_checker:
        tools.set_context(str(job_dir), produced["std_cmd"])
        msg = use_builtin_checker(builtin_checker)
        job_store.add_progress(job, f"安装内置 checker: {msg}")
    else:
        checker_task = build_checker_task(
            stmt_plain, range_plain, output_plain, std_for_prompt, produced,
            failure_context=resume_failure_block,
        )
        checker_summary = agent_run(
            checker_task,
            max_steps=25,
            verbose=False,
            on_event=on_event,
            system_prompt=prompts.build_checker_prompt(),
            tool_schemas=CHECKER_TOOL_SCHEMAS,
        )
        job_store.add_progress(job, f"Checker Agent 结束: {checker_summary}")

    _exe = lambda b: b + (".exe" if Path().name == "nt" else "")
    has_checker = (job_dir / _exe("checker")).exists() or (job_dir / "checker.cpp").exists()
    if not has_checker:
        raise RuntimeError(
            "已开启 special judge，但 checker 生成失败"
            "（未指定内置 checker，且自定义 checker 未能产出）"
        )
    job_store.add_progress(job, "checker 产物 OK")
