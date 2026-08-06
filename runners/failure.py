"""失败阶段推断与 failure_context 落盘。"""
import hashlib
import json
from datetime import datetime
from pathlib import Path
from typing import Any

from runners.resume import RESUME_VERSION, text_hash
from runners.snapshot import detect_artifacts, detect_good_artifacts


def infer_failure_stage(progress: list[str]) -> str:
    """根据进度日志推断失败阶段。"""
    for msg in reversed(progress):
        if "阶段 4/5" in msg or "批量生成" in msg:
            return "batch_generate"
        if "阶段 3.5/5" in msg or "生成 checker" in msg:
            return "checker"
        if "阶段 3/5" in msg or "校验产物" in msg:
            return "validate"
        if "阶段 2/5" in msg or "启动 Agent" in msg:
            return "agent"
        if "阶段 1/5" in msg or "准备标程" in msg:
            return "std_compile"
    return "unknown"


def extract_review_issues(review_summary: str) -> tuple[bool, str]:
    """解析 Reviewer 报告，返回 (是否有 MUST_FIX, 问题摘要)。"""
    text = (review_summary or "").lower()
    has_must_fix = "must_fix" in text or "must fix" in text or "必须修" in text
    lines = [ln.strip() for ln in (review_summary or "").splitlines() if ln.strip()]
    issue_lines = [ln for ln in lines if any(k in ln.lower() for k in ("must_fix", "must fix", "should_fix", "should fix", "严重", "超时", "性能问题"))]
    summary = "\n".join(issue_lines[:10]) or ("MUST_FIX" if has_must_fix else "")
    return has_must_fix, summary


def write_failure_context(
    job_dir: Path,
    job_id: str,
    stage: str,
    error: str,
    statement: str,
    std_code: str,
    lang: str,
    range_file: Path | None,
    review_report: str = "",
) -> None:
    """失败时把可携带的上下文写入 failure_context.json。"""
    artifacts = detect_artifacts(job_dir) + detect_good_artifacts(job_dir)
    seen = set()
    artifacts = [a for a in artifacts if not (a in seen or seen.add(a))]
    ctx = {
        "v": 1,
        "resume_version": RESUME_VERSION,
        "parent_job_id": job_id,
        "stage": stage,
        "statement_hash": text_hash(statement or ""),
        "std_hash": text_hash(std_code or ""),
        "lang": lang,
        "range_hash": "",
        "artifacts": artifacts,
        "error_summary": (error or "")[:1000],
        "review_report": (review_report or "")[:2000],
        "created_at": datetime.now().isoformat(),
    }
    if range_file and range_file.is_file():
        try:
            ctx["range_hash"] = text_hash(range_file.read_text(encoding="utf-8"))
        except Exception:
            pass

    # 完整错误正文单独落盘（JSON 里只留摘要）
    err_rel = ""
    try:
        from agent.errors import persist_error

        err_rel = persist_error(
            job_dir, error or "", kind="job_error", tool="job"
        )
        if err_rel:
            ctx["error_file"] = err_rel
    except Exception:
        try:
            err_path = job_dir / "errors" / "job_error.txt"
            err_path.parent.mkdir(parents=True, exist_ok=True)
            err_path.write_text(error or "", encoding="utf-8", errors="replace")
            ctx["error_file"] = "errors/job_error.txt"
        except Exception:
            pass

    try:
        (job_dir / "failure_context.json").write_text(
            json.dumps(ctx, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except Exception:
        pass
