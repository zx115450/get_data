"""单个任务的执行编排：薄壳，具体逻辑拆到 server/runners/ 下。"""
import json
import os
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent import prompts, tools
from server import job_store
from server.few_shots import normalize_problem_type
from server.runners import agent, batch, failure, resume, review, scaffold, snapshot
from server.runners.snapshot import has_gen_val_at_resume
from utils.markup import to_plain_for_llm

JOBS_DIR = Path("jobs")


def _exe(base: str) -> str:
    return base + (".exe" if os.name == "nt" else "")


def _run_job_impl(
    job: job_store.Job, std_code: str, lang: str,
    problem_statement: str, data_range_desc: str,
    problem_type: str,
    output_desc: str,
    range_json,
    special_judge: bool,
    builtin_checker: str,
    resume_context: dict[str, Any] | None,
) -> None:
    """run_job 的实际实现：按阶段调用 runners 子模块。"""
    builtin_checker = (builtin_checker or "").strip().lower()
    if builtin_checker and builtin_checker not in tools.BUILTIN_CHECKERS:
        raise ValueError(
            f"builtin_checker 只支持: {', '.join(tools.BUILTIN_CHECKERS)}，收到 {builtin_checker!r}"
        )

    job_dir = scaffold.setup_job_dir(job.id)
    out_dir = job_dir / "out"
    zip_path = job_dir / "data.zip"
    sources_zip_path = job_dir / "sources.zip"
    checker_zip_path = job_dir / "checker.zip"
    range_file = job_dir / "range.json"

    def on_event(step, name, args, preview):
        brief = {}
        if name == "run_gen":
            brief = {"seed": args.get("seed"), "type": args.get("type")}
        elif name in ("write_gen", "write_validate", "write_range", "write_checker"):
            content = args.get("content") or ""
            brief = {"chars": len(content)}
        elif name == "use_builtin_checker":
            brief = {"name": args.get("name")}
        elif name in ("run_validate", "run_std"):
            t = args.get("input_text") or ""
            brief = {"input_chars": len(t)}
        elif name == "finish":
            brief = {"summary": (args.get("summary") or "")[:80]}
        else:
            brief = {k: (str(v)[:40] if not isinstance(v, (int, float, bool)) else v)
                     for k, v in list(args.items())[:4]}
        pv = preview if len(preview) <= 100 else preview[:100] + "…"
        job_store.add_progress(job, f"[step {step}] {name} {brief} -> {pv}")

    # 1.0) 同题复用：只扫描最近 3 个 job
    resume_info, resume_failure_block = resume.try_resume(
        job, job_dir, problem_statement, std_code, lang, lookback=3
    )
    if resume_context:
        job_store.add_progress(
            job,
            f"【复用】收到客户端 resume_context（已废弃，仅记录），父任务 {resume_context.get('parent_job_id')!r}"
        )

    # 1) 准备标程
    job_store.add_progress(job, f"【阶段 1/5】准备标程 (lang={lang})")
    std_cmd = scaffold.compile_std(job_dir, std_code, lang)
    job_store.add_progress(job, f"标程就绪: {std_cmd}")
    if lang == "cpp" and os.name == "nt":
        job_store.add_progress(job, "标程编译已加大栈(16MB)")

    # 统一设置工具上下文，避免后续 Agent / 自检读写错目录
    tools.set_context(str(job_dir), std_cmd)

    # 1.5) 校验 sandbox 公共头文件（编译用 -I，不向 job 拷贝）
    try:
        tools.prewarm_generator_headers()
        job_store.add_progress(
            job,
            "头文件就绪: sandbox/testlib.h + generator.h（编译 -I，不落盘到 job）",
        )
    except Exception as e:
        job_store.add_progress(job, f"校验 sandbox 头文件失败: {e}")

    # 2) 准备 Prompts 并启动 Agent
    # 题型优先级：用户显式 > range.json.problem_type；仍空则在 Range 阶段单独 LLM 判定
    range_plain = to_plain_for_llm(data_range_desc or "")
    eff_type = normalize_problem_type(problem_type)
    if not eff_type and isinstance(range_json, dict):
        eff_type = normalize_problem_type(str(range_json.get("problem_type") or ""))
    type_note = "用户指定" if normalize_problem_type(problem_type) else (
        "来自 range.json" if eff_type else "待 Range 阶段 LLM 判定"
    )
    job_store.add_progress(
        job,
        f"题型: {eff_type or '(未定)'} ({type_note})"
        + f" | 题面 {len(to_plain_for_llm(problem_statement or ''))} 字"
        + f" | 范围描述 {len(range_plain)} 字 | std {len(std_code)} 字",
    )
    stmt_plain, task, preset, output_plain, std_for_prompt = agent.prepare_prompts(
        problem_statement, data_range_desc, output_desc, std_code, lang,
        eff_type, eff_type, range_json, resume_failure_block, job_dir, job,
    )
    summary, eff_type = agent.run_gen_agent(
        job, job_dir, task, eff_type, range_json or preset, resume_info, on_event,
        stmt_plain=stmt_plain, std_code=std_code, resume_failure_block=resume_failure_block,
    )
    scaffold.ensure_agent_log(job, job_dir)

    # 3) 校验产物
    job_store.add_progress(job, "【阶段 3/5】校验产物 (range.json / gen / validator)")
    if not range_file.exists():
        raise RuntimeError("Agent 没有产出 range.json")
    from pipeline.gen_data import normalize_range_json, validate_range_json
    try:
        produced = json.loads(range_file.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise RuntimeError(f"Agent 产出的 range.json 不是合法 JSON: {e}")
    if preset is not None:
        produced = dict(preset)
        # 保留流水线判定的题型（GUI 方案可能未带 problem_type）
        if eff_type and not produced.get("problem_type"):
            produced["problem_type"] = eff_type
        range_file.write_text(json.dumps(produced, ensure_ascii=False, indent=2), encoding="utf-8")
    if eff_type:
        produced["problem_type"] = produced.get("problem_type") or eff_type
    produced["std_cmd"] = std_cmd
    produced = normalize_range_json(produced)
    errs = validate_range_json(produced)
    if errs:
        raise RuntimeError("range.json 不合法:\n" + "\n".join(f"  - {e}" for e in errs))

    sp_constraints = produced.get("special_constraints") or []
    if sp_constraints:
        job_store.add_progress(job, f"题目特殊结构约束: {sp_constraints}")

    has_gen = (job_dir / _exe("gen")).exists() or (job_dir / "gen.py").exists() or (job_dir / "gen.cpp").exists()
    has_val = (job_dir / _exe("validator")).exists() or (job_dir / "validate.py").exists() or (job_dir / "validator.cpp").exists()
    if not has_gen or not has_val:
        with job.lock:
            tail = list(job.progress)[-8:]
        missing = []
        if not has_gen:
            missing.append("gen")
        if not has_val:
            missing.append("validator")
        raise RuntimeError(
            f"Agent 没有产出 {' / '.join(missing)}。\n"
            f"Agent summary: {summary!r}\n"
            f"最后几条进度:\n" + "\n".join(f"  {t}" for t in tail)
            + "\n常见原因：LLM 未调工具就返回文本（被当成 finish）、"
            f"Gen Agent 步数预算用尽、或 write_gen/write_validate 编译失败循环。"
            f"完整日志见 {job_dir / 'agent_log.txt'}。"
        )

    # 3.5) Checker
    review.run_checker(
        job, job_dir, special_judge, builtin_checker, stmt_plain,
        range_plain, output_plain, std_for_prompt, produced,
        resume_failure_block, on_event,
    )

    # 4/5) 批量生成与打包
    batch.run_batch_and_pack(
        job, job_dir, produced, eff_type, special_judge,
        stmt_plain=stmt_plain,
        range_plain=range_plain,
        on_event=on_event,
    )


def run_job(job: job_store.Job, std_code: str, lang: str,
            problem_statement: str, data_range_desc: str,
            problem_type: str = "",
            output_desc: str = "",
            range_json=None,
            special_judge: bool = False,
            builtin_checker: str = "",
            resume_context: dict[str, Any] | None = None) -> None:
    """在 worker 线程里跑完整流程。

    resume_context: 已废弃，保留仅作兼容；现在 runner 会自动扫描 jobs/ 下最近同题任务。
    """
    job_dir = JOBS_DIR / job.id
    range_file = job_dir / "range.json"
    cancelled = False
    try:
        _run_job_impl(
            job, std_code, lang,
            problem_statement, data_range_desc,
            problem_type,
            output_desc,
            range_json,
            special_judge,
            builtin_checker,
            resume_context,
        )
    except (Exception, KeyboardInterrupt, SystemExit) as e:
        cancelled = isinstance(e, (KeyboardInterrupt, SystemExit)) or "客户端请求取消" in str(e)
        stage = failure.infer_failure_stage(list(job.progress))
        review_report = ""
        try:
            p = job_dir / "review_report.txt"
            if p.is_file():
                review_report = p.read_text(encoding="utf-8")
        except Exception:
            pass
        failure.write_failure_context(
            job_dir, job.id, stage, f"{type(e).__name__}: {e}",
            problem_statement, std_code, lang, range_file,
            review_report=review_report,
        )
        if cancelled:
            job_store.add_progress(job, "任务已取消，但 failure_context 已落盘，可同题续跑")
            job_store.add_progress(job, f"取消原因: {e}")
        raise
    finally:
        if cancelled and job.status.value != "error":
            with job.lock:
                job.status = job_store.JobStatus.CANCELLED
