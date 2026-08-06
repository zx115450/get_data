"""任务存储：内存索引 + 落盘 job_status.json；单 worker 并发闸门。"""
import hashlib
import json
import os
import shutil
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path

from storage.problem_store import JOBS_DIR as _DEFAULT_JOBS_DIR, ROOT as _PROJECT_ROOT

# 绝对路径；测试可 monkeypatch 本模块的 JOBS_DIR。勿用相对 Path("jobs")（GUI 曾把 cwd 设到 gui/）。
JOBS_DIR: Path = _DEFAULT_JOBS_DIR


class JobStatus(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    DONE = "done"
    ERROR = "error"
    CANCELLED = "cancelled"


@dataclass
class Job:
    id: str
    status: JobStatus = JobStatus.QUEUED
    progress: list = field(default_factory=list)   # 每步事件文本
    zip_path: str = ""                              # 数据 zip 路径
    sources_zip_path: str = ""                      # gen/validator/range 源码包
    checker_zip_path: str = ""                      # special judge zip 路径（空表示无）
    error: str = ""                                 # 失败原因
    cancel_requested: bool = False                  # 客户端请求取消
    started_at: float | None = None                 # time.time()，任务开始
    finished_at: float | None = None                # time.time()，任务结束
    elapsed_ms: int = 0                             # 总耗时（毫秒）
    token_usage: dict = field(default_factory=dict) # LLM token 累计
    # 产物体积（字节）：打包后由 batch 写入，供 GUI 状态栏展示
    artifact_sizes: dict = field(default_factory=dict)
    lock: threading.Lock = field(default_factory=threading.Lock, repr=False)


_store: dict[str, Job] = {}
_store_lock = threading.Lock()

# 同时执行的 job 数（CPU/LLM/全局编译资源）；排队等待，不拒绝提交
MAX_CONCURRENT_JOBS = max(1, int(os.environ.get("GET_DATA_MAX_JOBS", "1")))
_job_slots = threading.Semaphore(MAX_CONCURRENT_JOBS)

# progress 落盘节流：避免每条日志都写盘
_PROGRESS_PERSIST_EVERY = 8


def _job_dir(job_id: str) -> Path:
    return JOBS_DIR / job_id


def _status_path(job_id: str) -> Path:
    return _job_dir(job_id) / "job_status.json"


def persist_job(job: Job) -> None:
    """把任务元数据写入 jobs/<id>/job_status.json（供重启后查询）。"""
    with job.lock:
        data = {
            "id": job.id,
            "status": job.status.value,
            "progress": list(job.progress),
            "error": job.error,
            "zip_path": job.zip_path,
            "sources_zip_path": job.sources_zip_path,
            "checker_zip_path": job.checker_zip_path,
            "cancel_requested": job.cancel_requested,
            "started_at": job.started_at,
            "finished_at": job.finished_at,
            "elapsed_ms": job.elapsed_ms,
            "token_usage": dict(job.token_usage),
            "artifact_sizes": dict(job.artifact_sizes),
        }
    path = _status_path(job.id)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(path)
    except Exception:
        pass


def _load_job_from_disk(job_id: str) -> Job | None:
    path = _status_path(job_id)
    jd = _job_dir(job_id)
    if not path.is_file():
        # 无元数据但已有 data.zip：视为可下载的完成任务（兼容旧目录）
        if (jd / "data.zip").is_file():
            job = Job(id=job_id, status=JobStatus.DONE, zip_path=str(jd / "data.zip"))
            if (jd / "sources.zip").is_file():
                job.sources_zip_path = str(jd / "sources.zip")
            if (jd / "checker.zip").is_file():
                job.checker_zip_path = str(jd / "checker.zip")
            return job
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    try:
        status = JobStatus(data.get("status") or "error")
    except ValueError:
        status = JobStatus.ERROR
    # 进程崩溃时 running/queued 视为中断；若已有完整 data.zip 则按完成处理
    recovered_crash = False
    if status in (JobStatus.RUNNING, JobStatus.QUEUED):
        if (jd / "data.zip").is_file():
            status = JobStatus.DONE
        else:
            status = JobStatus.ERROR
            data["error"] = data.get("error") or "进程重启：任务中断"
            recovered_crash = True
    job = Job(
        id=job_id,
        status=status,
        progress=list(data.get("progress") or []),
        zip_path=str(data.get("zip_path") or ""),
        sources_zip_path=str(data.get("sources_zip_path") or ""),
        checker_zip_path=str(data.get("checker_zip_path") or ""),
        error=str(data.get("error") or ""),
        cancel_requested=bool(data.get("cancel_requested")),
        started_at=data.get("started_at"),
        finished_at=data.get("finished_at"),
        elapsed_ms=int(data.get("elapsed_ms") or 0),
        token_usage=dict(data.get("token_usage") or {}),
        artifact_sizes=dict(data.get("artifact_sizes") or {}),
    )
    if recovered_crash:
        _ensure_failure_context_for_crash(jd, job)
    # 补全 zip 路径与体积（若元数据空但文件仍在）
    if not job.zip_path and (jd / "data.zip").is_file():
        job.zip_path = str(jd / "data.zip")
    if not job.sources_zip_path and (jd / "sources.zip").is_file():
        job.sources_zip_path = str(jd / "sources.zip")
    if not job.checker_zip_path and (jd / "checker.zip").is_file():
        job.checker_zip_path = str(jd / "checker.zip")
    if not job.artifact_sizes:
        sizes: dict = {}
        try:
            if job.zip_path and Path(job.zip_path).is_file():
                sizes["data_zip"] = Path(job.zip_path).stat().st_size
            out_dir = jd / "out"
            if out_dir.is_dir():
                total = 0
                for p in out_dir.iterdir():
                    if p.is_file() and p.suffix in (".in", ".out"):
                        total += p.stat().st_size
                sizes["out_total"] = total
            if job.sources_zip_path and Path(job.sources_zip_path).is_file():
                sizes["sources_zip"] = Path(job.sources_zip_path).stat().st_size
            if job.checker_zip_path and Path(job.checker_zip_path).is_file():
                sizes["checker_zip"] = Path(job.checker_zip_path).stat().st_size
        except OSError:
            pass
        if sizes:
            job.artifact_sizes = sizes
    return job


def _ensure_failure_context_for_crash(job_dir: Path, job: Job) -> None:
    """进程崩溃恢复为 ERROR 时补写 failure_context，便于同题失败复用。"""
    if (job_dir / "failure_context.json").is_file():
        return
    try:
        from runners.failure import infer_failure_stage, write_failure_context

        stmt = ""
        for name in ("statement.txt", "statement_simplified.txt"):
            p = job_dir / name
            if p.is_file():
                stmt = p.read_text(encoding="utf-8", errors="replace")
                if stmt.strip():
                    break
        lang = "cpp"
        std = ""
        if (job_dir / "std.cpp").is_file():
            std = (job_dir / "std.cpp").read_text(encoding="utf-8", errors="replace")
            lang = "cpp"
        elif (job_dir / "std.py").is_file():
            std = (job_dir / "std.py").read_text(encoding="utf-8", errors="replace")
            lang = "python"
        stage = infer_failure_stage(list(job.progress))
        err = job.error or "进程重启：任务中断"
        write_failure_context(
            job_dir, job.id, stage, err, stmt, std, lang, job_dir / "range.json",
        )
    except Exception:
        pass


def _merge_job_tree(src: Path, dst: Path) -> int:
    """把 src 合并进 dst。

    - 缺文件则拷贝
    - 同名文件：上下文/指纹相关文件优先用 src（gui/jobs 才是当时真实提交）；
      其余优先保留更大的（通常是完整产物）
    """
    prefer_src_names = {
        "success_context.json",
        "failure_context.json",
        "job_status.json",
        "std.cpp",
        "std.py",
        "statement.txt",
        "statement_simplified.txt",
        "input_desc.txt",
        "output_desc.txt",
    }
    n = 0
    dst.mkdir(parents=True, exist_ok=True)
    for item in src.iterdir():
        target = dst / item.name
        if item.is_dir():
            n += _merge_job_tree(item, target)
            continue
        if not target.is_file():
            shutil.copy2(item, target)
            n += 1
            continue
        try:
            if item.name in prefer_src_names or item.stat().st_size > target.stat().st_size:
                shutil.copy2(item, target)
                n += 1
        except OSError:
            pass
    return n


def migrate_legacy_gui_jobs() -> dict:
    """合并 gui/jobs（旧 cwd）到项目根 jobs/，修复复用扫错目录。"""
    legacy = _PROJECT_ROOT / "gui" / "jobs"
    dest_root = JOBS_DIR
    if not legacy.is_dir() or legacy.resolve() == dest_root.resolve():
        return {"merged": 0, "files": 0, "legacy": str(legacy)}
    dest_root.mkdir(parents=True, exist_ok=True)
    merged = 0
    files = 0
    for src in legacy.iterdir():
        if not src.is_dir():
            continue
        dst = dest_root / src.name
        n = _merge_job_tree(src, dst)
        if n:
            merged += 1
            files += n
    return {"merged": merged, "files": files, "legacy": str(legacy), "dest": str(dest_root)}


def load_persisted_jobs(lookback: int = 80) -> int:
    """启动时从 jobs/ 恢复近期任务索引。返回载入数量。"""
    root = JOBS_DIR
    if not root.is_dir():
        return 0
    dirs = sorted(
        [p for p in root.iterdir() if p.is_dir()],
        key=lambda p: p.name,
        reverse=True,
    )[:lookback]
    n = 0
    with _store_lock:
        for d in dirs:
            if d.name in _store:
                continue
            job = _load_job_from_disk(d.name)
            if job is None:
                continue
            _store[job.id] = job
            n += 1
    return n


def create_job() -> Job:
    # 目录名：日期 + 时间戳，例如 20260730_210456
    jid = datetime.now().strftime("%Y%m%d_%H%M%S")
    base = jid
    suffix = 0
    while jid in _store or (JOBS_DIR / jid).exists():
        suffix += 1
        jid = f"{base}_{suffix}"
    job = Job(id=jid)
    with _store_lock:
        _store[jid] = job
    persist_job(job)
    return job


def get_job(jid: str) -> Job | None:
    with _store_lock:
        job = _store.get(jid)
    if job is not None:
        return job
    # 冷启动后按需从磁盘加载
    job = _load_job_from_disk(jid)
    if job is None:
        return None
    with _store_lock:
        _store.setdefault(jid, job)
        return _store[jid]


def add_progress(job: Job, msg: str) -> None:
    with job.lock:
        job.progress.append(msg)
        n = len(job.progress)
    if n == 1 or n % _PROGRESS_PERSIST_EVERY == 0:
        persist_job(job)


def acquire_job_slot(job: Job) -> bool:
    """阻塞等待执行槽；若已取消则返回 False。"""
    add_progress(
        job,
        f"【排队】等待执行槽（并发上限 {MAX_CONCURRENT_JOBS}）…",
    )
    while True:
        got = _job_slots.acquire(timeout=0.5)
        with job.lock:
            cancelled = job.cancel_requested
        if cancelled:
            if got:
                _job_slots.release()
            return False
        if got:
            return True


def release_job_slot() -> None:
    _job_slots.release()


def count_active_jobs() -> int:
    """内存中 queued/running 的任务数。"""
    with _store_lock:
        return sum(
            1
            for j in _store.values()
            if j.status in (JobStatus.QUEUED, JobStatus.RUNNING)
        )


def mark_job_started(job: Job) -> None:
    """记录开始时间，并重置本线程 token 累计与 JSONL 追踪。"""
    from agent.llm import reset_token_usage, set_token_usage_sink
    from agent.trace import set_trace_context

    reset_token_usage()
    set_trace_context(job_id=job.id, path=_job_dir(job.id) / "agent_trace.jsonl")

    def _sink(usage: dict) -> None:
        with job.lock:
            job.token_usage = dict(usage)

    set_token_usage_sink(_sink)
    with job.lock:
        job.started_at = time.time()
        job.finished_at = None
        job.elapsed_ms = 0
        job.token_usage = {}
        job.status = JobStatus.RUNNING
    persist_job(job)


def mark_job_finished(job: Job) -> None:
    """记录结束时间与 token 用量，并写入一条统计进度。"""
    from agent.llm import format_token_usage, get_token_usage, set_token_usage_sink
    from agent.trace import clear_trace_context

    usage = get_token_usage()
    set_token_usage_sink(None)
    clear_trace_context()
    now = time.time()
    with job.lock:
        if job.started_at is None:
            job.started_at = now
        job.finished_at = now
        job.elapsed_ms = max(0, int((job.finished_at - job.started_at) * 1000))
        job.token_usage = dict(usage)
        elapsed_ms = job.elapsed_ms
        sizes = dict(job.artifact_sizes)
    size_s = format_artifact_sizes(sizes)
    size_part = f" | {size_s}" if size_s else ""
    add_progress(
        job,
        f"【统计】耗时 {_format_elapsed_ms(elapsed_ms)} | tokens: {format_token_usage(usage)}{size_part}",
    )
    persist_job(job)


def format_bytes(n: int | float | None) -> str:
    """人类可读的字节数。"""
    try:
        b = float(n or 0)
    except (TypeError, ValueError):
        return "-"
    if b < 0:
        b = 0
    units = ("B", "KB", "MB", "GB")
    i = 0
    while b >= 1024 and i < len(units) - 1:
        b /= 1024
        i += 1
    if i == 0:
        return f"{int(b)}{units[i]}"
    return f"{b:.1f}{units[i]}"


def format_artifact_sizes(sizes: dict | None) -> str:
    """把 artifact_sizes 格式化成状态栏短串。"""
    if not sizes:
        return ""
    parts = []
    mapping = (
        ("data_zip", "data.zip"),
        ("out_total", "测例"),
        ("sources_zip", "sources"),
        ("checker_zip", "checker"),
    )
    for key, label in mapping:
        if key in sizes and sizes[key] is not None:
            parts.append(f"{label}={format_bytes(sizes[key])}")
    return " · ".join(parts)


def _format_elapsed_ms(elapsed_ms: int) -> str:
    total_s = max(0, elapsed_ms) / 1000.0
    if total_s < 60:
        return f"{total_s:.1f}s"
    m = int(total_s // 60)
    s = total_s - m * 60
    if m < 60:
        return f"{m}m{s:04.1f}s"
    h = m // 60
    m = m % 60
    return f"{h}h{m}m{s:04.1f}s"


def snapshot(job: Job) -> dict:
    """返回给前端的只读快照。"""
    with job.lock:
        started = job.started_at
        finished = job.finished_at
        elapsed_ms = job.elapsed_ms
        usage = dict(job.token_usage)
        status = job.status
        sizes = dict(job.artifact_sizes)
        data = {
            "id": job.id,
            "status": status.value,
            "progress": list(job.progress),
            "error": job.error,
            "has_zip": bool(job.zip_path),
            "has_sources_zip": bool(job.sources_zip_path),
            "has_checker_zip": bool(job.checker_zip_path),
            "cancel_requested": job.cancel_requested,
            "started_at": started,
            "finished_at": finished,
            "elapsed_ms": elapsed_ms,
            "token_usage": usage,
            "artifact_sizes": sizes,
            "artifact_sizes_text": format_artifact_sizes(sizes),
            "max_concurrent_jobs": MAX_CONCURRENT_JOBS,
        }

    if status == JobStatus.RUNNING and started is not None:
        data["elapsed_ms"] = max(0, int((time.time() - started) * 1000))
    elif status in (JobStatus.DONE, JobStatus.ERROR, JobStatus.CANCELLED) and started is not None:
        if finished is None:
            data["elapsed_ms"] = max(0, int((time.time() - started) * 1000))

    if status in (JobStatus.ERROR, JobStatus.DONE, JobStatus.CANCELLED):
        try:
            p = _job_dir(job.id) / "failure_context.json"
            if p.exists():
                data["failure_context"] = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            pass
    return data


def request_cancel(job: Job) -> bool:
    """标记任务为取消请求。worker 线程应主动检查并结束。"""
    with job.lock:
        if job.status not in (JobStatus.RUNNING, JobStatus.QUEUED):
            return False
        job.cancel_requested = True
    persist_job(job)
    return True


def _text_hash(text: str) -> str:
    """返回文本的 sha256 摘要（用于同题校验）。"""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def clean_jobs(
    keep_days: int | None = None,
    max_keep: int | None = None,
    *,
    dry_run: bool = False,
) -> dict:
    """清理过期 / 超额的 jobs/ 目录，释放磁盘。

    - keep_days：删除 mtime 早于 N 天的目录（默认读 JOB_RETENTION_DAYS）
    - max_keep：若 >0，在按天清理后再按目录名（时间戳）只保留最近 N 个
    - 不会删除内存中仍为 queued/running 的任务目录

    返回 {"removed": [...], "kept": n, "bytes_freed": int, "dry_run": bool}
    """
    import shutil

    try:
        from config.settings import get_settings

        s = get_settings()
        if keep_days is None:
            keep_days = int(s.job_retention_days)
        if max_keep is None:
            max_keep = int(s.job_max_keep)
    except Exception:
        if keep_days is None:
            keep_days = 30
        if max_keep is None:
            max_keep = 0

    root = JOBS_DIR
    if not root.is_dir():
        return {"removed": [], "kept": 0, "bytes_freed": 0, "dry_run": dry_run}

    cutoff = time.time() - max(0, int(keep_days)) * 86400
    active_ids: set[str] = set()
    with _store_lock:
        for jid, j in _store.items():
            if j.status in (JobStatus.QUEUED, JobStatus.RUNNING):
                active_ids.add(jid)

    dirs = [p for p in root.iterdir() if p.is_dir()]
    dirs.sort(key=lambda p: p.name, reverse=True)

    to_remove: list[Path] = []
    for d in dirs:
        if d.name in active_ids:
            continue
        try:
            mtime = d.stat().st_mtime
        except OSError:
            continue
        if mtime < cutoff:
            to_remove.append(d)

    # LRU：按名字新→旧，超出 max_keep 的删掉（已标记删除的除外）
    if max_keep and max_keep > 0:
        survivors = [d for d in dirs if d not in to_remove and d.name not in active_ids]
        for d in survivors[max_keep:]:
            if d not in to_remove:
                to_remove.append(d)

    bytes_freed = 0
    removed_names: list[str] = []
    for d in to_remove:
        size = _dir_size(d)
        if not dry_run:
            try:
                shutil.rmtree(d)
            except OSError:
                continue
            with _store_lock:
                _store.pop(d.name, None)
        bytes_freed += size
        removed_names.append(d.name)

    kept = len(dirs) - len(removed_names)
    return {
        "removed": removed_names,
        "kept": max(0, kept),
        "bytes_freed": bytes_freed,
        "dry_run": dry_run,
        "keep_days": int(keep_days),
        "max_keep": int(max_keep or 0),
    }


def _dir_size(path: Path) -> int:
    total = 0
    try:
        for p in path.rglob("*"):
            if p.is_file():
                try:
                    total += p.stat().st_size
                except OSError:
                    pass
    except OSError:
        pass
    return total
