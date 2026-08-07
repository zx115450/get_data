"""单个任务的执行编排：薄壳，具体逻辑拆到 runners/ 下。"""
import json
import os
from pathlib import Path
from typing import Any

from agent import tools
from storage import job_store
from knowledge.few_shots import normalize_problem_types
from pipeline.gen_data import special_enabled
from . import agent, batch, failure, resume, review, scaffold, snapshot
from .snapshot import has_gen_val_at_resume
from .special import run_special_agent
from utils.markup import to_plain_for_llm


def _exe(base: str) -> str:
    return base + (".exe" if os.name == "nt" else "")


def _run_job_impl(
    job: job_store.Job, std_code: str, lang: str,
    problem_statement: str, data_range_desc: str,
    problem_type: str | list[str] | None,
    output_desc: str,
    range_json,
    special_judge: bool,
    builtin_checker: str,
    resume_context: dict[str, Any] | None,
    special_samples_desc: str = "",
    special_samples_count: int = 1,
    auto_discover_special: bool = False,
    skip_resume: bool = False,
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

    # 落盘题面/输入/输出（与 std 一起，关闭 GUI 或历史回载用）
    scaffold.save_problem_workspace(
        job_dir,
        std_code=std_code,
        lang=lang,
        problem_statement=problem_statement,
        data_range_desc=data_range_desc,
        output_desc=output_desc,
        problem_type=problem_type,
    )

    def on_event(step, name, args, preview):
        brief = {}
        if name == "run_gen":
            brief = {"seed": args.get("seed"), "type": args.get("type")}
        elif name in ("write_gen", "write_validate", "write_range", "write_checker",
                       "write_special_gen", "write_special_check"):
            content = (
                args.get("content") or args.get("code") or args.get("source") or ""
            )
            brief = {"chars": len(content)}
            if args.get("_args_recovered"):
                brief["recovered"] = True
            if args.get("_args_parse_failed"):
                brief["parse_failed"] = True
            if not content:
                brief["warn"] = "missing_content"
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

        from agent.errors import (
            format_error_for_progress,
            is_error_text,
            persist_error,
        )

        preview_s = preview if isinstance(preview, str) else str(preview)
        # 失败：完整落盘 errors/；进度保留长摘要并标注文件路径
        if is_error_text(preview_s):
            rel = ""
            try:
                rel = persist_error(
                    job_dir, preview_s, tool=str(name or ""), step=int(step or 0)
                )
            except Exception:
                rel = ""
            if name == "run_self_check":
                try:
                    from agent.tools import format_self_check_for_progress

                    pv = format_self_check_for_progress(preview_s)
                except Exception:
                    pv = format_error_for_progress(preview_s)
            else:
                pv = format_error_for_progress(preview_s)
            if rel:
                tip = f"（完整见 {rel} 与 errors/last_error.txt）"
                if tip not in pv:
                    pv = f"{pv}\n{tip}"
        else:
            pv = preview_s if len(preview_s) <= 100 else preview_s[:100] + "…"
        job_store.add_progress(job, f"[step {step}] {name} {brief} -> {pv}")

    # 1.0) 同题复用：客户端指定父任务优先，否则扫描最近 3 个失败 job
    prefer_parent = ""
    if isinstance(resume_context, dict):
        prefer_parent = str(
            resume_context.get("parent_job_id") or resume_context.get("parent_id") or ""
        ).strip()
    if skip_resume:
        job_store.add_progress(job, "【复用】客户端要求跳过同题复用，按新任务执行。")
        resume_info, resume_failure_block = None, ""
    else:
        resume_info, resume_failure_block = resume.try_resume(
            job, job_dir, problem_statement, std_code, lang,
            lookback=3,
            prefer_parent_id=prefer_parent or None,
        )

    # 父任务已成功：只挂 zip，整条流水线直接结束
    if isinstance(resume_info, dict) and resume_info.get("short_circuit"):
        if (job_dir / "data.zip").is_file():
            job.zip_path = str(job_dir / "data.zip")
        if (job_dir / "sources.zip").is_file():
            job.sources_zip_path = str(job_dir / "sources.zip")
        if (job_dir / "checker.zip").is_file():
            job.checker_zip_path = str(job_dir / "checker.zip")
        parent_id = resume_info.get("parent_job_id") or ""
        job_store.add_progress(
            job,
            f"【完成】已复用成功任务 {parent_id} 的数据包，未重新 Plan / 写 gen / 出数",
        )
        return

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
    # 每次开始生成都会跑 Range Agent（有方案则审核复用/重写，无则新建）；
    # 题型以 Range 审核后的 range.json.problem_type 为准。
    range_plain = to_plain_for_llm(data_range_desc or "")
    has_preset_range = isinstance(range_json, dict) and bool(range_json.get("constraints"))
    if has_preset_range:
        eff_type = normalize_problem_types(problem_type) or normalize_problem_types(
            range_json.get("problem_type")
        )
        has_user_type = bool(normalize_problem_types(problem_type))
        type_note = "候选(待 Range 审核)" + (
            "/用户指定" if has_user_type else (
                "/来自 range.json" if eff_type else ""
            )
        )
    else:
        eff_type = []
        type_note = "待 Range 写入 problem_type"
    type_label = ", ".join(eff_type) if eff_type else "(未定)"
    job_store.add_progress(
        job,
        f"题型: {type_label} ({type_note})"
        + f" | 题面 {len(to_plain_for_llm(problem_statement or ''))} 字"
        + f" | 范围描述 {len(range_plain)} 字 | std {len(std_code)} 字",
    )
    # 若用户通过 API 传了 special_samples_desc / auto_discover，但 range_json 里没写，则合并进去
    if isinstance(range_json, dict) and (
        special_samples_desc or auto_discover_special
    ):
        range_json = dict(range_json)
        if special_samples_desc:
            range_json.setdefault("special_samples_desc", special_samples_desc)
            range_json.setdefault("special_samples_count", special_samples_count)
        if auto_discover_special:
            range_json["auto_discover_special"] = True

    stmt_plain, task, preset, output_plain, std_for_prompt = agent.prepare_prompts(
        problem_statement, data_range_desc, output_desc, std_code, lang,
        eff_type, eff_type, range_json, resume_failure_block, job_dir, job,
        special_samples_desc=special_samples_desc,
        special_samples_count=special_samples_count,
        auto_discover_special=auto_discover_special,
    )
    summary, eff_type = agent.run_gen_agent(
        job, job_dir, task, eff_type, range_json or preset, resume_info, on_event,
        stmt_plain=stmt_plain, std_code=std_code, resume_failure_block=resume_failure_block,
        special_samples_desc=special_samples_desc,
        special_samples_count=special_samples_count,
        auto_discover_special=auto_discover_special,
        output_plain=output_plain,
        std_for_prompt=std_for_prompt,
        lang=lang,
    )
    # 续跑路径可能返回字符串，统一为列表便于下游处理
    if isinstance(eff_type, str):
        eff_type = normalize_problem_types(eff_type) or [eff_type]
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
    # 以 Range Agent 审核后的磁盘文件为准，不再用 GUI preset 覆盖
    if preset is not None:
        job_store.add_progress(
            job, "提示: 曾有 GUI range 候选，最终以 Range Agent 审核后的磁盘 range.json 为准",
        )
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
    # 启用特殊样例时：先生成常规数据 → Special Plan/Coder → 再补特殊组 → 打包
    special_hook = None
    if special_enabled(produced) or (special_samples_desc or "").strip():
        def special_hook():
            run_special_agent(
                job,
                job_dir,
                produced,
                eff_type,
                on_event,
                stmt_plain=stmt_plain,
                range_plain=range_plain,
                special_samples_desc=special_samples_desc or produced.get("special_samples_desc") or "",
                special_samples_count=int(
                    produced.get("special_samples_count") or special_samples_count or 1
                ),
            )

    result = batch.run_batch_and_pack(
        job, job_dir, produced, eff_type, special_judge,
        stmt_plain=stmt_plain,
        range_plain=range_plain,
        on_event=on_event,
        after_regular_hook=special_hook,
    )
    return result


def run_job(job: job_store.Job, std_code: str, lang: str,
            problem_statement: str, data_range_desc: str,
            problem_type: str | list[str] | None = "",
            output_desc: str = "",
            range_json=None,
            special_judge: bool = False,
            builtin_checker: str = "",
            resume_context: dict[str, Any] | None = None,
            special_samples_desc: str = "",
            special_samples_count: int = 1,
            auto_discover_special: bool = False,
            skip_resume: bool = False) -> dict[str, Any] | None:
    """在 worker 线程里跑完整流程。

    resume_context: 可选；含 parent_job_id 时优先复用该历史任务产物。
    skip_resume: 为 True 时强制跳过同题复用，从零重新生成。

    返回: batch.run_batch_and_pack 的 stats（若执行到该阶段）；short_circuit 时返回 resume_info。
    """
    job_dir = job_store.JOBS_DIR / job.id
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
            special_samples_desc=special_samples_desc,
            special_samples_count=special_samples_count,
            auto_discover_special=auto_discover_special,
            skip_resume=skip_resume,
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
