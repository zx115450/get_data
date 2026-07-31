"""同题复用：扫描、校验、产物复制。"""
import hashlib
import json
import shutil
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from server import job_store
from server.runners.snapshot import (
    detect_artifacts,
    detect_good_artifacts,
)

JOBS_DIR = Path("jobs")


def text_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def load_parent_failure_context(parent_dir: Path) -> dict[str, Any] | None:
    """读取父任务的 failure_context.json，若不存在或非法则返回 None。"""
    p = parent_dir / "failure_context.json"
    if not p.is_file():
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or data.get("v") != 1:
            return None
        return data
    except Exception:
        return None


def find_matching_parent_job(statement_hash: str, std_hash: str, lang: str, lookback: int = 3) -> tuple[Path, dict[str, Any]] | None:
    """扫描 jobs/ 下最近 lookback 个有 failure_context 的任务，找同题。"""
    if not JOBS_DIR.is_dir():
        return None
    dirs = [
        d for d in JOBS_DIR.iterdir()
        if d.is_dir() and (d / "failure_context.json").is_file()
    ]
    # 目录名是时间戳，排序后取最近 lookback 个
    dirs.sort(key=lambda d: d.name, reverse=True)
    for d in dirs[:lookback]:
        ctx = load_parent_failure_context(d)
        if not ctx:
            continue
        if (
            ctx.get("statement_hash") == statement_hash
            and ctx.get("std_hash") == std_hash
            and ctx.get("lang") == lang
        ):
            return d, ctx
    return None


def build_resume_failure_block(resume_info: dict[str, Any]) -> str:
    """把失败上下文格式化成一段 prompt 追加语。"""
    stage = resume_info.get("stage", "unknown")
    error_summary = resume_info.get("error_summary", "")
    parent_id = resume_info.get("parent_job_id", "")
    return (
        "\n\n【从上次失败续跑】\n"
        f"- 父任务: {parent_id}\n"
        f"- 失败阶段: {stage}\n"
        f"- 错误摘要: {error_summary}\n"
        "- 工作目录已复制上次产物，请 read_file 查看；"
        "请针对性修复导致失败的问题，不要从零重写。\n"
    )


def copy_resume_artifacts(parent_dir: Path, job_dir: Path, resume_info: dict | None = None) -> list[str]:
    """把父任务的可复用产物拷到当前目录。返回实际拷贝的列表。"""
    copied = []
    for name in detect_good_artifacts(parent_dir):
        src = parent_dir / name
        dst = job_dir / name
        if src.is_dir():
            if dst.exists():
                shutil.rmtree(dst)
            shutil.copytree(src, dst)
        else:
            shutil.copy2(src, dst)
        copied.append(name)
    for name in detect_artifacts(parent_dir):
        if name in copied:
            continue
        src = parent_dir / name
        dst = job_dir / name
        shutil.copy2(src, dst)
        copied.append(name)
    if (parent_dir / "statement_simplified.txt").is_file():
        shutil.copy2(
            parent_dir / "statement_simplified.txt",
            job_dir / "statement_simplified.txt",
        )
        copied.append("statement_simplified.txt")
    return copied


def try_resume(
    job: job_store.Job,
    job_dir: Path,
    problem_statement: str,
    std_code: str,
    lang: str,
    lookback: int = 3,
) -> tuple[dict[str, Any] | None, str]:
    """尝试自动复用最近同题任务。返回 (resume_info, resume_failure_block)。"""
    stmt_hash = text_hash(problem_statement or "")
    std_hash = text_hash(std_code or "")
    auto_parent = find_matching_parent_job(stmt_hash, std_hash, lang, lookback=lookback)
    if auto_parent is None:
        job_store.add_progress(job, "【复用】未找到同题历史任务，按新任务执行。")
        return None, ""

    parent_dir, parent_ctx = auto_parent
    parent_id = parent_dir.name
    if not parent_dir.is_dir():
        job_store.add_progress(
            job,
            f"【复用】父任务目录 {parent_dir} 不存在，按新任务执行。"
        )
        return None, ""

    copied = copy_resume_artifacts(parent_dir, job_dir, parent_ctx)
    job_store.add_progress(
        job,
        f"【复用】同题校验通过（父任务 {parent_id}），复制 {len(copied)} 项产物: {copied}"
    )
    resume_info = dict(parent_ctx)
    resume_info["parent_job_id"] = parent_id
    return resume_info, build_resume_failure_block(resume_info)
